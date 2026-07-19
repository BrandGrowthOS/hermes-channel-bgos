"""Tests for the version heartbeat (Gap 2).

The adapter reports its package version via
POST /api/v1/integrations/heartbeat at boot (connect) and every 6h so the
backend pairing row's daemon_version is never NULL — the BGOS app's update
prompt depends on it. Failures are swallowed (a heartbeat must never block
or crash the daemon), and the version constant is the single source of
truth kept in step with pyproject.toml.
"""
from __future__ import annotations

import asyncio
import re
import tomllib
from pathlib import Path

import pytest

from hermes_channel_bgos import __version__
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.bgos_api import BgosApi
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT_PATH = "/api/v1/integrations/heartbeat"


async def _wait_for_request(server, method: str, path: str, timeout: float = 3.0):
    async def _wait() -> None:
        while not any(
            r.method == method and r.path == path for r in server.requests
        ):
            await asyncio.sleep(0.05)
    await asyncio.wait_for(_wait(), timeout=timeout)


# -----------------------------------------------------------------------------
# BgosApi.post_heartbeat wire format
# -----------------------------------------------------------------------------


async def test_post_heartbeat_minimal_body(mock_bgos_server):
    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(204)

    await api.post_heartbeat(daemon_version="1.2.3")

    req = mock_bgos_server.last_request("POST", HEARTBEAT_PATH)
    assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
    assert req.json_body == {"daemonVersion": "1.2.3"}
    await api.close()


async def test_post_heartbeat_optional_fields(mock_bgos_server):
    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(204)

    await api.post_heartbeat(
        daemon_version="1.2.3",
        env={"platform": "linux", "python": "3.12.1", "hermes": "0.9.0"},
        last_error={
            "code": "WS_DROP",
            "message": "boom",
            "at": "2026-07-17T00:00:00Z",
        },
    )

    req = mock_bgos_server.last_request("POST", HEARTBEAT_PATH)
    assert req.json_body == {
        "daemonVersion": "1.2.3",
        "env": {"platform": "linux", "python": "3.12.1", "hermes": "0.9.0"},
        "lastError": {
            "code": "WS_DROP",
            "message": "boom",
            "at": "2026-07-17T00:00:00Z",
        },
    }
    await api.close()


# -----------------------------------------------------------------------------
# Adapter lifecycle: boot heartbeat + failure tolerance
# -----------------------------------------------------------------------------


async def _connected_adapter(mock_bgos_server) -> BGOSAdapter:
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": []},
    )
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    return adapter


async def test_connect_sends_boot_heartbeat_with_package_version(mock_bgos_server):
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(204)
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await _wait_for_request(mock_bgos_server, "POST", HEARTBEAT_PATH)
        req = mock_bgos_server.last_request("POST", HEARTBEAT_PATH)
        assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
        assert req.json_body["daemonVersion"] == __version__
        # Env facts ride the same beat (backend HeartbeatDto `env` object).
        env = req.json_body["env"]
        assert env["platform"]
        assert env["python"]
        assert all(len(value) <= 64 for value in env.values())
    finally:
        await adapter.disconnect()


async def test_heartbeat_failure_is_swallowed(mock_bgos_server):
    """A failing heartbeat endpoint must never crash the daemon: connect
    succeeds, the loop task stays alive (sleeping until the next beat), and
    normal sends keep working."""
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(
        500, {"error": "INTERNAL"},
    )
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 900})
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await _wait_for_request(mock_bgos_server, "POST", HEARTBEAT_PATH)
        # Give the loop a beat to process the failure.
        await asyncio.sleep(0.1)
        assert adapter._heartbeat_task is not None
        assert not adapter._heartbeat_task.done()

        # The daemon is still fully functional after the failed beat.
        adapter._state.record_inbound_chat(11)
        result = await adapter.send(chat_id=11, content="still alive")
        assert result.success is True
    finally:
        await adapter.disconnect()


async def test_disconnect_cancels_heartbeat_task(mock_bgos_server):
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(204)
    adapter = await _connected_adapter(mock_bgos_server)
    task = adapter._heartbeat_task
    assert task is not None
    await adapter.disconnect()
    assert adapter._heartbeat_task is None
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


# -----------------------------------------------------------------------------
# Version single source of truth
# -----------------------------------------------------------------------------


async def test_version_constant_matches_pyproject():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


async def test_plugin_yaml_version_matches_package():
    yaml_text = (
        REPO_ROOT / "plugins" / "platforms" / "bgos" / "plugin.yaml"
    ).read_text()
    match = re.search(r"^version:\s*(\S+)\s*$", yaml_text, re.MULTILINE)
    assert match is not None, "plugin.yaml has no version line"
    assert match.group(1) == __version__
