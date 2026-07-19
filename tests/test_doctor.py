"""Tests for hermes-bgos-doctor."""
from __future__ import annotations

import asyncio
import enum
import json
import os
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.doctor import (
    FAIL, OK, WARN, CheckResult, check_catalog, check_config, check_env,
    check_package, check_registration, check_whoami, gateway_env,
    main as doctor_main, render_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO_ROOT / "plugins" / "platforms" / "bgos"


def _hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"])


def _install_fake_gateway(monkeypatch) -> None:
    """A `gateway.config.Platform` WITHOUT a BGOS member, mimicking a Hermes
    install where the doctor's own process has not run plugin discovery.
    `hermes_cli` stays unimportable in this venv, so the discovery probe
    raises - exactly the fresh-install situation observed on KC's machine."""
    gw = types.ModuleType("gateway")
    cfgmod = types.ModuleType("gateway.config")

    class Platform(enum.Enum):
        TELEGRAM = "telegram"

    cfgmod.Platform = Platform
    gw.config = cfgmod
    monkeypatch.setitem(sys.modules, "gateway", gw)
    monkeypatch.setitem(sys.modules, "gateway.config", cfgmod)


def test_check_package_reports_version():
    r = check_package()
    assert r.status == OK
    assert "hermes_channel_bgos" in r.detail


def test_check_catalog_warns_when_unset(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS", raising=False)
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    r = check_catalog()
    assert r.status == WARN
    assert r.fix


def test_check_catalog_ok(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.setenv("BGOS_AGENTS", "default:David")
    r = check_catalog()
    assert r.status == OK
    assert "default:David" in r.detail


def test_check_env_warns_without_auth(monkeypatch):
    monkeypatch.delenv("BGOS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("BGOS_ALLOWED_USERS", raising=False)
    assert check_env().status == WARN


def test_check_env_ok_with_allow_all(monkeypatch):
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    assert check_env().status == OK


def test_check_config_not_paired(monkeypatch):
    # autouse fixture points HERMES_HOME at an empty tmp dir → no secrets file
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    cfg, r = check_config()
    assert cfg is None
    assert r.status == FAIL
    assert "hermes-pair-bgos" in r.fix


async def test_check_whoami_reports_exposed_assistants(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": [
            {"assistant_id": 892, "agent_route": "default", "name": "David"}]},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok"))
    assert r.status == OK
    assert "892" in r.detail


async def test_check_whoami_401_is_fail(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        401, {"error": "PAIRING_REVOKED"},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="bad"))
    assert r.status == FAIL
    assert "re-pair" in r.fix.lower()


async def test_check_whoami_warns_when_no_assistants(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": []},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok"))
    assert r.status == WARN


def test_render_json_marks_fail():
    data = json.loads(render_json([
        CheckResult("a", OK, "x"),
        CheckResult("b", FAIL, "y", "fixit"),
    ]))
    assert data["result"] == "fail"
    assert data["checks"][1]["fix"] == "fixit"


def test_render_json_ok_when_no_fail():
    data = json.loads(render_json([CheckResult("a", OK, "x"), CheckResult("b", WARN, "y")]))
    assert data["result"] == "ok"


def test_check_registration_reports_none_without_hermes():
    # No `gateway` importable in the test env, and Platform.BGOS not present.
    from hermes_channel_bgos.doctor import check_registration, FAIL
    r = check_registration()
    assert r.name == "registration"
    assert r.status == FAIL
    assert "plugin" in r.fix.lower() or "patch" in r.fix.lower()


async def test_doctor_main_exits_1_when_unconfigured(monkeypatch):
    # No Hermes gateway in the test env → registration FAILs → exit 1.
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    runner = CliRunner()
    result = await asyncio.to_thread(runner.invoke, doctor_main, ["--offline"])
    assert result.exit_code == 1
    assert "registration" in result.output


# -----------------------------------------------------------------------------
# Gateway-effective env sourcing (auth/catalog must read what the GATEWAY sees:
# $HERMES_HOME/.env is loaded by the gateway itself at startup - reading only
# the doctor's process env false-alarmed "unset" on every fresh macOS install)
# -----------------------------------------------------------------------------


def test_check_env_reads_hermes_home_env_file(monkeypatch):
    monkeypatch.delenv("BGOS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("BGOS_ALLOWED_USERS", raising=False)
    (_hermes_home() / ".env").write_text(
        "# written by install.sh\nBGOS_ALLOW_ALL_USERS=true\n",
    )
    r = check_env()
    assert r.status == OK
    assert ".env" in r.detail


def test_check_env_file_overrides_process_env(monkeypatch):
    # Mirrors load_hermes_dotenv(override=True): the user .env beats a stale
    # shell export, so the doctor must report the file's value.
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "false")
    (_hermes_home() / ".env").write_text("BGOS_ALLOW_ALL_USERS=true\n")
    assert check_env().status == OK


def test_check_catalog_reads_hermes_home_env_file(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS", raising=False)
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    (_hermes_home() / ".env").write_text('BGOS_AGENTS="default:David"\n')
    r = check_catalog()
    assert r.status == OK
    assert "default:David" in r.detail


def test_gateway_env_legacy_install_env_fallback(monkeypatch, tmp_path):
    # No $HERMES_HOME/.env → the hermes-install project .env fills gaps
    # (legacy installs where install.sh wrote $install/.env).
    monkeypatch.delenv("BGOS_AGENTS", raising=False)
    install = tmp_path / "hermes-agent"
    install.mkdir()
    (install / ".env").write_text("BGOS_AGENTS=default:Legacy\n")
    monkeypatch.setenv("HERMES_INSTALL", str(install))
    env, used = gateway_env()
    assert env["BGOS_AGENTS"] == "default:Legacy"
    assert used == install / ".env"


def test_check_config_normalizes_suffixed_secrets_base_url(monkeypatch):
    # The exact bad persisted value from KC's fresh install must self-heal.
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    monkeypatch.delenv("BGOS_BACKEND_URL", raising=False)
    secrets_dir = _hermes_home() / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "bgos.json").write_text(json.dumps({
        "pairing_token": "pair_x",
        "pairing_id": 1,
        "base_url": "https://api.brandgrowthos.ai/api/v1",
    }))
    cfg, r = check_config()
    assert cfg is not None
    assert cfg.base_url == "https://api.brandgrowthos.ai"
    assert r.status == OK
    assert "normalized from" in r.detail


# -----------------------------------------------------------------------------
# Honest registration check: the doctor cannot see the RUNNING gateway's
# registry, so a valid installed plugin must not FAIL just because the
# in-process discovery probe is unavailable.
# -----------------------------------------------------------------------------


def test_check_registration_ok_with_valid_symlink_when_probe_unavailable(monkeypatch):
    _install_fake_gateway(monkeypatch)
    plugins = _hermes_home() / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "bgos").symlink_to(PLUGIN_SRC)
    r = check_registration()
    assert r.status == OK
    assert "plugin installed" in r.detail
    assert "gateway registers it" in r.detail


def test_check_registration_fails_on_broken_symlink(monkeypatch, tmp_path):
    _install_fake_gateway(monkeypatch)
    plugins = _hermes_home() / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "bgos").symlink_to(tmp_path / "nowhere")
    r = check_registration()
    assert r.status == FAIL
    assert "broken symlink" in r.detail


def test_check_registration_fails_without_plugin_or_patch(monkeypatch):
    _install_fake_gateway(monkeypatch)
    r = check_registration()
    assert r.status == FAIL
    assert "no plugin at" in r.detail
    assert "symlink" in r.fix


def test_check_registration_ok_via_patch(monkeypatch):
    _install_fake_gateway(monkeypatch)
    cfgmod = sys.modules["gateway.config"]

    class Platform(enum.Enum):
        BGOS = "bgos"

    monkeypatch.setattr(cfgmod, "Platform", Platform)
    r = check_registration()
    assert r.status == OK
    assert "via patch" in r.detail
