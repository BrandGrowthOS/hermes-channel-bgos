"""Tests for the hermes-pair-bgos CLI (Task 10).

The CLI's `main` wraps its work in `asyncio.run(...)`, which can't nest
inside pytest-asyncio's event loop. We work around that by dispatching
`CliRunner.invoke` through `asyncio.to_thread` so Click gets its own OS
thread (and therefore its own event loop).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from hermes_channel_bgos.pair_cli import main, secrets_path


# pytest-asyncio runs in auto mode (see pyproject.toml), so async def tests
# are discovered without marks. No module-level pytestmark here — the one
# sync test below (test_secrets_path_respects_hermes_home) then runs
# cleanly as a pure-sync test.


async def _invoke_cli(args: list[str]) -> object:
    runner = CliRunner()
    return await asyncio.to_thread(runner.invoke, main, args)


async def test_pair_cli_writes_secret_file(mock_bgos_server, tmp_secrets_dir):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz_secret", "pairing_id": 42},
    )

    result = await _invoke_cli([
        "BGOS-ABCD-EF",
        "--device-label", "hades-box",
        "--base-url", mock_bgos_server.url,
    ])
    assert result.exit_code == 0, result.output
    assert "Paired" in result.output

    secrets_file = secrets_path()
    assert secrets_file.exists()
    data = json.loads(secrets_file.read_text())
    assert data["pairing_token"] == "pair_xyz_secret"
    assert data["pairing_id"] == 42
    assert data["base_url"] == mock_bgos_server.url


async def test_pair_cli_normalizes_suffixed_base_url(mock_bgos_server, tmp_secrets_dir):
    """A base URL pasted in app-facing form (trailing /api/v1) must still pair
    AND must be persisted in origin form - the suffixed form used to double
    the API prefix on every later request (fresh-install whoami 404)."""
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_norm", "pairing_id": 7},
    )

    result = await _invoke_cli([
        "BGOS-ABCD-EF",
        "--device-label", "kc-macbook",
        "--base-url", f"{mock_bgos_server.url}/api/v1",
    ])
    assert result.exit_code == 0, result.output

    data = json.loads(secrets_path().read_text())
    assert data["base_url"] == mock_bgos_server.url


async def test_pair_cli_sends_correct_payload(mock_bgos_server, tmp_secrets_dir):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz", "pairing_id": 1},
    )

    result = await _invoke_cli([
        "BGOS-WXYZ-12", "--device-label", "laptop",
        "--base-url", mock_bgos_server.url,
    ])
    assert result.exit_code == 0, result.output

    req = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/pair-exchange",
    )
    # Backend PairExchangeDto is camelCase.
    assert req.json_body == {
        "code": "BGOS-WXYZ-12",
        "deviceLabel": "laptop",
        "integration": "hermes",
        "agentCatalog": [],
    }
    assert "X-BGOS-Pairing" not in req.headers  # pre-auth endpoint


async def test_pair_cli_exits_nonzero_on_failure(mock_bgos_server, tmp_secrets_dir):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        410, {"error": "CODE_EXPIRED"},
    )

    result = await _invoke_cli([
        "BGOS-EXPIRED", "--device-label", "x",
        "--base-url", mock_bgos_server.url,
    ])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()
    assert "410" in result.output

    # No secret file written on failure
    assert not secrets_path().exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only mode check")
async def test_pair_cli_secret_file_is_mode_0600(mock_bgos_server, tmp_secrets_dir):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz", "pairing_id": 1},
    )

    await _invoke_cli([
        "BGOS-CODE", "--device-label", "x",
        "--base-url", mock_bgos_server.url,
    ])
    mode = secrets_path().stat().st_mode & 0o777
    assert mode == 0o600
    # The secrets dir must also be owner-only (created 0700 up front, not a
    # world-listable 0755 that briefly exposes the token filename).
    dir_mode = secrets_path().parent.stat().st_mode & 0o777
    assert dir_mode == 0o700


async def test_device_label_is_required(mock_bgos_server, tmp_secrets_dir):
    result = await _invoke_cli(["BGOS-CODE"])
    assert result.exit_code != 0
    lower = result.output.lower()
    assert "device-label" in lower or "device_label" in lower


def test_secrets_path_respects_hermes_home(tmp_path, monkeypatch):
    """Pure sync — no fixtures that need an event loop."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert secrets_path() == tmp_path / "custom" / "secrets" / "bgos.json"


async def test_pair_cli_agents_pushes_catalog(mock_bgos_server, tmp_secrets_dir):
    # A 2-route catalog must pass the 0.23.0 topology guard to reach the
    # exchange: give the host a hades profile + multiplexing on.
    hermes_home = Path(os.environ["HERMES_HOME"])
    hades = hermes_home / "profiles" / "hades"
    hades.mkdir(parents=True)
    (hades / "SOUL.md").write_text("I am Hades.")
    (hermes_home / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n"
    )
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "tok", "pairing_id": 55},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/55/agent-catalog",
    ).respond(200, {})
    mock_bgos_server.on("GET", "/api/v1/integrations/pairings").respond(200, [])

    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "host",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:David,hades:Hades",
    ])
    assert result.exit_code == 0, result.output

    pe = mock_bgos_server.last_request("POST", "/api/v1/integrations/pair-exchange")
    assert pe.json_body["agentCatalog"] == [
        {"agent_route": "default", "name": "David"},
        {"agent_route": "hades", "name": "Hades"},
    ]

    cat = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/pairings/55/agent-catalog",
    )
    assert cat.headers["X-BGOS-Pairing"] == "tok"
    assert cat.json_body == {"agents": [
        {"agent_route": "default", "name": "David"},
        {"agent_route": "hades", "name": "Hades"},
    ]}


async def test_wait_for_exposure_returns_when_assistants_appear():
    from hermes_channel_bgos.pair_cli import wait_for_exposure

    seq = [
        {"assistants": []},
        {"assistants": []},
        {"assistants": [{"assistant_id": 892, "agent_route": "default", "name": "David"}]},
    ]

    class FakeApi:
        def __init__(self):
            self.i = 0

        async def whoami(self):
            r = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return r

    api = FakeApi()
    result = await wait_for_exposure(api, interval=0.01, timeout=5.0)
    assert result == [{"assistant_id": 892, "agent_route": "default", "name": "David"}]
    assert api.i >= 3


async def test_wait_for_exposure_times_out_empty():
    from hermes_channel_bgos.pair_cli import wait_for_exposure

    class FakeApi:
        async def whoami(self):
            return {"assistants": []}

    result = await wait_for_exposure(FakeApi(), interval=0.01, timeout=0.05)
    assert result == []


async def test_pair_cli_wait_for_exposure_timeout(mock_bgos_server, tmp_secrets_dir):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "tok", "pairing_id": 7},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": []},
    )

    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "h",
        "--base-url", mock_bgos_server.url,
        "--wait-for-exposure", "--wait-timeout", "0.1", "--wait-interval", "0.02",
    ])
    assert result.exit_code == 0, result.output
    assert "expos" in result.output.lower()


# -----------------------------------------------------------------------------
# Install-time topology guard (0.23.0)
#
# The 2026-08-04 Achilles/Shadow bug was fixed in code (0.22.0) but the
# BROKEN TOPOLOGY that produced it had to be repaired by hand: no `shadow`
# profile, multiplexing off, a stale per-profile secrets file, and a
# duplicate pairing. Connect-time log warnings did not prevent any of it.
# The pair CLI now refuses to pair a multi-route catalog into a broken
# topology, and loudly reports duplicate pairings after the exchange.
# -----------------------------------------------------------------------------


def _hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"])


def _make_profile(route: str, *, soul: bool = True) -> None:
    d = _hermes_home() / "profiles" / route
    d.mkdir(parents=True, exist_ok=True)
    if soul:
        (d / "SOUL.md").write_text(f"I am {route}.")


def _enable_multiplex() -> None:
    (_hermes_home() / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n"
    )


async def test_pair_multi_route_aborts_when_profile_missing(
    mock_bgos_server, tmp_secrets_dir,
):
    """Pairing a 2-route catalog with no `shadow` profile must FAIL LOUD
    before the exchange, naming the exact create command. The 0.22.0 CLI
    paired silently into this broken topology."""
    _enable_multiplex()
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_x", "pairing_id": 1},
    )
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "kc-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:Achilles,shadow:Shadow",
    ])
    assert result.exit_code == 1, result.output
    assert "hermes profile create shadow" in result.output
    # The exchange must NOT have happened - no pairing was minted.
    assert not any(
        r.path == "/api/v1/integrations/pair-exchange"
        for r in mock_bgos_server.requests
    )
    assert not secrets_path().exists()


async def test_pair_multi_route_aborts_when_multiplex_off(
    mock_bgos_server, tmp_secrets_dir,
):
    _make_profile("shadow")
    # no config.yaml -> multiplex_profiles defaults to off
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "kc-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:Achilles,shadow:Shadow",
    ])
    assert result.exit_code == 1, result.output
    assert "hermes config set gateway.multiplex_profiles true" in result.output


async def test_pair_multi_route_aborts_on_stray_profile_secrets(
    mock_bgos_server, tmp_secrets_dir,
):
    """A leftover profiles/shadow/secrets/bgos.json makes a second adapter
    connect with its own pairing and double-answer. The pair CLI must name
    the exact file to delete."""
    _enable_multiplex()
    _make_profile("shadow")
    stray = _hermes_home() / "profiles" / "shadow" / "secrets" / "bgos.json"
    stray.parent.mkdir(parents=True)
    stray.write_text(json.dumps({"pairing_token": "pair_old", "pairing_id": 9}))
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "kc-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:Achilles,shadow:Shadow",
    ])
    assert result.exit_code == 1, result.output
    assert str(stray) in result.output


async def test_pair_multi_route_proceeds_when_topology_is_clean(
    mock_bgos_server, tmp_secrets_dir,
):
    _enable_multiplex()
    _make_profile("shadow")
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_ok", "pairing_id": 5},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/5/agent-catalog",
    ).respond(200, {"ok": True})
    mock_bgos_server.on("GET", "/api/v1/integrations/pairings").respond(
        200, [{
            "id": 5, "device_label": "kc-box", "integration": "hermes",
            "agent_catalog": [
                {"agent_route": "default", "name": "Achilles"},
                {"agent_route": "shadow", "name": "Shadow"},
            ],
        }],
    )
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "kc-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:Achilles,shadow:Shadow",
    ])
    assert result.exit_code == 0, result.output
    assert secrets_path().exists()


async def test_pair_skip_topology_check_overrides(
    mock_bgos_server, tmp_secrets_dir,
):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_x", "pairing_id": 1},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/1/agent-catalog",
    ).respond(200, {"ok": True})
    mock_bgos_server.on("GET", "/api/v1/integrations/pairings").respond(200, [])
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "kc-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:Achilles,shadow:Shadow",
        "--skip-topology-check",
    ])
    assert result.exit_code == 0, result.output


async def test_pair_warns_on_overlapping_active_pairing(
    mock_bgos_server, tmp_secrets_dir,
):
    """A re-pair leftover: another ACTIVE pairing already carries the same
    agent route. Every inbound would be answered twice. The CLI must name
    the duplicate pairing and where to revoke it."""
    _enable_multiplex()
    _make_profile("shadow")
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_new", "pairing_id": 5},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/5/agent-catalog",
    ).respond(200, {"ok": True})
    mock_bgos_server.on("GET", "/api/v1/integrations/pairings").respond(
        200, [
            {
                "id": 5, "device_label": "kc-box", "integration": "hermes",
                "agent_catalog": [
                    {"agent_route": "default", "name": "Achilles"},
                    {"agent_route": "shadow", "name": "Shadow"},
                ],
            },
            {
                "id": 7, "device_label": "kc-box-old", "integration": "hermes",
                "agent_catalog": [
                    {"agent_route": "shadow", "name": "Shadow"},
                ],
            },
        ],
    )
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "kc-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:Achilles,shadow:Shadow",
    ])
    assert result.exit_code == 0, result.output
    assert "kc-box-old" in result.output
    assert "twice" in result.output.lower()
    assert "revoke" in result.output.lower()


async def test_pair_reports_when_overlap_check_unavailable(
    mock_bgos_server, tmp_secrets_dir,
):
    """If the backend cannot answer the pairings listing, say so honestly
    instead of implying the check passed. Non-fatal."""
    _enable_multiplex()
    _make_profile("shadow")
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_new", "pairing_id": 5},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/5/agent-catalog",
    ).respond(200, {"ok": True})
    # GET /pairings left unstubbed -> 501
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "kc-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:Achilles,shadow:Shadow",
    ])
    assert result.exit_code == 0, result.output
    assert "could not verify" in result.output.lower()


async def test_pair_single_route_skips_route_profile_checks(
    mock_bgos_server, tmp_secrets_dir,
):
    """A single agent on a non-default route served by the active profile is
    a correct, common topology - the guard must not block it."""
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_x", "pairing_id": 3},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/3/agent-catalog",
    ).respond(200, {"ok": True})
    mock_bgos_server.on("GET", "/api/v1/integrations/pairings").respond(200, [])
    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "hades-box",
        "--base-url", mock_bgos_server.url,
        "--agents", "hades:Hades",
    ])
    assert result.exit_code == 0, result.output


async def test_pair_cli_assistant_id_flag_pins_identity(
    mock_bgos_server, tmp_secrets_dir,
):
    """--assistant-id sends intended_assistant_id (snake_case wire contract,
    the same field the claude plugin pins with; board row 2026-08-09: the
    guard's advice was unfollowable because this flag did not exist)."""
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz", "pairing_id": 9},
    )

    result = await _invoke_cli([
        "BGOS-WXYZ-12", "--device-label", "laptop",
        "--assistant-id", "1012",
        "--base-url", mock_bgos_server.url,
    ])
    assert result.exit_code == 0, result.output

    req = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/pair-exchange",
    )
    assert req.json_body["intended_assistant_id"] == 1012


async def test_pair_cli_assistant_id_env_fallback(
    mock_bgos_server, tmp_secrets_dir, monkeypatch,
):
    monkeypatch.setenv("BGOS_ASSISTANT_ID", "77")
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz", "pairing_id": 9},
    )

    result = await _invoke_cli([
        "BGOS-WXYZ-12", "--device-label", "laptop",
        "--base-url", mock_bgos_server.url,
    ])
    assert result.exit_code == 0, result.output

    req = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/pair-exchange",
    )
    assert req.json_body["intended_assistant_id"] == 77


async def test_pair_cli_unpinned_body_omits_the_field(
    mock_bgos_server, tmp_secrets_dir,
):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz", "pairing_id": 9},
    )

    result = await _invoke_cli([
        "BGOS-WXYZ-12", "--device-label", "laptop",
        "--base-url", mock_bgos_server.url,
    ])
    assert result.exit_code == 0, result.output

    req = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/pair-exchange",
    )
    assert "intended_assistant_id" not in req.json_body
