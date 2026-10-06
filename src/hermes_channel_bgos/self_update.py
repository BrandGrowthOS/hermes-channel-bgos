"""One-click self-update primitives (update_rpc + readiness heartbeat).

Wire contract: BrandGrowthOS/BGOS branch design/one-click-plugin-update,
docs/handoff/one-click-plugin-update/wire-contract.md. This module owns the
daemon-side facts the contract needs:

- the newest version at the daemon's OWN pinned source (the public repo's
  main pyproject.toml, daily-cached; the backend never tells us a version
  or a URL, the update_rpc frame is `{rpcId, op}` and nothing else),
- the same-major-newer-only update decision (ported from the openclaw
  plugin's decideVersionUpdate),
- the supervisor probes (a systemd user unit on Linux, the launchd job on
  macOS) that decide whether this process has relaunch authority,
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
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__
from .update_cli import find_checkout_root, _is_official_remote

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
    no_update_available); `detail` stays local in logs. `retry_after` is
    the seconds until a `soak` refusal would pass.
    """

    def __init__(
        self, reason: str, detail: str = "", *, retry_after: float | None = None,
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.retry_after = retry_after


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
    # Only the header names the service. A log line mentioning a different
    # service is not restart authority. Confirm this process belongs to it.
    match = re.match(r"^[\s●*]*([A-Za-z0-9:@_.\\-]+\.service)(?:\s|$)", result.stdout or "")
    if match is None or not _owns_systemd_unit(match.group(1)):
        return None
    return match.group(1)


def _owns_systemd_unit(unit: str) -> bool:
    try:
        groups = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(
        line.split(":", 2)[-1].rstrip("/").split("/")[-1] == unit
        for line in groups.splitlines()
    )


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


# -----------------------------------------------------------------------------
# Relaunch authority (launchd job, macOS)
# -----------------------------------------------------------------------------

# Hermes upstream's LaunchAgent label (`hermes gateway install`; the label
# update_cli.detect_restart_command probes and install.sh kickstarts). A named
# profile's gateway gets `ai.hermes.gateway-<profile>`.
HERMES_LAUNCHD_LABEL = "ai.hermes.gateway"

# A profile name is spliced into a launchd label and a kickstart argv: only
# the characters Hermes profile names use. Anything else is not probed.
_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

# The launchd domains a Hermes gateway job is loaded in, in the order Hermes
# upstream probes them (hermes_cli/gateway.py _launchd_domain): gui/<uid> for
# an Aqua login session, user/<uid> for a Background or SSH session.
_LAUNCHD_DOMAINS = ("gui", "user")

# The only service targets a restart may ever kickstart. `kickstart -k` kills
# whatever the target names, so a foreign or malformed target is refused even
# though the probe is the only producer of targets.
_LAUNCHD_TARGET_RE = re.compile(
    r"(?:gui|user)/\d+/" + re.escape(HERMES_LAUNCHD_LABEL)
    + r"(?:-[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?"
)

_PID_LINE_RE = re.compile(r"^[ \t]*pid = (\S+)[ \t]*$", re.MULTILINE)

_launchd_result: object = _UNIT_UNRESOLVED

# Default for _probe_launchd_job's hermes_home: read the process env.
_HOME_FROM_ENV: object = object()


def launchd_service_target() -> str | None:
    """`<domain>/<uid>/<label>` (domain gui or user) of the launchd job
    whose running pid IS this process, else None.

    Probed once and cached for the process lifetime (supervision cannot
    change mid-run), like systemd_user_unit. Design 2.3: on macOS the
    systemd probe always answers none, so before this every update staged
    and nothing ever restarted onto it.
    """
    global _launchd_result
    if _launchd_result is not _UNIT_UNRESOLVED:
        return _launchd_result  # type: ignore[return-value]
    _launchd_result = _probe_launchd_job()
    return _launchd_result  # type: ignore[return-value]


def _launchd_candidate_labels(hermes_home: str | None) -> list[str]:
    labels = [HERMES_LAUNCHD_LABEL]
    if hermes_home:
        home = Path(hermes_home)
        # Named profiles live under <root>/profiles/<name>/ (mirrors
        # hermes_cli.profiles.get_profile_dir, see topology.profile_dir).
        if home.parent.name == "profiles" and _PROFILE_NAME_RE.fullmatch(home.name):
            labels.append(f"{HERMES_LAUNCHD_LABEL}-{home.name}")
    return labels


def _launchd_print_pid(body: str | None) -> int | None:
    """The job's running pid from a `launchctl print` body: the FIRST
    `pid = N` line (the job's own; nested sections come after it)."""
    match = _PID_LINE_RE.search(body or "")
    if match is None or not match.group(1).isdigit():
        return None
    return int(match.group(1))


def _probe_launchd_job(
    *,
    platform: str | None = None,
    uid: int | None = None,
    pid: int | None = None,
    hermes_home: str | None | object = _HOME_FROM_ENV,
    run: Callable[..., Any] | None = None,
) -> str | None:
    """Every argument is injectable so tests never run launchctl."""
    if (platform if platform is not None else sys.platform) != "darwin":
        return None
    try:
        uid = os.getuid() if uid is None else uid
    except Exception:
        return None
    pid = os.getpid() if pid is None else pid
    if hermes_home is _HOME_FROM_ENV:
        # The PROCESS home (what the LaunchAgent started us with), not a
        # multiplex profile's context-local home.
        hermes_home = os.environ.get("HERMES_HOME", "").strip() or None
    run = run if run is not None else subprocess.run
    for label in _launchd_candidate_labels(hermes_home):  # type: ignore[arg-type]
        # Both domains, gui first: a gateway installed from an SSH or
        # Background session lives in user/<uid>, where a gui-only probe
        # never finds it (every update would stage forever).
        for domain in _LAUNCHD_DOMAINS:
            target = f"{domain}/{uid}/{label}"
            try:
                result = run(
                    ["launchctl", "print", target],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except Exception:
                continue
            if result.returncode != 0:
                continue
            # A loaded job running ANOTHER pid (a second Hermes, a wrapper
            # that did not exec, the same label in the other domain) is not
            # authority: kickstart -k would kill that process and leave
            # this one running beside the new instance.
            if _launchd_print_pid(result.stdout) == pid:
                return target
    return None


@dataclass(frozen=True)
class Supervisor:
    """A verified relaunch authority: `kind` is the updateReadiness
    `supervised` value, `name` the unit (systemd) or service target
    (launchd) the restart addresses."""

    kind: str
    name: str


def verified_supervisor() -> Supervisor | None:
    """The supervisor that will bring this process back after a restart,
    or None (then an update may only stage, never exit; decision D8)."""
    unit = systemd_user_unit()
    if unit:
        return Supervisor("systemd", unit)
    target = launchd_service_target()
    if target:
        return Supervisor("launchd", target)
    return None


def update_readiness() -> dict:
    """The heartbeat's updateReadiness object (contract section 1).

    rollbackLatched is constitutionally False here: this plugin has no
    rollback latch (rollback is the operator-run command update_cli
    prints), so it can never report one tripped.
    """
    supervisor = verified_supervisor()
    return {
        "supervised": supervisor.kind if supervisor else "none",
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


def soak_remaining(committed_at: float, now: float, soak_seconds: float) -> float:
    """Pure: seconds until a commit made at `committed_at` (epoch) has been
    published for `soak_seconds`; 0 once it has."""
    return max(0.0, committed_at + soak_seconds - now)


def apply_update(
    clone_dir: Path | None = None,
    *,
    soak_seconds: float | None = None,
    now: Callable[[], float] | None = None,
) -> AppliedUpdate:
    """Fast-forward the editable clone to origin/main and report versions.

    Raises SelfUpdateError with a short reason on every refusal path:
    not_a_git_checkout, dirty_tree (brake: local edits are never touched),
    fetch_failed, no_update_available, major_jump (same-major gate),
    merge_failed (diverged history; ff-only never rewrites), plus the
    plumbing reasons git_unavailable and pyproject_unreadable. The running
    process still serves the OLD code afterwards; the caller owns the
    restart (or reports 'staged' when it has no relaunch authority).

    `soak_seconds` (the unattended scheduled apply only; update_now is a
    person asking now) also refuses with `soak` while the fetched
    origin/main commit is younger than that by its git committer time, so a
    bad release can be pulled before every supervised host takes it. `now`
    is the epoch clock, injectable for tests.
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

    remote = _git(root, "remote", "get-url", "origin")
    if remote.returncode != 0 or not _is_official_remote(remote.stdout):
        raise SelfUpdateError("untrusted_update_source")

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

    if soak_seconds is not None:
        # The commit the merge would take, as fetched: a later change on
        # main restarts the soak even when the version did not move.
        shown = _git(root, "show", "-s", "--format=%ct", f"origin/{MAIN_BRANCH}")
        stamp = shown.stdout.strip() if shown.returncode == 0 else ""
        if not stamp.isdigit():
            raise SelfUpdateError("fetch_failed", shown.stderr or "no committer time")
        remaining = soak_remaining(
            int(stamp), (now or time.time)(), soak_seconds,
        )
        if remaining > 0:
            raise SelfUpdateError(
                "soak",
                f"origin/{MAIN_BRANCH} {target_version} needs {int(remaining)}s more",
                retry_after=remaining,
            )

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
        result = subprocess.run(
            [
                "systemd-run", "--user", "--on-active=2s",
                "systemctl", "--user", "restart", unit,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            timeout=10,
            check=False,
        )
    except Exception:
        log.exception("self_update restart spawn failed unit=%s", unit)
        return False
    return result.returncode == 0


def schedule_launchd_restart(
    target: str, *, popen: Callable[..., Any] | None = None,
) -> bool:
    """Spawn a fully detached, 2s-delayed `launchctl kickstart -k <target>`.

    launchd has no transient timer like `systemd-run --on-active`, so the
    delay lives in a child shell in its OWN session: launchd tearing down
    this job's process group does not take the pending kickstart with it,
    and this process is free to flush its final progress POST first. It
    must be kickstart -k, never a plain exit: a KeepAlive {SuccessfulExit:
    false} plist (this Mac's own gateway) does not relaunch a clean exit.
    Returns False on a refused target or any spawn failure (never raises).
    """
    if not _LAUNCHD_TARGET_RE.fullmatch(target or ""):
        log.warning("self_update refused launchd restart target=%r", target)
        return False
    popen = popen if popen is not None else subprocess.Popen
    try:
        popen(
            [
                "/bin/sh", "-c", 'sleep 2; exec launchctl kickstart -k "$1"',
                "bgos-gateway-restart", target,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        log.exception("self_update restart spawn failed target=%s", target)
        return False
    return True


def schedule_supervisor_restart(supervisor: Supervisor) -> bool:
    """Hand the restart to whichever verified supervisor owns this process."""
    if supervisor.kind == "systemd":
        return schedule_unit_restart(supervisor.name)
    if supervisor.kind == "launchd":
        return schedule_launchd_restart(supervisor.name)
    return False
