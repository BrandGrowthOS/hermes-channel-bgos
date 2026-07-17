import os
import shlex
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_fakepy(tmp_path: Path) -> tuple[Path, Path]:
    capture_file = tmp_path / "fakepy-argv.txt"
    fakepy = tmp_path / "fakepy"
    fakepy.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" >> \"$CAPTURE_FILE\"\n",
        encoding="utf-8",
    )
    fakepy.chmod(0o755)
    return fakepy, capture_file


def _isolated_env(tmp_path: Path, capture_file: Path | None = None) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    for name in (
        "BGOS_CODE",
        "BGOS_PAIR_CODE",
        "CAPTURE_FILE",
        "HERMES_INSTALL",
        "HERMES_PYTHON",
        "REPO_DIR",
    ):
        env.pop(name, None)
    env["HOME"] = str(home)
    if capture_file is not None:
        env["CAPTURE_FILE"] = str(capture_file)
    return env


def _run_pair(tmp_path: Path, assignments: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    fakepy, capture_file = _make_fakepy(tmp_path)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source install.sh; {assignments} pair_if_requested {shlex.quote(str(fakepy))}",
        ],
        cwd=REPO_ROOT,
        env=_isolated_env(tmp_path, capture_file),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return result, capture_file


def _captured_args(capture_file: Path) -> list[str]:
    if not capture_file.exists():
        return []
    return capture_file.read_text(encoding="utf-8").splitlines()


def _expected_pair_args(code: str) -> list[str]:
    return [
        "-m",
        "hermes_channel_bgos.pair_cli",
        code,
        "--device-label",
        "lab",
        "--agents",
        "a:B",
    ]


def test_pair_if_requested_uses_bgos_pair_code(tmp_path: Path):
    result, capture_file = _run_pair(
        tmp_path,
        "BGOS_PAIR_CODE=BGOS-AAAA-11 DEVICE_LABEL=lab BGOS_AGENTS=a:B",
    )

    assert result.returncode == 0, result.stderr
    assert _captured_args(capture_file) == _expected_pair_args("BGOS-AAAA-11")


def test_pair_if_requested_accepts_bgos_code_synonym(tmp_path: Path):
    result, capture_file = _run_pair(
        tmp_path,
        "BGOS_CODE=BGOS-BBBB-22 DEVICE_LABEL=lab BGOS_AGENTS=a:B",
    )

    assert result.returncode == 0, result.stderr
    assert _captured_args(capture_file) == _expected_pair_args("BGOS-BBBB-22")


def test_pair_if_requested_prefers_bgos_pair_code(tmp_path: Path):
    result, capture_file = _run_pair(
        tmp_path,
        "BGOS_PAIR_CODE=BGOS-AAAA-11 BGOS_CODE=BGOS-BBBB-22 "
        "DEVICE_LABEL=lab BGOS_AGENTS=a:B",
    )

    assert result.returncode == 0, result.stderr
    assert _captured_args(capture_file) == _expected_pair_args("BGOS-AAAA-11")


def test_pair_if_requested_skips_without_code_when_noninteractive(tmp_path: Path):
    result, capture_file = _run_pair(
        tmp_path,
        "DEVICE_LABEL=lab BGOS_AGENTS=a:B",
    )

    assert result.returncode == 0, result.stderr
    assert _captured_args(capture_file) == []
    assert "Pairing skipped." in result.stderr


def test_sourcing_install_sh_does_not_run_main(tmp_path: Path):
    home = tmp_path / "home"
    result = subprocess.run(
        ["bash", "-c", "source install.sh"],
        cwd=REPO_ROOT,
        env=_isolated_env(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (home / "hermes-channel-bgos").exists()
    assert "Could not find Hermes" not in result.stderr


def test_piping_install_sh_runs_main(tmp_path: Path):
    with (REPO_ROOT / "install.sh").open(encoding="utf-8") as script:
        result = subprocess.run(
            ["bash"],
            cwd=REPO_ROOT,
            env=_isolated_env(tmp_path),
            stdin=script,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "Could not find Hermes" in result.stderr


def test_install_sh_parses_cleanly():
    result = subprocess.run(
        ["bash", "-n", "install.sh"],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
