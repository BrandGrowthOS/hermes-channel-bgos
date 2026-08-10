"""Tests for the official Hermes BGOS self-update command."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from hermes_channel_bgos import update_cli
from hermes_channel_bgos.update_cli import (
    InstallInfo,
    InstallKind,
    ReconResult,
    Target,
    UpdatePlan,
    detect_install_kind,
    frozen_requirement,
    render_plan,
    resolve_target,
    rollback_command,
)


def _checkout_layout(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hermes-channel-bgos"\nversion = "0.26.0"\n'
    )
    package_file = root / "src/hermes_channel_bgos/__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text('__version__ = "0.26.0"\n')
    return package_file


# ---------------------------------------------------------------------------
# Install classification
# ---------------------------------------------------------------------------


def test_install_kind_detects_editable_checkout(tmp_path: Path) -> None:
    package_file = _checkout_layout(tmp_path / "repo")
    assert detect_install_kind(package_file) is InstallKind.CHECKOUT


@pytest.mark.parametrize("directory", ["site-packages", "dist-packages"])
def test_install_kind_detects_site_install(
    tmp_path: Path,
    directory: str,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "hermes-channel-bgos"\n'
    )
    package_file = repo / ".venv/lib/python3.12" / directory
    package_file = package_file / "hermes_channel_bgos/__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("")

    assert detect_install_kind(package_file) is InstallKind.PIP


def test_install_kind_supports_git_worktree_file(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / ".git").write_text("gitdir: ../main/.git/worktrees/test\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hermes-channel-bgos"\n'
    )
    package_file = root / "src/hermes_channel_bgos/__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("")

    assert detect_install_kind(package_file) is InstallKind.CHECKOUT


def test_install_kind_rejects_unknown_layout(tmp_path: Path) -> None:
    package_file = tmp_path / "hermes_channel_bgos/__init__.py"
    package_file.parent.mkdir()
    package_file.write_text("")

    with pytest.raises(update_cli.UpdateError, match="unsupported install"):
        detect_install_kind(package_file)


def test_locate_install_preserves_venv_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = "/tmp/hermes-venv/bin/python"
    monkeypatch.setattr(update_cli.sys, "executable", launcher)

    assert update_cli.locate_install().python == Path(launcher)


# ---------------------------------------------------------------------------
# Pure target, snapshot, and plan helpers
# ---------------------------------------------------------------------------


def test_target_resolution_prefers_pin_then_tag_then_main() -> None:
    assert resolve_target("abc1234", "v0.26.0").ref == "abc1234"
    assert resolve_target("abc1234", "v0.26.0").source == "explicit pin"
    assert resolve_target(None, "v0.26.0").ref == "v0.26.0"
    assert resolve_target(None, "v0.26.0").source == "release tag"
    assert resolve_target(None, None).ref == "origin/main"
    assert resolve_target(None, None).source == "main"


def test_stale_release_tag_does_not_qualify_for_site_update() -> None:
    assert not update_cli._tag_is_not_older("v0.17.0", "0.25.1")
    assert update_cli._tag_is_not_older("v0.26.0", "0.25.1")


def test_release_tag_must_match_project_version() -> None:
    assert update_cli._release_is_eligible("v0.26.0", "0.26.0", "0.25.1")
    assert not update_cli._release_is_eligible("v0.26.0", "0.17.0", "0.16.0")


def test_checkout_rollback_uses_recorded_sha_and_quotes_path() -> None:
    command = rollback_command(
        InstallKind.CHECKOUT,
        python=Path("/unused/python"),
        checkout_root=Path("/tmp/Hermes Build"),
        current_commit="a" * 40,
    )
    assert command == (
        "git -C '/tmp/Hermes Build' checkout --detach " + "a" * 40
    )


def test_pip_rollback_preserves_frozen_requirement() -> None:
    requirement = (
        "hermes-channel-bgos @ "
        "git+https://github.com/BrandGrowthOS/hermes-channel-bgos.git@abc1234"
    )
    command = rollback_command(
        InstallKind.PIP,
        python=Path("/tmp/Hermes Python/bin/python"),
        requirement=requirement,
    )
    assert "'/tmp/Hermes Python/bin/python' -m pip install --force-reinstall" in command
    assert f"'{requirement}'" in command


def test_frozen_requirement_preserves_editable_vcs_line() -> None:
    requirement = (
        "-e git+https://github.com/BrandGrowthOS/hermes-channel-bgos.git"
        "@abc1234#egg=hermes_channel_bgos"
    )
    assert frozen_requirement(f"click==8.3.0\n{requirement}\n") == requirement


def test_render_plan_includes_apply_rollback_shortlog_and_restart() -> None:
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=Path("/tmp/repo/src/hermes_channel_bgos/__init__.py"),
        checkout_root=Path("/tmp/repo"),
        python=Path("/tmp/repo/.venv/bin/python"),
    )
    recon = ReconResult(
        current_commit="a" * 40,
        main_commit="b" * 40,
        main_version="0.26.0",
        latest_release_tag="v0.26.0",
        latest_release_commit="b" * 40,
        latest_release_version="0.26.0",
        target=Target("origin/main", "b" * 40, "0.26.0", "main"),
        incoming=("b123456 feat: official updater",),
        already_current=False,
        dirty=False,
    )
    plan = UpdatePlan(
        install=install,
        current_version="0.25.1",
        recon=recon,
        rollback="git -C /tmp/repo checkout --detach " + "a" * 40,
        apply_argv=(
            "git", "-C", "/tmp/repo", "pull", "--ff-only", "origin", "b" * 40,
        ),
        restart_command="systemctl --user restart hermes-gateway.service",
    )

    rendered = render_plan(plan)

    assert "Target: origin/main (main)" in rendered
    assert "b123456 feat: official updater" in rendered
    assert "pull --ff-only origin " + "b" * 40 in rendered
    assert "checkout --detach" in rendered
    assert "systemctl --user restart hermes-gateway.service" in rendered


def test_render_plan_shows_empty_shortlog() -> None:
    install = InstallInfo(
        kind=InstallKind.PIP,
        package_file=Path("/venv/site-packages/hermes_channel_bgos/__init__.py"),
        python=Path("/venv/bin/python"),
    )
    recon = ReconResult(
        current_commit=None,
        main_commit="a" * 40,
        main_version="0.26.0",
        latest_release_tag=None,
        latest_release_commit=None,
        latest_release_version=None,
        target=Target("origin/main", "a" * 40, "0.26.0", "main"),
        incoming=(),
        already_current=True,
        dirty=False,
    )
    plan = UpdatePlan(
        install=install,
        current_version="0.26.0",
        recon=recon,
        rollback="rollback",
        apply_argv=("python", "-m", "pip", "install"),
        restart_command=None,
    )

    assert "Incoming commits:\n    (none)" in render_plan(plan)


def test_checkout_apply_pulls_the_resolved_commit() -> None:
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=Path("/repo/src/hermes_channel_bgos/__init__.py"),
        python=Path("/repo/.venv/bin/python"),
        checkout_root=Path("/repo"),
    )
    target_commit = "b" * 40
    recon = ReconResult(
        current_commit="a" * 40,
        main_commit=target_commit,
        main_version="0.26.0",
        latest_release_tag=None,
        latest_release_commit=None,
        latest_release_version=None,
        target=Target("origin/main", target_commit, "0.26.0", "main"),
        incoming=(),
        already_current=False,
        dirty=False,
    )

    argv = update_cli._apply_argv(install, recon, pin=None)

    assert argv[-3:] == ("--ff-only", "origin", target_commit)
    assert argv == (
        "git", "-C", "/repo", "pull", "--ff-only", "origin", target_commit,
    )


def test_checkout_pin_detaches_at_resolved_commit() -> None:
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=Path("/repo/src/hermes_channel_bgos/__init__.py"),
        python=Path("/repo/.venv/bin/python"),
        checkout_root=Path("/repo"),
    )
    target_commit = "b" * 40
    recon = ReconResult(
        current_commit="a" * 40,
        main_commit=target_commit,
        main_version="0.26.0",
        latest_release_tag="v0.26.0",
        latest_release_commit=target_commit,
        latest_release_version="0.26.0",
        target=Target("v0.26.0", target_commit, "0.26.0", "explicit pin"),
        incoming=(),
        already_current=False,
        dirty=False,
    )

    assert update_cli._apply_argv(install, recon, pin="v0.26.0") == (
        "git", "-C", "/repo", "checkout", "--detach", target_commit,
    )


def test_pip_apply_uses_official_repo_at_resolved_commit() -> None:
    install = InstallInfo(
        kind=InstallKind.PIP,
        package_file=Path("/venv/site-packages/hermes_channel_bgos/__init__.py"),
        python=Path("/venv/bin/python"),
    )
    target_commit = "b" * 40
    recon = ReconResult(
        current_commit=None,
        main_commit=target_commit,
        main_version="0.26.0",
        latest_release_tag="v0.26.0",
        latest_release_commit=target_commit,
        latest_release_version="0.26.0",
        target=Target("v0.26.0", target_commit, "0.26.0", "release tag"),
        incoming=(),
        already_current=False,
        dirty=False,
    )

    assert update_cli._apply_argv(install, recon, pin=None) == (
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        f"git+{update_cli.REPO_URL}@{target_commit}",
    )


def test_already_current_verify_still_requires_target_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=Path("/repo/src/hermes_channel_bgos/__init__.py"),
        python=Path("/repo/.venv/bin/python"),
        checkout_root=Path("/repo"),
    )
    commit = "a" * 40
    recon = ReconResult(
        current_commit=commit,
        main_commit=commit,
        main_version="0.26.0",
        latest_release_tag=None,
        latest_release_commit=None,
        latest_release_version=None,
        target=Target("origin/main", commit, "0.26.0", "main"),
        incoming=(),
        already_current=True,
        dirty=True,
    )
    plan = UpdatePlan(
        install=install,
        current_version="0.27.0",
        recon=recon,
        rollback="rollback",
        apply_argv=("git", "pull"),
        restart_command=None,
    )
    monkeypatch.setattr(update_cli, "_run_command", lambda *_args, **_kwargs: "0.27.0")

    with pytest.raises(update_cli.UpdateError, match="does not match target 0.26.0"):
        update_cli._verify(plan)


def test_verify_success_checks_imported_version_and_checkout_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_commit = "b" * 40
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=Path("/repo/src/hermes_channel_bgos/__init__.py"),
        python=Path("/repo/.venv/bin/python"),
        checkout_root=Path("/repo"),
    )
    recon = ReconResult(
        current_commit="a" * 40,
        main_commit=target_commit,
        main_version="0.27.0",
        latest_release_tag=None,
        latest_release_commit=None,
        latest_release_version=None,
        target=Target("origin/main", target_commit, "0.27.0", "main"),
        incoming=(),
        already_current=False,
        dirty=False,
    )
    plan = UpdatePlan(
        install=install,
        current_version="0.26.0",
        recon=recon,
        rollback="rollback",
        apply_argv=("git", "pull"),
        restart_command=None,
    )
    calls: list[tuple[str, ...]] = []

    def fake_command(argv, **_kwargs):
        command = tuple(str(arg) for arg in argv)
        calls.append(command)
        if command[1:2] == ("-c",):
            return "0.27.0"
        if command[-3:] == ("rev-parse", "--verify", "HEAD^{commit}"):
            return target_commit
        pytest.fail(f"unexpected verify command: {command}")

    monkeypatch.setattr(update_cli, "_run_command", fake_command)

    assert update_cli._verify(plan) == "0.27.0"
    assert any(command[-1] == "HEAD^{commit}" for command in calls)


def test_restart_detection_prefers_loaded_launchd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        update_cli.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(update_cli.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        update_cli,
        "_probe",
        lambda argv: subprocess.CompletedProcess(argv, 0, "loaded\n", ""),
    )

    assert update_cli.detect_restart_command() == (
        "launchctl kickstart -k gui/501/ai.hermes.gateway"
    )


def test_restart_detection_uses_loaded_systemd_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        update_cli.shutil,
        "which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr(
        update_cli,
        "_probe",
        lambda argv: subprocess.CompletedProcess(argv, 0, "loaded\n", ""),
    )

    assert update_cli.detect_restart_command() == (
        "systemctl --user restart hermes-gateway.service"
    )


# ---------------------------------------------------------------------------
# Git recon with subprocess mocked at the process boundary
# ---------------------------------------------------------------------------


def test_git_recon_fetches_tags_and_renders_incoming_shortlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkout"
    package_file = _checkout_layout(root)
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=package_file,
        checkout_root=root,
        python=Path("/venv/bin/python"),
    )
    current = "a" * 40
    main = "b" * 40
    old_tag = "c" * 40
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, *, capture_output, text, check):
        command = tuple(str(arg) for arg in argv)
        calls.append(command)
        assert capture_output is True
        assert text is True
        assert check is False
        tail = command[3:]
        stdout = ""
        if tail == ("remote", "get-url", "origin"):
            stdout = update_cli.REPO_URL + "\n"
        elif tail == ("rev-parse", "--verify", "HEAD^{commit}"):
            stdout = current + "\n"
        elif tail[:1] == ("fetch",):
            stdout = ""
        elif tail == ("rev-parse", "--verify", "origin/main^{commit}"):
            stdout = main + "\n"
        elif tail == ("rev-parse", "--verify", "refs/tags/v0.17.0^{commit}"):
            stdout = old_tag + "\n"
        elif tail == ("show", f"{main}:pyproject.toml"):
            stdout = '[project]\nversion = "0.26.0"\n'
        elif tail == ("show", f"{old_tag}:pyproject.toml"):
            stdout = '[project]\nversion = "0.17.0"\n'
        elif tail[0] == "for-each-ref":
            stdout = "v0.17.0\n"
        elif tail == (
            "log", "--oneline", "--no-decorate", f"{current}..{main}",
        ):
            stdout = "b123456 feat: update command\nb234567 test: recon\n"
        elif tail == ("status", "--porcelain", "--untracked-files=normal"):
            stdout = ""
        else:
            pytest.fail(f"unexpected subprocess command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(update_cli.subprocess, "run", fake_run)

    result = update_cli.git_recon(
        install,
        current_version="0.25.1",
        pin=None,
    )

    assert result.current_commit == current
    assert result.target.ref == "origin/main"
    assert result.target.commit == main
    assert result.target.version == "0.26.0"
    assert result.incoming == (
        "b123456 feat: update command",
        "b234567 test: recon",
    )
    assert result.already_current is False
    assert any(command[3:4] == ("fetch",) for command in calls)


def test_subprocess_failure_keeps_stdout_and_stderr_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, *, capture_output, text, check):
        return subprocess.CompletedProcess(
            argv,
            23,
            "remote stdout line 1\nremote stdout line 2\n",
            "fatal line 1\nfatal line 2\n",
        )

    monkeypatch.setattr(update_cli.subprocess, "run", fake_run)

    with pytest.raises(update_cli.CommandFailed) as caught:
        update_cli._run_command(["git", "fetch"])

    assert caught.value.stdout == "remote stdout line 1\nremote stdout line 2\n"
    assert caught.value.stderr == "fatal line 1\nfatal line 2\n"


def test_main_prints_subprocess_failure_output_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_update(*, yes: bool, pin: str | None) -> None:
        raise update_cli.CommandFailed(
            ["git", "fetch"],
            23,
            "remote stdout line 1\nremote stdout line 2\n",
            "fatal line 1\nfatal line 2\n",
        )

    monkeypatch.setattr(update_cli, "run_update", fail_update)

    result = CliRunner().invoke(update_cli.main, [])

    assert result.exit_code == 23
    assert "remote stdout line 1\nremote stdout line 2\n" in result.output
    assert "fatal line 1\nfatal line 2\n" in result.output


def test_default_run_stops_after_plan_without_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_file = _checkout_layout(tmp_path / "repo")
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=package_file,
        checkout_root=tmp_path / "repo",
        python=Path("/venv/bin/python"),
    )
    recon = ReconResult(
        current_commit="a" * 40,
        main_commit="b" * 40,
        main_version="0.26.0",
        latest_release_tag=None,
        latest_release_commit=None,
        latest_release_version=None,
        target=Target("origin/main", "b" * 40, "0.26.0", "main"),
        incoming=("b123456 feat: updater",),
        already_current=False,
        dirty=False,
    )
    monkeypatch.setattr(update_cli, "locate_install", lambda: install)
    monkeypatch.setattr(
        update_cli,
        "git_recon",
        lambda *_args, **_kwargs: recon,
    )
    monkeypatch.setattr(update_cli, "_snapshot", lambda *_args: "rollback")
    monkeypatch.setattr(update_cli, "detect_restart_command", lambda: None)

    def unexpected_apply(*_args, **_kwargs):
        pytest.fail("dry run executed a subprocess after rendering the plan")

    monkeypatch.setattr(update_cli, "_run_command", unexpected_apply)

    update_cli.run_update(yes=False, pin=None)

    output = capsys.readouterr().out
    assert "PLAN" in output
    assert "Dry run complete" in output


def test_yes_run_applies_verifies_and_only_prints_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_file = _checkout_layout(tmp_path / "repo")
    install = InstallInfo(
        kind=InstallKind.CHECKOUT,
        package_file=package_file,
        checkout_root=tmp_path / "repo",
        python=Path("/venv/bin/python"),
    )
    target_commit = "b" * 40
    recon = ReconResult(
        current_commit="a" * 40,
        main_commit=target_commit,
        main_version="0.27.0",
        latest_release_tag=None,
        latest_release_commit=None,
        latest_release_version=None,
        target=Target("origin/main", target_commit, "0.27.0", "main"),
        incoming=("b123456 feat: updater",),
        already_current=False,
        dirty=False,
    )
    restart = "systemctl --user restart hermes-gateway.service"
    monkeypatch.setattr(update_cli, "__version__", "0.26.0")
    monkeypatch.setattr(update_cli, "locate_install", lambda: install)
    monkeypatch.setattr(
        update_cli,
        "git_recon",
        lambda *_args, **_kwargs: recon,
    )
    monkeypatch.setattr(update_cli, "_snapshot", lambda *_args: "rollback")
    monkeypatch.setattr(update_cli, "detect_restart_command", lambda: restart)
    commands: list[tuple[str, ...]] = []

    def fake_command(argv, **_kwargs):
        command = tuple(str(arg) for arg in argv)
        commands.append(command)
        if command[1:2] == ("-c",):
            return "0.27.0"
        if command[-3:] == ("rev-parse", "--verify", "HEAD^{commit}"):
            return target_commit
        if command[3:4] == ("pull",):
            return ""
        pytest.fail(f"unexpected update command: {command}")

    monkeypatch.setattr(update_cli, "_run_command", fake_command)

    update_cli.run_update(yes=True, pin=None)

    output = capsys.readouterr().out
    assert output.index("APPLY") < output.index("VERIFY") < output.index("RESTART")
    assert f"Run deliberately: {restart}" in output
    assert "The updater did not restart the gateway" in output
    assert not any(command[0] in {"launchctl", "systemctl"} for command in commands)
