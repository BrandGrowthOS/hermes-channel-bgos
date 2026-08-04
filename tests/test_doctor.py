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


# -----------------------------------------------------------------------------
# Token-source honesty (shadowed-token incident): a stale BGOS_API_KEY env
# export holding a BGOS USER api key (not a pair_ pairing token) silently
# shadowed the freshly paired secrets-file token, whoami 401 forever, and the
# doctor could not explain it. The doctor must now report WHERE its token came
# from, prefer the secrets token over a non-pairing env key, and emit a
# specific shadowing finding with the exact fix.
# -----------------------------------------------------------------------------

SECRETS_TOKEN = "pair_9dBfreshlyPairedSecretToken123"
STALE_USER_KEY = "bgos_user_api_key_STALE_abcdef0123456789"


def _write_secrets_file(token: str = SECRETS_TOKEN) -> Path:
    secrets_dir = _hermes_home() / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    sp = secrets_dir / "bgos.json"
    sp.write_text(json.dumps({"pairing_token": token, "pairing_id": 7,
                              "base_url": "https://api.brandgrowthos.ai"}))
    return sp


def test_check_config_reports_secrets_source_and_token_prefix(monkeypatch):
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    monkeypatch.delenv("BGOS_BACKEND_URL", raising=False)
    sp = _write_secrets_file()
    cfg, r = check_config()
    assert cfg is not None and cfg.pairing_token == SECRETS_TOKEN
    assert r.status == OK
    assert "secrets" in r.detail
    assert str(sp) in r.detail
    assert SECRETS_TOKEN[:6] in r.detail       # honest prefix shown
    assert SECRETS_TOKEN not in r.detail       # never the full token


def test_check_config_reports_env_source(monkeypatch):
    monkeypatch.setenv("BGOS_API_KEY", "pair_envTokenExplicitOverride999")
    monkeypatch.delenv("BGOS_BACKEND_URL", raising=False)
    cfg, r = check_config()
    assert cfg is not None and cfg.pairing_token == "pair_envTokenExplicitOverride999"
    assert "BGOS_API_KEY" in r.detail
    assert "pair_envTokenExplicitOverride999" not in r.detail


def test_check_config_prefers_secrets_over_non_pairing_env_key(monkeypatch):
    # The incident case: stale USER api key in env, fresh pairing in secrets.
    monkeypatch.setenv("BGOS_API_KEY", STALE_USER_KEY)
    monkeypatch.delenv("BGOS_BACKEND_URL", raising=False)
    _write_secrets_file()
    cfg, r = check_config()
    assert cfg is not None
    assert cfg.pairing_token == SECRETS_TOKEN


def test_check_token_hygiene_flags_env_shadowing(monkeypatch):
    from hermes_channel_bgos.doctor import check_token_hygiene
    monkeypatch.setenv("BGOS_API_KEY", STALE_USER_KEY)
    sp = _write_secrets_file()
    r = check_token_hygiene()
    assert r is not None
    assert r.status == WARN
    assert "BGOS_API_KEY" in r.detail
    assert "pair_" in r.detail                 # names the expected prefix
    assert str(sp) in r.detail
    assert STALE_USER_KEY not in r.detail      # redacted
    assert STALE_USER_KEY[:8] in r.detail      # but identifiable
    fix = r.fix.lower()
    assert "unset" in fix or "remove" in fix
    assert "restart" in fix


def test_check_token_hygiene_flags_non_pairing_token_without_secrets(monkeypatch):
    from hermes_channel_bgos.doctor import check_token_hygiene
    monkeypatch.setenv("BGOS_API_KEY", STALE_USER_KEY)
    r = check_token_hygiene()
    assert r is not None
    assert r.status == WARN
    assert "pair_" in r.detail
    assert STALE_USER_KEY not in r.detail
    assert "re-pair" in r.fix.lower() or "hermes-pair-bgos" in r.fix


def test_check_token_hygiene_silent_when_clean(monkeypatch):
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    from hermes_channel_bgos.doctor import check_token_hygiene
    _write_secrets_file()
    assert check_token_hygiene() is None


async def test_check_whoami_401_names_token_source(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        401, {"error": "PAIRING_REVOKED"},
    )
    r = await check_whoami(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="bad"),
        token_source="env $BGOS_API_KEY",
    )
    assert r.status == FAIL
    assert "env $BGOS_API_KEY" in r.detail


def test_check_token_hygiene_secrets_token_without_pair_prefix(monkeypatch):
    # A malformed secrets-file token (no pair_ prefix) with NO env override:
    # the finding must point at the secrets file, not at BGOS_API_KEY.
    from hermes_channel_bgos.doctor import check_token_hygiene
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    sp = _write_secrets_file(token="not_a_pairing_token_123456")
    r = check_token_hygiene()
    assert r is not None
    assert r.status == WARN
    assert str(sp) in r.detail
    assert "not_a_pairing_token_123456" not in r.detail
    assert "unset BGOS_API_KEY" not in r.fix
    assert "hermes-pair-bgos" in r.fix


# -----------------------------------------------------------------------------
# Topology checks (0.23.0 install-time topology guard, doctor surface)
# -----------------------------------------------------------------------------


def _write_multi_route_env(monkeypatch) -> Path:
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.setenv("BGOS_AGENTS", "default:Achilles,shadow:Shadow")
    return _hermes_home()


def test_check_topology_fails_on_missing_profile(monkeypatch):
    from hermes_channel_bgos.doctor import check_topology
    _write_multi_route_env(monkeypatch)
    results = check_topology()
    fails = [r for r in results if r.status == FAIL]
    assert any("profile" in r.detail.lower() for r in fails)
    assert any("hermes profile create shadow" in r.fix for r in fails)


def test_check_topology_fails_on_multiplex_off(monkeypatch):
    from hermes_channel_bgos.doctor import check_topology
    home = _write_multi_route_env(monkeypatch)
    shadow = home / "profiles" / "shadow"
    shadow.mkdir(parents=True)
    (shadow / "SOUL.md").write_text("I am Shadow.")
    results = check_topology()
    fails = [r for r in results if r.status == FAIL]
    assert any("multiplex_profiles" in r.detail for r in fails)
    assert any(
        "hermes config set gateway.multiplex_profiles true" in r.fix
        for r in fails
    )


def test_check_topology_fails_on_stray_profile_secrets(monkeypatch):
    from hermes_channel_bgos.doctor import check_topology
    home = _write_multi_route_env(monkeypatch)
    shadow = home / "profiles" / "shadow"
    (shadow / "secrets").mkdir(parents=True)
    (shadow / "SOUL.md").write_text("I am Shadow.")
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    (home / "secrets").mkdir()
    (home / "secrets" / "bgos.json").write_text(
        json.dumps({"pairing_token": "pair_a", "pairing_id": 42}),
    )
    (shadow / "secrets" / "bgos.json").write_text(
        json.dumps({"pairing_token": "pair_a", "pairing_id": 42}),
    )
    results = check_topology()
    fails = [r for r in results if r.status == FAIL]
    assert any("twice" in r.detail for r in fails)
    assert any("rm " in r.fix for r in fails)


def test_check_topology_ok_when_clean(monkeypatch):
    from hermes_channel_bgos.doctor import check_topology
    home = _write_multi_route_env(monkeypatch)
    shadow = home / "profiles" / "shadow"
    shadow.mkdir(parents=True)
    (shadow / "SOUL.md").write_text("I am Shadow.")
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    results = check_topology()
    assert results, "clean topology should still report an OK line"
    assert all(r.status == OK for r in results)


def test_check_topology_single_route_is_ok(monkeypatch):
    from hermes_channel_bgos.doctor import check_topology
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.setenv("BGOS_AGENTS", "hades:Hades")
    results = check_topology()
    assert all(r.status == OK for r in results)


def test_check_topology_reports_blocked_soul(monkeypatch):
    """Layer A of the 2026-08-04 incident: a SOUL.md that trips Hermes's
    threat scanner is silently replaced with [BLOCKED: ...] and the agent
    answers with no persona. The doctor must run the scan when the scanner
    is importable and FAIL loudly."""
    from hermes_channel_bgos.doctor import check_topology
    home = _write_multi_route_env(monkeypatch)
    shadow = home / "profiles" / "shadow"
    shadow.mkdir(parents=True)
    (shadow / "SOUL.md").write_text("I carry controlled mythic weight.")
    (home / "SOUL.md").write_text("Plain and harmless.")
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")

    def fake_scanner(content: str) -> list[str]:
        return ["known_c2_framework"] if "mythic" in content else []

    results = check_topology(soul_scanner=fake_scanner)
    fails = [r for r in results if r.status == FAIL]
    assert any("known_c2_framework" in r.detail for r in fails)
    assert any("SOUL.md" in r.detail for r in fails)


def test_check_topology_reports_stale_blocked_sessions(monkeypatch):
    """Layer B: sessions whose STORED system prompt carries the [BLOCKED:
    placeholder replay it forever (prefix-cache prompt reuse). The doctor
    counts them and prints the exact remediation."""
    import sqlite3
    from hermes_channel_bgos.doctor import check_topology
    home = _write_multi_route_env(monkeypatch)
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, system_prompt TEXT, "
        "ended_at REAL)"
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?)",
        [
            ("s1", "[BLOCKED: SOUL.md contained potential prompt injection]", None),
            ("s2", "[BLOCKED: SOUL.md contained potential prompt injection]", 123.0),
            ("s3", "healthy prompt", None),
        ],
    )
    conn.commit()
    conn.close()
    results = check_topology()
    hits = [r for r in results if "stale_blocked_sessions" in r.name]
    assert hits and hits[0].status == FAIL  # one affected session still open
    assert "2 session(s)" in hits[0].detail
    assert "system_prompt = NULL" in hits[0].fix


async def test_check_pairing_overlap_fails_on_duplicate(mock_bgos_server):
    from hermes_channel_bgos.doctor import check_pairing_overlap
    cfg = BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_t")
    mock_bgos_server.on("GET", "/api/v1/integrations/pairings").respond(
        200, [
            {"id": 1, "device_label": "this-box",
             "agent_catalog": [{"agent_route": "shadow", "name": "Shadow"}]},
            {"id": 2, "device_label": "old-box",
             "agent_catalog": [{"agent_route": "shadow", "name": "Shadow"}]},
        ],
    )
    result = await check_pairing_overlap(
        cfg, self_pairing_id=1, routes=["default", "shadow"],
    )
    assert result.status == FAIL
    assert "old-box" in result.detail
    assert "revoke" in result.fix.lower()


async def test_check_pairing_overlap_honest_when_unavailable(mock_bgos_server):
    """If the backend cannot answer, the doctor must say the check could not
    run - never imply it passed."""
    from hermes_channel_bgos.doctor import check_pairing_overlap
    cfg = BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_t")
    # route unstubbed -> 501
    result = await check_pairing_overlap(
        cfg, self_pairing_id=1, routes=["default", "shadow"],
    )
    assert result.status == WARN
    assert "could not" in result.detail.lower()


async def test_check_pairing_overlap_ok_when_clean(mock_bgos_server):
    from hermes_channel_bgos.doctor import check_pairing_overlap
    cfg = BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_t")
    mock_bgos_server.on("GET", "/api/v1/integrations/pairings").respond(
        200, [
            {"id": 1, "device_label": "this-box",
             "agent_catalog": [{"agent_route": "shadow", "name": "Shadow"}]},
        ],
    )
    result = await check_pairing_overlap(
        cfg, self_pairing_id=1, routes=["default", "shadow"],
    )
    assert result.status == OK


async def test_run_checks_includes_topology(monkeypatch):
    """run_checks surfaces the topology tier even offline/unpaired."""
    from hermes_channel_bgos.doctor import run_checks
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    _write_multi_route_env(monkeypatch)
    results = await run_checks(offline=True)
    names = {r.name for r in results}
    assert any(n.startswith("topology") for n in names)
