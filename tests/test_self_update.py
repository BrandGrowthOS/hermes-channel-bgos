"""Tests for the self_update module (one-click update, wire contract v1).

Covers the same-major-newer-only decision, the pyproject version parser,
the daily-cached latest-version check, the systemd relaunch-authority
probe, readiness assembly, and apply_update against real throwaway git
repos (the brake paths are the security surface: dirty tree, non-clone
layouts, fetch failures, major jumps).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_channel_bgos import __version__, self_update
from hermes_channel_bgos.self_update import (
    AppliedUpdate,
    SelfUpdateError,
    decide_version_update,
    parse_pyproject_version,
    parse_version_tuple,
)


# -----------------------------------------------------------------------------
# decide_version_update (openclaw decideVersionUpdate semantics)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("0.27.0", "0.28.0", True),
        ("0.27.0", "0.27.1", True),
        ("0.27.9", "0.28.0", True),
        ("0.27.0", "0.27.0", False),
        ("0.28.0", "0.27.0", False),
        # Major jumps are out of one-click scope in v1, both directions.
        ("0.28.0", "1.0.0", False),
        ("1.2.0", "0.28.0", False),
        # Invalid or missing input never updates.
        ("garbage", "0.28.0", False),
        ("0.27.0", "garbage", False),
        (None, "0.28.0", False),
        ("0.27.0", None, False),
        ("", "", False),
    ],
)
def test_decide_version_update(current, latest, expected) -> None:
    assert decide_version_update(current, latest) is expected


def test_parse_version_tuple_tolerates_suffixes() -> None:
    assert parse_version_tuple("0.28.0") == (0, 28, 0)
    assert parse_version_tuple(" 1.2.3-rc1 ") == (1, 2, 3)
    assert parse_version_tuple("1.2") is None
    assert parse_version_tuple(None) is None


# -----------------------------------------------------------------------------
# pyproject version parser
# -----------------------------------------------------------------------------


def test_parse_pyproject_version_reads_project_version() -> None:
    text = '[project]\nname = "x"\nversion = "0.28.0"\n'
    assert parse_pyproject_version(text) == "0.28.0"


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "not toml [",
        "[project]\nname = 'x'\n",
        '[project]\nversion = 123\n',
        '[project]\nversion = "not-semver"\n',
    ],
)
def test_parse_pyproject_version_bad_input_is_none(text) -> None:
    assert parse_pyproject_version(text) is None


def test_parser_reads_this_repos_pyproject() -> None:
    repo_pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert parse_pyproject_version(repo_pyproject.read_text()) == __version__


# -----------------------------------------------------------------------------
# latest_known_version (daily cache; failure -> None, never raises)
# -----------------------------------------------------------------------------


def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "_latest_check", None)


def test_latest_known_version_fetches_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_cache(monkeypatch)
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        assert kwargs["timeout"] == self_update._FETCH_TIMEOUT_SECONDS
        return SimpleNamespace(
            status_code=200,
            text='[project]\nname = "hermes-channel-bgos"\nversion = "0.29.0"\n',
        )

    monkeypatch.setattr(self_update.httpx, "get", fake_get)
    assert self_update.latest_known_version() == "0.29.0"
    assert self_update.latest_known_version() == "0.29.0"
    assert calls == [self_update.PYPROJECT_URL]


def test_latest_known_version_failure_is_none_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_cache(monkeypatch)
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        raise OSError("network down")

    monkeypatch.setattr(self_update.httpx, "get", fake_get)
    assert self_update.latest_known_version() is None
    assert self_update.latest_known_version() is None
    assert len(calls) == 1


def test_latest_known_version_http_error_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(
        self_update.httpx,
        "get",
        lambda url, **kwargs: SimpleNamespace(status_code=500, text="boom"),
    )
    assert self_update.latest_known_version() is None


def test_latest_known_version_cache_expires_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        self_update,
        "_latest_check",
        self_update._LatestCheck(
            "0.29.0",
            time.monotonic() - self_update._CHECK_INTERVAL_SECONDS - 1,
        ),
    )
    monkeypatch.setattr(
        self_update.httpx,
        "get",
        lambda url, **kwargs: SimpleNamespace(
            status_code=200, text='[project]\nversion = "0.30.0"\n',
        ),
    )
    assert self_update.latest_known_version() == "0.30.0"


# -----------------------------------------------------------------------------
# systemd relaunch-authority probe
# -----------------------------------------------------------------------------


def _fresh_unit_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        self_update, "_unit_result", self_update._UNIT_UNRESOLVED,
    )


def test_systemd_user_unit_parses_status_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_unit_cache(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "● hermes-gateway-ava.service - Hermes Agent Gateway\n"
                "     Loaded: loaded (/home/kc/.config/systemd/user/"
                "hermes-gateway-ava.service; enabled)\n"
            ),
        )

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    assert self_update.systemd_user_unit() == "hermes-gateway-ava.service"
    # Cached: the probe subprocess runs exactly once per process.
    assert self_update.systemd_user_unit() == "hermes-gateway-ava.service"
    assert len(calls) == 1
    assert calls[0][:3] == ["systemctl", "--user", "status"]


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=4, stdout=""),
        SimpleNamespace(returncode=0, stdout="no unit line here"),
        # A session scope is not a restartable service.
        SimpleNamespace(returncode=0, stdout="● session-4.scope - Session"),
    ],
)
def test_systemd_user_unit_failure_is_none(
    monkeypatch: pytest.MonkeyPatch, result,
) -> None:
    _fresh_unit_cache(monkeypatch)
    monkeypatch.setattr(
        self_update.subprocess, "run", lambda argv, **kwargs: result,
    )
    assert self_update.systemd_user_unit() is None


def test_systemd_user_unit_oserror_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_unit_cache(monkeypatch)

    def fake_run(argv, **kwargs):
        raise OSError("no systemctl")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    assert self_update.systemd_user_unit() is None


# -----------------------------------------------------------------------------
# Kill switch + pending restart + readiness assembly
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        (None, True),
        ("", True),
        ("1", True),
        ("true", True),
        ("0", False),
        ("false", False),
        ("No", False),
        ("OFF", False),
    ],
)
def test_auto_update_enabled(
    monkeypatch: pytest.MonkeyPatch, value, enabled,
) -> None:
    if value is None:
        monkeypatch.delenv("BGOS_AUTO_UPDATE", raising=False)
    else:
        monkeypatch.setenv("BGOS_AUTO_UPDATE", value)
    assert self_update.auto_update_enabled() is enabled


def test_pending_restart_version_reports_on_disk_difference(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "9.9.9"\n', encoding="utf-8",
    )
    assert self_update.pending_restart_version(tmp_path) == "9.9.9"


def test_pending_restart_version_none_when_matching_or_missing(
    tmp_path: Path,
) -> None:
    assert self_update.pending_restart_version(tmp_path) is None
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nversion = "{__version__}"\n', encoding="utf-8",
    )
    assert self_update.pending_restart_version(tmp_path) is None


def test_update_readiness_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        self_update, "systemd_user_unit", lambda: "hermes-gateway.service",
    )
    monkeypatch.setattr(
        self_update, "pending_restart_version", lambda clone_dir=None: "0.29.0",
    )
    monkeypatch.delenv("BGOS_AUTO_UPDATE", raising=False)
    assert self_update.update_readiness() == {
        "supervised": "systemd",
        "autoUpdateEnabled": True,
        "rollbackLatched": False,
        "pendingRestartVersion": "0.29.0",
    }


def test_update_readiness_unsupervised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "systemd_user_unit", lambda: None)
    monkeypatch.setattr(
        self_update, "pending_restart_version", lambda clone_dir=None: None,
    )
    monkeypatch.setenv("BGOS_AUTO_UPDATE", "0")
    assert self_update.update_readiness() == {
        "supervised": "none",
        "autoUpdateEnabled": False,
        "rollbackLatched": False,
        "pendingRestartVersion": None,
    }


def test_detect_attachment_mode(tmp_path: Path) -> None:
    assert (
        self_update.detect_attachment_mode(tmp_path)
        == "fork-patch-or-unknown"
    )
    (tmp_path / "plugins" / "bgos").mkdir(parents=True)
    assert self_update.detect_attachment_mode(tmp_path) == "plugin"


# -----------------------------------------------------------------------------
# apply_update against real throwaway git repos
# -----------------------------------------------------------------------------


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_version(repo: Path, version: str) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "hermes-channel-bgos"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def _commit_all(repo: Path, message: str) -> None:
    _run_git(repo, "add", "-A")
    _run_git(
        repo, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-m", message,
    )


@pytest.fixture
def cloned_repos(tmp_path: Path) -> tuple[Path, Path]:
    """(origin, clone) pair: origin holds v0.28.0 on main, clone tracks it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run_git(tmp_path, "init", "-b", "main", str(origin))
    _write_version(origin, "0.28.0")
    _commit_all(origin, "v0.28.0")
    clone = tmp_path / "clone"
    _run_git(tmp_path, "clone", str(origin), str(clone))
    return origin, clone


def test_apply_update_fast_forwards_and_reports_versions(
    cloned_repos: tuple[Path, Path],
) -> None:
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all(origin, "v0.28.1")

    applied = self_update.apply_update(clone)

    assert applied == AppliedUpdate("0.28.0", "0.28.1")
    assert self_update.parse_pyproject_version(
        (clone / "pyproject.toml").read_text()
    ) == "0.28.1"


def test_apply_update_refuses_dirty_tree(
    cloned_repos: tuple[Path, Path],
) -> None:
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all(origin, "v0.28.1")
    (clone / "local-note.txt").write_text("operator edit", encoding="utf-8")

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(clone)
    assert excinfo.value.reason == "dirty_tree"
    # The brake never touches local state.
    assert (clone / "local-note.txt").exists()


def test_apply_update_no_update_available(
    cloned_repos: tuple[Path, Path],
) -> None:
    _origin, clone = cloned_repos
    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(clone)
    assert excinfo.value.reason == "no_update_available"


def test_apply_update_refuses_major_jump(
    cloned_repos: tuple[Path, Path],
) -> None:
    origin, clone = cloned_repos
    _write_version(origin, "1.0.0")
    _commit_all(origin, "v1.0.0")

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(clone)
    assert excinfo.value.reason == "major_jump"
    # Nothing merged.
    assert self_update.parse_pyproject_version(
        (clone / "pyproject.toml").read_text()
    ) == "0.28.0"


def test_apply_update_same_version_commit_is_no_update(
    cloned_repos: tuple[Path, Path],
) -> None:
    origin, clone = cloned_repos
    (origin / "README.md").write_text("docs only", encoding="utf-8")
    _commit_all(origin, "docs")

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(clone)
    assert excinfo.value.reason == "no_update_available"


def test_apply_update_fetch_failure(
    cloned_repos: tuple[Path, Path], tmp_path: Path,
) -> None:
    _origin, clone = cloned_repos
    _run_git(
        clone, "remote", "set-url", "origin",
        str(tmp_path / "gone-missing"),
    )
    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(clone)
    assert excinfo.value.reason == "fetch_failed"


def test_apply_update_rejects_non_checkout(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(plain)
    assert excinfo.value.reason == "not_a_git_checkout"


def test_apply_update_rejects_missing_clone_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "clone_root", lambda: None)
    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update()
    assert excinfo.value.reason == "not_a_git_checkout"


def test_clone_root_resolves_this_editable_checkout() -> None:
    root = self_update.clone_root()
    assert root is not None
    assert (root / "pyproject.toml").exists()
    assert root == Path(__file__).resolve().parents[1]


# -----------------------------------------------------------------------------
# Detached restart spawn
# -----------------------------------------------------------------------------


def test_schedule_unit_restart_spawns_detached_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        assert kwargs["start_new_session"] is True
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)
    assert self_update.schedule_unit_restart("hermes-gateway.service") is True
    assert spawned == [[
        "systemd-run", "--user", "--on-active=2s",
        "systemctl", "--user", "restart", "hermes-gateway.service",
    ]]


def test_schedule_unit_restart_spawn_failure_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(argv, **kwargs):
        raise OSError("no systemd-run")

    monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)
    assert self_update.schedule_unit_restart("hermes-gateway.service") is False
