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
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "tok", "pairing_id": 55},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/55/agent-catalog",
    ).respond(200, {})

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
