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
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    for name in (
        "BGOS_ASSISTANT_ID",
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


# -----------------------------------------------------------------------------
# write_env: must land where the gateway actually reads env from.
# The gateway loads $HERMES_HOME/.env itself at startup on every platform
# (gateway/run.py -> load_hermes_dotenv), which is what makes the vars reach a
# launchd-managed gateway on macOS. The old fallback wrote $install/.env,
# which nothing consumed on macOS -> doctor reported auth/catalog "unset".
# -----------------------------------------------------------------------------


def _run_env_fn(tmp_path: Path, script: str, extra_env: dict[str, str] | None = None):
    env = _isolated_env(tmp_path)
    env.pop("BGOS_ENV_FILE", None)
    env.pop("BGOS_BACKEND_URL", None)
    env.pop("HERMES_HOME", None)
    # Strip hermes from PATH so `hermes config env-path` cannot hijack the
    # test onto the developer's real ~/.hermes/.env.
    env["PATH"] = "/usr/bin:/bin"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", f"source install.sh; {script}"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def test_write_env_defaults_to_hermes_home_env(tmp_path: Path):
    hermes_home = tmp_path / "hermes_home"
    result = _run_env_fn(
        tmp_path,
        "BGOS_AGENTS=a:B write_env /tmp/does-not-matter",
        {"HERMES_HOME": str(hermes_home)},
    )
    assert result.returncode == 0, result.stderr
    envfile = hermes_home / ".env"
    assert envfile.is_file()
    content = envfile.read_text()
    assert "BGOS_AGENTS=a:B\n" in content
    assert "BGOS_ALLOW_ALL_USERS=true\n" in content


def test_write_env_is_idempotent_and_preserves_other_keys(tmp_path: Path):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    envfile = hermes_home / ".env"
    envfile.write_text("OPENAI_API_KEY=sk-keepme\nBGOS_AGENTS=old:Old\n")
    for _ in range(2):
        result = _run_env_fn(
            tmp_path,
            "BGOS_AGENTS=new:New write_env /tmp/x",
            {"HERMES_HOME": str(hermes_home)},
        )
        assert result.returncode == 0, result.stderr
    content = envfile.read_text()
    assert content.count("BGOS_AGENTS=") == 1
    assert "BGOS_AGENTS=new:New\n" in content
    assert content.count("BGOS_ALLOW_ALL_USERS=") == 1
    assert "OPENAI_API_KEY=sk-keepme\n" in content


def test_write_env_normalizes_suffixed_backend_url(tmp_path: Path):
    hermes_home = tmp_path / "hermes_home"
    result = _run_env_fn(
        tmp_path,
        "BGOS_AGENTS=a:B write_env /tmp/x",
        {
            "HERMES_HOME": str(hermes_home),
            "BGOS_BACKEND_URL": "https://api.brandgrowthos.ai/api/v1",
        },
    )
    assert result.returncode == 0, result.stderr
    content = (hermes_home / ".env").read_text()
    assert "BGOS_BACKEND_URL=https://api.brandgrowthos.ai\n" in content
    assert "/api/v1" not in content


def test_write_env_honors_bgos_env_file_override(tmp_path: Path):
    target = tmp_path / "custom" / "my.env"
    result = _run_env_fn(
        tmp_path,
        "BGOS_AGENTS=a:B write_env /tmp/x",
        {"BGOS_ENV_FILE": str(target)},
    )
    assert result.returncode == 0, result.stderr
    assert target.is_file()
    assert "BGOS_AGENTS=a:B\n" in target.read_text()


def test_normalize_backend_url_variants(tmp_path: Path):
    cases = {
        "https://api.brandgrowthos.ai/api/v1": "https://api.brandgrowthos.ai",
        "https://api.brandgrowthos.ai/api/v1/": "https://api.brandgrowthos.ai",
        "https://api.brandgrowthos.ai/": "https://api.brandgrowthos.ai",
        "https://api.brandgrowthos.ai": "https://api.brandgrowthos.ai",
        "http://localhost:4000/api/v1": "http://localhost:4000",
    }
    for raw, expected in cases.items():
        result = _run_env_fn(
            tmp_path, f"normalize_backend_url {shlex.quote(raw)}",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected, raw


def test_register_plugin_symlinks_valid_target(tmp_path: Path):
    fakepy, capture = _make_fakepy(tmp_path)  # exits 0 -> "modern Hermes" path
    hermes_home = tmp_path / "hermes_home"
    result = _run_env_fn(
        tmp_path,
        f"register_plugin_or_patch /tmp/install {shlex.quote(str(fakepy))}",
        {
            "HERMES_HOME": str(hermes_home),
            "REPO_DIR": str(REPO_ROOT),
            "CAPTURE_FILE": str(capture),
        },
    )
    assert result.returncode == 0, result.stderr
    link = hermes_home / "plugins" / "bgos"
    assert link.is_symlink()
    assert (link / "plugin.yaml").is_file()
    assert (link / "__init__.py").is_file()
    assert (link / "adapter.py").is_file()


def test_register_plugin_fails_loudly_on_missing_source(tmp_path: Path):
    fakepy, capture = _make_fakepy(tmp_path)
    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    result = _run_env_fn(
        tmp_path,
        f"register_plugin_or_patch /tmp/install {shlex.quote(str(fakepy))}",
        {
            "HERMES_HOME": str(tmp_path / "hh"),
            "REPO_DIR": str(empty_repo),
            "CAPTURE_FILE": str(capture),
        },
    )
    assert result.returncode != 0
    assert "Plugin source incomplete" in result.stderr


def test_pair_if_requested_forwards_assistant_id(tmp_path: Path):
    result, capture_file = _run_pair(
        tmp_path,
        "BGOS_PAIR_CODE=BGOS-AAAA-11 BGOS_ASSISTANT_ID=1012",
    )
    assert result.returncode == 0, result.stderr
    args = _captured_args(capture_file)
    assert "--assistant-id" in args
    assert args[args.index("--assistant-id") + 1] == "1012"


def test_pair_if_requested_omits_assistant_id_when_unset(tmp_path: Path):
    result, capture_file = _run_pair(
        tmp_path, "BGOS_PAIR_CODE=BGOS-AAAA-11",
    )
    assert result.returncode == 0, result.stderr
    assert "--assistant-id" not in _captured_args(capture_file)
