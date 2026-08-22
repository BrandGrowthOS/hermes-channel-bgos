"""One-click self-update primitives (update_rpc + readiness heartbeat).

Wire contract: BrandGrowthOS/BGOS branch design/one-click-plugin-update,
docs/handoff/one-click-plugin-update/wire-contract.md. This module owns the
daemon-side facts the contract needs:

- the newest version at the daemon's OWN pinned source (the public repo's
  main pyproject.toml, daily-cached; the backend never tells us a version
  or a URL, the update_rpc frame is `{rpcId, op}` and nothing else),
- the same-major-newer-only update decision (ported from the openclaw
  plugin's decideVersionUpdate),
- the systemd user-unit probe that decides whether this process has
  relaunch authority,
- `apply_update`: a fast-forward pull of the editable clone the running
  module was imported from (dirty-tree brake, never a reset).

Everything here is synchronous and best-effort: callers on the asyncio
side run these in a worker thread and the query helpers never raise.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__
from .update_cli import find_checkout_root

log = logging.getLogger(__name__)

# The pinned source of truth for "what is the newest version": the public
# repo's main-branch pyproject. There is no PyPI release (the package is
# private), so raw.githubusercontent is the only registry equivalent.
PYPROJECT_URL = (
    "https://raw.githubusercontent.com/BrandGrowthOS/hermes-channel-bgos/"
    "main/pyproject.toml"
)
MAIN_BRANCH = "main"

_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_FETCH_TIMEOUT_SECONDS = 10.0
_GIT_TIMEOUT_SECONDS = 120.0


class SelfUpdateError(RuntimeError):
    """A self-update failure carrying a short wire-safe reason code.

    `reason` is what rides the update_rpc progress `message` field
    (e.g. dirty_tree, fetch_failed, not_a_git_checkout,
    no_update_available); `detail` stays local in logs.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass(frozen=True)
class AppliedUpdate:
    before_version: str
    after_version: str


# -----------------------------------------------------------------------------
# Version parsing + the update decision
# -----------------------------------------------------------------------------


def parse_version_tuple(version: str | None) -> tuple[int, int, int] | None:
    """Leading MAJOR.MINOR.PATCH of a version string, else None."""
    if not isinstance(version, str):
        return None
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def parse_pyproject_version(text: str | None) -> str | None:
    """`project.version` out of a pyproject.toml body. Never raises."""
    if not isinstance(text, str):
        return None
    try:
        version = tomllib.loads(text)["project"]["version"]
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(version, str) or parse_version_tuple(version) is None:
        return None
    return version.strip()


def decide_version_update(current: str | None, latest: str | None) -> bool:
    """Same-major-newer-only gate (openclaw decideVersionUpdate semantics).

    True only when both versions parse, the majors match, and `latest` is
    strictly newer. Major jumps are out of one-click scope in v1; they
    need a human.
    """
    cur = parse_version_tuple(current)
    new = parse_version_tuple(latest)
    if cur is None or new is None:
        return False
    if new[0] != cur[0]:
        return False
    return new > cur


# -----------------------------------------------------------------------------
# Latest-version check (daily cache)
# -----------------------------------------------------------------------------


@dataclass
class _LatestCheck:
    version: str | None
    checked_at: float


_latest_check: _LatestCheck | None = None


def _fetch_pyproject_text() -> str | None:
    try:
        resp = httpx.get(
            PYPROJECT_URL,
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except Exception:
        log.debug("self_update version fetch failed", exc_info=True)
        return None
    if resp.status_code != 200:
        return None
    return resp.text


def latest_known_version() -> str | None:
    """Newest version at the pinned source, checked at most once a day.

    Failure (network, non-200, unparsable pyproject) yields None and is
    cached like a success so a broken source can't turn the heartbeat loop
    into a retry hammer. Never raises.
    """
    global _latest_check
    now = time.monotonic()
    if (
        _latest_check is not None
        and now - _latest_check.checked_at < _CHECK_INTERVAL_SECONDS
    ):
        return _latest_check.version
    version = parse_pyproject_version(_fetch_pyproject_text())
    _latest_check = _LatestCheck(version, now)
    return version


# -----------------------------------------------------------------------------
# Relaunch authority (systemd user unit) + readiness assembly
# -----------------------------------------------------------------------------


# Distinct sentinel: None is a valid (cached) probe outcome.
_UNIT_UNRESOLVED: object = object()
_unit_result: object = _UNIT_UNRESOLVED


def systemd_user_unit() -> str | None:
    """Name of the systemd user .service supervising this process, else None.

    Probes `systemctl --user status <pid>` once and caches the outcome for
    the process lifetime (supervision cannot change mid-run). Any failure,
    no systemctl, non-zero exit, unparsable output, a .scope instead of a
    restartable .service, resolves to None.
    """
    global _unit_result
    if _unit_result is not _UNIT_UNRESOLVED:
        return _unit_result  # type: ignore[return-value]
    _unit_result = _probe_systemd_user_unit()
    return _unit_result  # type: ignore[return-value]


def _probe_systemd_user_unit() -> str | None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "status", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"([A-Za-z0-9:@_.\\-]+\.service)\b", result.stdout or "")
    if match is None:
        return None
    return match.group(1)


def auto_update_enabled() -> bool:
    """The BGOS_AUTO_UPDATE kill switch. Unset means enabled."""
    raw = os.environ.get("BGOS_AUTO_UPDATE", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def clone_root() -> Path | None:
    """The editable-install checkout the running module was imported from,
    verified to be a git clone of this package. None for site installs or
    any layout `apply_update` must refuse."""
    try:
        return find_checkout_root(Path(__file__).resolve())
    except Exception:
        return None


def pending_restart_version(clone_dir: Path | None = None) -> str | None:
    """The on-disk clone version when it differs from the running module.

    Non-None means an update was installed (git pulled) but the gateway has
    not restarted yet: the editable install keeps serving the old code
    until relaunch. Rides the heartbeat as pendingRestartVersion.
    """
    root = clone_dir if clone_dir is not None else clone_root()
    if root is None:
        return None
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    on_disk = parse_pyproject_version(text)
    if on_disk is None or on_disk == __version__:
        return None
    return on_disk


def update_readiness() -> dict:
    """The heartbeat's updateReadiness object (contract section 1).

    rollbackLatched is constitutionally False here: this plugin has no
    rollback latch (rollback is the operator-run command update_cli
    prints), so it can never report one tripped.
    """
    return {
        "supervised": "systemd" if systemd_user_unit() else "none",
        "autoUpdateEnabled": auto_update_enabled(),
        "rollbackLatched": False,
        "pendingRestartVersion": pending_restart_version(),
    }


def detect_attachment_mode(hermes_home: Path) -> str:
    """'plugin' when the plugin-path attachment dir exists under the Hermes
    home, else 'fork-patch-or-unknown'. Log-only: both modes update the
    same editable clone, so the updater detects and reports rather than
    branching (see docs/distribution-decision.md)."""
    try:
        if (hermes_home.expanduser() / "plugins" / "bgos").exists():
            return "plugin"
    except OSError:
        pass
    return "fork-patch-or-unknown"


# -----------------------------------------------------------------------------
# apply_update: fast-forward the editable clone
# -----------------------------------------------------------------------------


def _git(clone_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(clone_dir), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise SelfUpdateError("git_unavailable", str(exc)) from exc


def _local_pyproject_version(clone_dir: Path) -> str:
    try:
        text = (clone_dir / "pyproject.toml").read_text(encoding="utf-8")
    except OSError as exc:
        raise SelfUpdateError("pyproject_unreadable", str(exc)) from exc
    version = parse_pyproject_version(text)
    if version is None:
        raise SelfUpdateError("pyproject_unreadable")
    return version


def apply_update(clone_dir: Path | None = None) -> AppliedUpdate:
    """Fast-forward the editable clone to origin/main and report versions.

    Raises SelfUpdateError with a short reason on every refusal path:
    not_a_git_checkout, dirty_tree (brake: local edits are never touched),
    fetch_failed, no_update_available, major_jump (same-major gate),
    merge_failed (diverged history; ff-only never rewrites), plus the
    plumbing reasons git_unavailable and pyproject_unreadable. The running
    process still serves the OLD code afterwards; the caller owns the
    restart (or reports 'staged' when it has no relaunch authority).
    """
    root = clone_dir if clone_dir is not None else clone_root()
    if root is None or not (root / ".git").exists():
        raise SelfUpdateError("not_a_git_checkout")

    before = _local_pyproject_version(root)

    status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0:
        raise SelfUpdateError("git_status_failed", status.stderr)
    if status.stdout.strip():
        raise SelfUpdateError("dirty_tree")

    fetch = _git(
        root, "fetch", "--prune", "origin",
        f"+refs/heads/{MAIN_BRANCH}:refs/remotes/origin/{MAIN_BRANCH}",
    )
    if fetch.returncode != 0:
        raise SelfUpdateError("fetch_failed", fetch.stderr)

    head = _git(root, "rev-parse", "--verify", "HEAD")
    target = _git(root, "rev-parse", "--verify", f"origin/{MAIN_BRANCH}")
    if head.returncode != 0 or target.returncode != 0:
        raise SelfUpdateError("fetch_failed", head.stderr or target.stderr)
    if head.stdout.strip() == target.stdout.strip():
        raise SelfUpdateError("no_update_available")

    shown = _git(root, "show", f"origin/{MAIN_BRANCH}:pyproject.toml")
    if shown.returncode != 0:
        raise SelfUpdateError("fetch_failed", shown.stderr)
    target_version = parse_pyproject_version(shown.stdout)
    if target_version is None:
        raise SelfUpdateError("pyproject_unreadable")
    if not decide_version_update(before, target_version):
        before_tuple = parse_version_tuple(before)
        target_tuple = parse_version_tuple(target_version)
        if (
            before_tuple is not None
            and target_tuple is not None
            and target_tuple[0] != before_tuple[0]
        ):
            raise SelfUpdateError("major_jump")
        raise SelfUpdateError("no_update_available")

    merge = _git(root, "merge", "--ff-only", f"origin/{MAIN_BRANCH}")
    if merge.returncode != 0:
        raise SelfUpdateError("merge_failed", merge.stderr)

    return AppliedUpdate(before, _local_pyproject_version(root))


def schedule_unit_restart(unit: str) -> bool:
    """Spawn a fully detached, 2s-delayed restart of the given user unit.

    `systemd-run --user --on-active=2s` hands the restart to the user
    manager as a transient timer, so this gateway process is free to flush
    its final progress POST before its own unit is torn down. Returns False
    on any spawn failure (the caller reports it; never raises).
    """
    try:
        subprocess.Popen(
            [
                "systemd-run", "--user", "--on-active=2s",
                "systemctl", "--user", "restart", unit,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        log.exception("self_update restart spawn failed unit=%s", unit)
        return False
    return True
