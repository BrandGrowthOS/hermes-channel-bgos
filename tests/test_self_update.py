"""Tests for the self_update module (one-click update, wire contract v1).

Covers the same-major-newer-only decision, the pyproject version parser,
the daily-cached latest-version check, the systemd relaunch-authority
probe, readiness assembly, and apply_update against real throwaway git
repos (the brake paths are the security surface: dirty tree, non-clone
layouts, fetch failures, major jumps).
"""
from __future__ import annotations

import os
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
    monkeypatch.setattr(self_update, "_owns_systemd_unit", lambda unit: unit == "hermes-gateway-ava.service")

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


def test_systemd_unit_must_own_this_process(monkeypatch):
    _fresh_unit_cache(monkeypatch)
    monkeypatch.setattr(self_update.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="● someone-else.service - Other gateway\n",
    ))
    monkeypatch.setattr(self_update, "_owns_systemd_unit", lambda unit: False)
    assert self_update.systemd_user_unit() is None


@pytest.mark.parametrize("group,expected", [
    ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes.service", True),
    ("0::/user.slice/app.slice/another.service", False),
    ("0::/user.slice/hermes.service/session.scope", False),
])
def test_systemd_cgroup_ownership(monkeypatch, group, expected):
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: group)
    assert self_update._owns_systemd_unit("hermes.service") is expected


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
def cloned_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(origin, clone) pair: origin holds v0.28.0 on main, clone tracks it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run_git(tmp_path, "init", "-b", "main", str(origin))
    _write_version(origin, "0.28.0")
    _commit_all(origin, "v0.28.0")
    clone = tmp_path / "clone"
    _run_git(tmp_path, "clone", str(origin), str(clone))
    # A local throwaway remote substitutes for GitHub; source validation is
    # tested separately without permitting external sources in production.
    monkeypatch.setattr(self_update, "_is_official_remote", lambda url: True)
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


def test_apply_update_refuses_unofficial_origin(cloned_repos, monkeypatch):
    from hermes_channel_bgos.update_cli import _is_official_remote
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all(origin, "candidate")
    before = _run_git(clone, "rev-parse", "HEAD")
    monkeypatch.setattr(self_update, "_is_official_remote", _is_official_remote)
    with pytest.raises(SelfUpdateError, match="untrusted_update_source"):
        self_update.apply_update(clone)
    assert _run_git(clone, "rev-parse", "HEAD") == before


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


# Soak (scheduled apply only): the fetched origin/main commit must have been
# there for 24 hours, so a bad release can be pulled before every supervised
# host takes it unattended. The clock is injected; the age is the git
# committer time of the fetched target commit.

DAY = 24 * 60 * 60
COMMITTED_AT = 1_790_000_000


def _commit_all_at(
    repo: Path, message: str, epoch: int, *, authored_at: int | None = None,
) -> None:
    _run_git(repo, "add", "-A")
    stamp = f"@{epoch} +0000"
    authored = f"@{authored_at} +0000" if authored_at is not None else stamp
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", message,
        ],
        capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": authored},
    )


def test_apply_update_soak_refuses_a_target_younger_than_a_day(cloned_repos) -> None:
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all_at(origin, "v0.28.1", COMMITTED_AT)
    before = _run_git(clone, "rev-parse", "HEAD")

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(
            clone, soak_seconds=DAY, now=lambda: COMMITTED_AT + 3600,
        )
    assert excinfo.value.reason == "soak"
    assert excinfo.value.retry_after == DAY - 3600
    # Nothing merged: the clone still runs (and stages) nothing new.
    assert _run_git(clone, "rev-parse", "HEAD") == before


def test_apply_update_soak_passes_once_the_target_is_a_day_old(cloned_repos) -> None:
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all_at(origin, "v0.28.1", COMMITTED_AT)

    applied = self_update.apply_update(
        clone, soak_seconds=DAY, now=lambda: COMMITTED_AT + DAY,
    )
    assert applied == AppliedUpdate("0.28.0", "0.28.1")


def test_apply_update_soak_reads_the_fetched_target_commit(cloned_repos) -> None:
    """The age is the NEWEST commit fetched (what the merge would take), not
    the commit that bumped the version: a later change restarts the soak."""
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all_at(origin, "v0.28.1", COMMITTED_AT - 2 * DAY)
    (origin / "fix.txt").write_text("late change", encoding="utf-8")
    _commit_all_at(origin, "late change", COMMITTED_AT)

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(
            clone, soak_seconds=DAY, now=lambda: COMMITTED_AT + 60,
        )
    assert excinfo.value.reason == "soak"


def test_apply_update_soak_reads_the_committer_time_not_the_author_time(
    cloned_repos,
) -> None:
    """A change written days ago but landed on main (rebased, cherry-picked)
    just now has only just been published: the age is its committer time."""
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all_at(origin, "v0.28.1", COMMITTED_AT, authored_at=COMMITTED_AT - 2 * DAY)

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(
            clone, soak_seconds=DAY, now=lambda: COMMITTED_AT + 60,
        )
    assert excinfo.value.reason == "soak"


def test_apply_update_without_a_soak_takes_a_fresh_commit(cloned_repos) -> None:
    """update_now passes no soak: a person asked for it now."""
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all_at(origin, "v0.28.1", COMMITTED_AT)
    applied = self_update.apply_update(clone, now=lambda: COMMITTED_AT)
    assert applied.after_version == "0.28.1"


# A pin or a rollback (update_cli: `git checkout --detach <commit>`) is an
# operator holding this clone where it is. apply_update only ever moves the
# main branch: a detached HEAD, or any other branch, is refused as `pinned`
# and left exactly where it is, on every path (finding H1).


def _head_is_detached(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 1


@pytest.mark.parametrize("soak", [DAY, None], ids=["scheduled", "update_now"])
def test_apply_update_refuses_a_pinned_detached_head(cloned_repos, soak) -> None:
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all_at(origin, "v0.28.1", COMMITTED_AT)
    _run_git(clone, "checkout", "--detach", "HEAD")
    before = _run_git(clone, "rev-parse", "HEAD")

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(
            clone, soak_seconds=soak, now=lambda: COMMITTED_AT + 2 * DAY,
        )
    assert excinfo.value.reason == "pinned"
    assert _run_git(clone, "rev-parse", "HEAD") == before
    assert _head_is_detached(clone)


def test_apply_update_refuses_a_branch_other_than_main(cloned_repos) -> None:
    """A developer's branch is not ours to fast-forward onto main."""
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all(origin, "v0.28.1")
    _run_git(clone, "checkout", "-b", "local-work")
    before = _run_git(clone, "rev-parse", "HEAD")

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(clone)
    assert excinfo.value.reason == "pinned"
    assert _run_git(clone, "rev-parse", "HEAD") == before


def test_apply_update_reports_an_unreadable_repository_not_a_pin(
    cloned_repos, monkeypatch,
) -> None:
    """Only git's quiet "not a symbolic ref" (exit 1, a detached HEAD) is a
    pin. A repository git cannot read (exit 128) is a failure the app must
    see, as the status read reported it before the pin check existed: read
    as `pinned`, the scheduled apply would wait on it quietly and withdraw
    a failure it reported before."""
    _origin, clone = cloned_repos
    # Git must not climb out of the broken clone to a repository above it.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(clone.parent))
    (clone / ".git" / "HEAD").write_text("not a ref\n", encoding="utf-8")

    with pytest.raises(SelfUpdateError) as excinfo:
        self_update.apply_update(
            clone, soak_seconds=DAY, now=lambda: COMMITTED_AT + 2 * DAY,
        )
    assert excinfo.value.reason == "git_status_failed"


def test_clone_pinned_reads_the_head_apply_update_refuses(cloned_repos) -> None:
    """The scheduled plan's cheap probe (findings F1 and F2): the same
    branch test as apply_update's `pinned`, without a fetch, so a held
    clone is known before a run is planned."""
    _origin, clone = cloned_repos
    assert self_update.clone_pinned(clone) is False
    _run_git(clone, "checkout", "--detach", "HEAD")
    assert self_update.clone_pinned(clone) is True
    _run_git(clone, "checkout", "-b", "local-work")
    assert self_update.clone_pinned(clone) is True
    _run_git(clone, "checkout", "main")
    assert self_update.clone_pinned(clone) is False


def test_clone_pinned_is_false_when_git_cannot_say(
    cloned_repos, monkeypatch, tmp_path: Path,
) -> None:
    """No clone, or one git cannot read, is a failure apply_update reports,
    never a pin that would withdraw it."""
    _origin, clone = cloned_repos
    plain = tmp_path / "plain"
    plain.mkdir()
    assert self_update.clone_pinned(plain) is False
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(clone.parent))
    (clone / ".git" / "HEAD").write_text("not a ref\n", encoding="utf-8")
    assert self_update.clone_pinned(clone) is False


def test_apply_update_takes_updates_again_once_back_on_main(cloned_repos) -> None:
    """Undoing the pin is `git checkout main` (or re-running install.sh)."""
    origin, clone = cloned_repos
    _write_version(origin, "0.28.1")
    _commit_all(origin, "v0.28.1")
    _run_git(clone, "checkout", "--detach", "HEAD")
    _run_git(clone, "checkout", "main")

    assert self_update.apply_update(clone) == AppliedUpdate("0.28.0", "0.28.1")


@pytest.mark.parametrize(
    ("committed_at", "now", "expected"),
    [
        (1_000, 1_000, DAY),
        (1_000, 1_000 + DAY - 1, 1),
        (1_000, 1_000 + DAY, 0),
        (1_000, 1_000 + 2 * DAY, 0),
        # A committer clock ahead of ours waits until a day past its stamp.
        (1_000 + 600, 1_000, DAY + 600),
    ],
)
def test_soak_remaining(committed_at, now, expected) -> None:
    assert self_update.soak_remaining(committed_at, now, DAY) == expected


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

    def fake_run(argv, **kwargs):
        spawned.append(argv)
        assert kwargs["start_new_session"] is True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    assert self_update.schedule_unit_restart("hermes-gateway.service") is True
    assert spawned == [[
        "systemd-run", "--user", "--on-active=2s",
        "systemctl", "--user", "restart", "hermes-gateway.service",
    ]]


def test_schedule_unit_restart_spawn_failure_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):
        raise OSError("no systemd-run")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    assert self_update.schedule_unit_restart("hermes-gateway.service") is False


def test_schedule_unit_restart_rejected_timer_is_false(monkeypatch):
    monkeypatch.setattr(self_update.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    assert self_update.schedule_unit_restart("hermes-gateway.service") is False


# -----------------------------------------------------------------------------
# launchd relaunch-authority probe (design 2.3: on macOS the systemd probe
# always answers none, so without this every update stages forever)
# -----------------------------------------------------------------------------


def _launchctl_print(pid: int, label: str = "ai.hermes.gateway") -> str:
    """Shape of a real `launchctl print gui/<uid>/<label>` body (tabs)."""
    return (
        f"gui/501/{label} = {{\n"
        "\tactive count = 1\n"
        f"\tpath = /Users/kc/Library/LaunchAgents/{label}.plist\n"
        "\ttype = LaunchAgent\n"
        "\tstate = running\n"
        "\n"
        "\tprogram = /Users/kc/.hermes/hermes-agent/venv/bin/python\n"
        f"\tpid = {pid}\n"
        "\timmediate reason = inefficient\n"
        "}\n"
    )


class _FakeLaunchctl:
    """Recording stand-in for subprocess.run: answers `launchctl print` per
    service target from a table, everything else is 'Could not find
    service' (exit 113, what launchctl prints for an unknown label)."""

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs.get("timeout"), "a probe must never hang the heartbeat"
        body = self.bodies.get(argv[-1])
        if argv[:2] != ["launchctl", "print"] or body is None:
            return SimpleNamespace(returncode=113, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=body, stderr="")


def test_launchd_probe_finds_the_job_that_owns_this_pid() -> None:
    fake = _FakeLaunchctl({"gui/501/ai.hermes.gateway": _launchctl_print(4242)})
    target = self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, hermes_home=None, run=fake,
    )
    assert target == "gui/501/ai.hermes.gateway"
    assert fake.calls[0] == ["launchctl", "print", "gui/501/ai.hermes.gateway"]


def test_launchd_probe_refuses_a_job_running_another_pid() -> None:
    """A loaded ai.hermes.gateway that is NOT this process (a second Hermes)
    is no relaunch authority: kickstart -k would kill the wrong process and
    leave this one running."""
    fake = _FakeLaunchctl({"gui/501/ai.hermes.gateway": _launchctl_print(999)})
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, ppid=1, hermes_home=None, run=fake,
    ) is None


# Hermes upstream since 2026-08-15 (1db9273584) writes ProgramArguments
# `python -m hermes_cli.stderr_timestamp --error-log ... -- <gateway run>`:
# the wrapper Popen()s the gateway as its child and never execs, so launchd's
# job pid is the WRAPPER's, this process's parent (finding H2). That wrapper
# forwards SIGTERM to its child and launchd ends the job's process group, so
# kickstart -k on the job restarts this gateway like a direct one.

WRAPPER_ARGV = (
    "/Users/kc/.hermes/hermes-agent/venv/bin/python -m hermes_cli.stderr_timestamp"
    " --error-log /Users/kc/.hermes/logs/gateway.error.log --"
    " /Users/kc/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main"
    " gateway run --external-supervisor"
)


class _FakeLaunchctlAndPs(_FakeLaunchctl):
    """Also answers `ps -o command= -p <pid>` from a pid -> argv table."""

    def __init__(self, bodies: dict[str, str], commands: dict[int, str]) -> None:
        super().__init__(bodies)
        self.commands = commands

    def __call__(self, argv, **kwargs):
        if argv[0] == "ps":
            self.calls.append(list(argv))
            assert kwargs.get("timeout"), "a probe must never hang the heartbeat"
            command = self.commands.get(int(argv[-1]))
            if command is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=command + "\n", stderr="")
        return super().__call__(argv, **kwargs)


def test_launchd_probe_accepts_hermes_stderr_wrapper_as_the_job() -> None:
    fake = _FakeLaunchctlAndPs(
        {"gui/501/ai.hermes.gateway": _launchctl_print(4200)},
        {4200: WRAPPER_ARGV},
    )
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, ppid=4200, hermes_home=None,
        run=fake,
    ) == "gui/501/ai.hermes.gateway"
    assert ["ps", "-ww", "-o", "command=", "-p", "4200"] in fake.calls


def test_launchd_probe_refuses_a_parent_that_is_not_the_hermes_wrapper() -> None:
    """A job whose pid is our parent but runs something else (a shell
    script that did not exec) is no authority: nothing says it forwards
    the stop to this process."""
    fake = _FakeLaunchctlAndPs(
        {"gui/501/ai.hermes.gateway": _launchctl_print(4200)},
        {4200: "/bin/bash /Users/kc/bin/run-hermes.sh"},
    )
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, ppid=4200, hermes_home=None,
        run=fake,
    ) is None


def test_launchd_probe_refuses_the_wrapper_when_ps_cannot_answer() -> None:
    fake = _FakeLaunchctlAndPs(
        {"gui/501/ai.hermes.gateway": _launchctl_print(4200)}, {},
    )
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, ppid=4200, hermes_home=None,
        run=fake,
    ) is None


def test_launchd_probe_refuses_a_wrapper_that_is_not_our_parent() -> None:
    """The wrapper of ANOTHER gateway (same label, a second Hermes) is not
    this process's job, whatever its argv says."""
    fake = _FakeLaunchctlAndPs(
        {"gui/501/ai.hermes.gateway": _launchctl_print(5000)},
        {5000: WRAPPER_ARGV},
    )
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, ppid=4200, hermes_home=None,
        run=fake,
    ) is None
    assert not any(call[0] == "ps" for call in fake.calls)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (WRAPPER_ARGV, True),
        ("python3.11 -m hermes_cli.stderr_timestamp --error-log x -- y", True),
        ("python -m hermes_cli.main gateway run", False),
        ("python -m hermes_cli.stderr_timestamp_evil --error-log x", False),
        ("/bin/sh -c echo -m hermes_cli.stderr_timestampx", False),
        ("", False),
    ],
)
def test_is_hermes_stderr_wrapper(command, expected) -> None:
    assert self_update._is_hermes_stderr_wrapper(command) is expected


def test_launchd_probe_tries_the_profile_label() -> None:
    """Hermes names a named profile's agent ai.hermes.gateway-<profile>;
    the profile is the HERMES_HOME leaf under profiles/ (topology.profile_dir
    mirrors hermes_cli.profiles.get_profile_dir)."""
    fake = _FakeLaunchctl({
        "gui/501/ai.hermes.gateway-ava": _launchctl_print(77, "ai.hermes.gateway-ava"),
    })
    target = self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=77,
        hermes_home="/Users/kc/.hermes/profiles/ava", run=fake,
    )
    assert target == "gui/501/ai.hermes.gateway-ava"
    assert [c[-1] for c in fake.calls] == [
        "gui/501/ai.hermes.gateway", "user/501/ai.hermes.gateway",
        "gui/501/ai.hermes.gateway-ava",
    ]


def test_launchd_probe_skips_an_unsafe_profile_name() -> None:
    fake = _FakeLaunchctl({})
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=1,
        hermes_home="/x/profiles/a b;rm -rf", run=fake,
    ) is None
    assert [c[-1] for c in fake.calls] == [
        "gui/501/ai.hermes.gateway", "user/501/ai.hermes.gateway",
    ]


# Hermes upstream (hermes_cli/gateway.py _launchd_domain) loads the gateway
# in gui/<uid> for an Aqua login and in user/<uid> for a Background or SSH
# session. The probe asks both, gui first, and keeps the one whose job IS
# this process; the restart then kickstarts exactly that target.


def test_launchd_probe_finds_the_job_in_the_user_domain() -> None:
    fake = _FakeLaunchctl({"user/501/ai.hermes.gateway": _launchctl_print(4242)})
    target = self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, hermes_home=None, run=fake,
    )
    assert target == "user/501/ai.hermes.gateway"
    assert [c[-1] for c in fake.calls] == [
        "gui/501/ai.hermes.gateway", "user/501/ai.hermes.gateway",
    ]


def test_launchd_probe_keeps_the_domain_whose_job_is_this_pid() -> None:
    """The same label loaded in both domains: the gui job runs ANOTHER
    gateway, the user job runs this one. Kickstarting the gui target would
    kill the wrong process."""
    fake = _FakeLaunchctl({
        "gui/501/ai.hermes.gateway": _launchctl_print(999),
        "user/501/ai.hermes.gateway": _launchctl_print(4242),
    })
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, hermes_home=None, run=fake,
    ) == "user/501/ai.hermes.gateway"


def test_launchd_probe_asks_gui_first_and_stops_there() -> None:
    fake = _FakeLaunchctl({
        "gui/501/ai.hermes.gateway": _launchctl_print(4242),
        "user/501/ai.hermes.gateway": _launchctl_print(4242),
    })
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, hermes_home=None, run=fake,
    ) == "gui/501/ai.hermes.gateway"
    assert [c[-1] for c in fake.calls] == ["gui/501/ai.hermes.gateway"]


def test_launchd_probe_finds_a_profile_job_in_the_user_domain() -> None:
    fake = _FakeLaunchctl({
        "user/501/ai.hermes.gateway-ava": _launchctl_print(77, "ai.hermes.gateway-ava"),
    })
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=77,
        hermes_home="/Users/kc/.hermes/profiles/ava", run=fake,
    ) == "user/501/ai.hermes.gateway-ava"


def test_launchd_probe_refuses_when_neither_domain_runs_this_pid() -> None:
    fake = _FakeLaunchctl({
        "gui/501/ai.hermes.gateway": _launchctl_print(1),
        "user/501/ai.hermes.gateway": _launchctl_print(2),
    })
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=4242, hermes_home=None, run=fake,
    ) is None


def test_schedule_launchd_restart_kickstarts_a_user_domain_target() -> None:
    spawned: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        spawned.append(list(argv))
        return SimpleNamespace(pid=1)

    assert self_update.schedule_launchd_restart(
        "user/501/ai.hermes.gateway-ava", popen=fake_popen,
    ) is True
    assert spawned[0][-1] == "user/501/ai.hermes.gateway-ava"


def test_launchd_probe_is_macos_only() -> None:
    fake = _FakeLaunchctl({"gui/501/ai.hermes.gateway": _launchctl_print(5)})
    assert self_update._probe_launchd_job(
        platform="linux", uid=501, pid=5, hermes_home=None, run=fake,
    ) is None
    assert fake.calls == []


def test_launchd_probe_spawn_error_is_none() -> None:
    def boom(argv, **kwargs):
        raise OSError("no launchctl")

    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=5, hermes_home=None, run=boom,
    ) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("\tstate = running\n\tpid = 4242\n", 4242),
        ("\tstate = not running\n", None),
        # The job's own pid line comes first; a later nested pid never wins.
        ("\tpid = 10\n\t\tpid = 4242\n", 10),
        ("\tpid = abc\n", None),
        ("", None),
    ],
)
def test_launchd_print_pid(body, expected) -> None:
    assert self_update._launchd_print_pid(body) == expected


def test_launchd_service_target_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        self_update, "_launchd_result", self_update._UNIT_UNRESOLVED,
    )
    calls: list[int] = []

    def probe(**kwargs):
        calls.append(1)
        return "gui/501/ai.hermes.gateway"

    monkeypatch.setattr(self_update, "_probe_launchd_job", probe)
    assert self_update.launchd_service_target() == "gui/501/ai.hermes.gateway"
    assert self_update.launchd_service_target() == "gui/501/ai.hermes.gateway"
    assert calls == [1]


def test_verified_supervisor_prefers_systemd(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "systemd_user_unit", lambda: "hermes-gateway.service")
    monkeypatch.setattr(
        self_update, "launchd_service_target", lambda: "gui/501/ai.hermes.gateway",
    )
    assert self_update.verified_supervisor() == self_update.Supervisor(
        "systemd", "hermes-gateway.service",
    )


def test_verified_supervisor_falls_back_to_launchd(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "systemd_user_unit", lambda: None)
    monkeypatch.setattr(
        self_update, "launchd_service_target", lambda: "gui/501/ai.hermes.gateway",
    )
    assert self_update.verified_supervisor() == self_update.Supervisor(
        "launchd", "gui/501/ai.hermes.gateway",
    )


def test_verified_supervisor_none(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "systemd_user_unit", lambda: None)
    monkeypatch.setattr(self_update, "launchd_service_target", lambda: None)
    assert self_update.verified_supervisor() is None


def test_update_readiness_reports_launchd(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "systemd_user_unit", lambda: None)
    monkeypatch.setattr(
        self_update, "launchd_service_target", lambda: "gui/501/ai.hermes.gateway",
    )
    monkeypatch.setattr(
        self_update, "pending_restart_version", lambda clone_dir=None: None,
    )
    monkeypatch.delenv("BGOS_AUTO_UPDATE", raising=False)
    assert self_update.update_readiness()["supervised"] == "launchd"


# -----------------------------------------------------------------------------
# Detached launchd restart (fact: this Mac's gateway plist has KeepAlive
# {SuccessfulExit:false}, so a clean exit is NOT relaunched; the restart must
# be `launchctl kickstart -k`, never a plain exit)
# -----------------------------------------------------------------------------


def test_schedule_launchd_restart_spawns_a_detached_delayed_kickstart() -> None:
    spawned: list[tuple[list[str], dict]] = []

    def fake_popen(argv, **kwargs):
        spawned.append((list(argv), kwargs))
        return SimpleNamespace(pid=1)

    assert self_update.schedule_launchd_restart(
        "gui/501/ai.hermes.gateway", popen=fake_popen,
    ) is True
    [(argv, kwargs)] = spawned
    assert argv == [
        "/bin/sh", "-c", 'sleep 2; exec launchctl kickstart -k "$1"',
        "bgos-gateway-restart", "gui/501/ai.hermes.gateway",
    ]
    # Its own session: launchd's teardown of OUR process group must not
    # take the pending kickstart down with it.
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_schedule_launchd_restart_refuses_a_foreign_target() -> None:
    spawned: list[list[str]] = []
    for target in (
        "gui/501/com.apple.Finder",
        "user/501/com.apple.Finder",
        "system/ai.hermes.gateway",
        "pid/4242/ai.hermes.gateway",
        "gui/501/ai.hermes.gateway; rm -rf ~",
    ):
        assert self_update.schedule_launchd_restart(
            target, popen=lambda argv, **kw: spawned.append(argv),
        ) is False
    assert spawned == []


def test_schedule_launchd_restart_spawn_failure_is_false() -> None:
    def boom(argv, **kwargs):
        raise OSError("no /bin/sh")

    assert self_update.schedule_launchd_restart(
        "gui/501/ai.hermes.gateway", popen=boom,
    ) is False


def test_schedule_supervisor_restart_dispatches_by_kind(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        self_update, "schedule_unit_restart",
        lambda unit: seen.append(("systemd", unit)) or True,
    )
    monkeypatch.setattr(
        self_update, "schedule_launchd_restart",
        lambda target: seen.append(("launchd", target)) or True,
    )
    assert self_update.schedule_supervisor_restart(
        self_update.Supervisor("systemd", "hermes-gateway.service"),
    )
    assert self_update.schedule_supervisor_restart(
        self_update.Supervisor("launchd", "gui/501/ai.hermes.gateway"),
    )
    assert self_update.schedule_supervisor_restart(
        self_update.Supervisor("pm2", "x"),
    ) is False
    assert seen == [
        ("systemd", "hermes-gateway.service"),
        ("launchd", "gui/501/ai.hermes.gateway"),
    ]


def test_schedule_launchd_restart_refuses_a_trailing_newline() -> None:
    spawned: list[list[str]] = []
    assert self_update.schedule_launchd_restart(
        "gui/501/ai.hermes.gateway\n", popen=lambda argv, **kw: spawned.append(argv),
    ) is False
    assert spawned == []


def test_launchd_probe_reads_the_process_hermes_home(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", "/Users/kc/.hermes/profiles/ava")
    fake = _FakeLaunchctl({
        "gui/501/ai.hermes.gateway-ava": _launchctl_print(77, "ai.hermes.gateway-ava"),
    })
    assert self_update._probe_launchd_job(
        platform="darwin", uid=501, pid=77, run=fake,
    ) == "gui/501/ai.hermes.gateway-ava"
