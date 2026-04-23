"""Tests for BGOSAdapter — lifecycle, `send`, `get_chat_info`.

Inbound translation (Task 5), media overrides (Task 6), send_exec_approval
(Task 7), callback routing (Task 8), and slash-manifest sync (Task 9) are
covered in their respective test files.
"""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.bgos_api import BgosApiError
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


async def test_adapter_connect_calls_whoami_and_builds_route_map(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {
            "pairing_id": 42,
            "assistants": [
                {"assistant_id": 7, "agent_route": "hades", "command_count": 0},
                {"assistant_id": 8, "agent_route": "ramy", "command_count": 3},
            ],
        },
    )

    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    ok = await adapter.connect()

    try:
        assert ok is True
        assert adapter.pairing_id == 42
        assert adapter.assistant_route_map == {7: "hades", 8: "ramy"}
    finally:
        await adapter.disconnect()


async def test_adapter_disconnect_is_idempotent(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": []},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    await adapter.disconnect()

    # Second disconnect is a no-op, not an error
    await adapter.disconnect()


async def test_adapter_401_raises(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        401, {"error": "PAIRING_REVOKED"},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_dead"))

    with pytest.raises(BgosApiError) as excinfo:
        await adapter.connect()
    assert excinfo.value.code == "PAIRING_REVOKED"
    await adapter.disconnect()


async def test_adapter_send_posts_text_message(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 300})

    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    try:
        result = await adapter.send(chat_id=11, content="hello back")
        assert result.message_id == 300
        assert result.ok is True

        req = mock_bgos_server.last_request("POST", "/api/v1/messages")
        body = req.json_body
        # Wire format matches backend CreateMessageDto: camelCase chatId +
        # messageType, lowercase `sender: "assistant"`.
        assert body["chatId"] == 11
        assert body["text"] == "hello back"
        assert body["sender"] == "assistant"
        assert body["messageType"] == "standard"

        # Adapter records the last assistant message id per chat for future
        # streaming-edit support
        assert adapter._state.last_assistant_message_by_chat[11] == 300
    finally:
        await adapter.disconnect()


async def test_adapter_send_accepts_reply_to_kwarg(mock_bgos_server):
    """send() accepts reply_to for Hermes interface compatibility, but
    backend's CreateMessageDto doesn't have a replyTo field yet — it gets
    dropped by the whitelist. This test just verifies the kwarg doesn't
    crash the call; Phase F will add backend support."""
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": []},
    )
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 301})

    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    try:
        await adapter.send(chat_id=11, content="re: earlier", reply_to=100)
        # Just confirms the call went through — no reply_to field on wire yet
        req = mock_bgos_server.last_request("POST", "/api/v1/messages")
        assert req.json_body["sender"] == "assistant"
    finally:
        await adapter.disconnect()


async def test_adapter_get_chat_info_returns_minimal(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": []},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    try:
        info = await adapter.get_chat_info(42)
        assert info == {"platform": "bgos", "chat_id": 42}
    finally:
        await adapter.disconnect()


async def test_assistant_route_map_is_defensive_copy(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    try:
        m = adapter.assistant_route_map
        m[99] = "poisoned"
        assert 99 not in adapter.assistant_route_map
    finally:
        await adapter.disconnect()
