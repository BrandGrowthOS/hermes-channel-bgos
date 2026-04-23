"""Tests for BGOSAdapter.send_exec_approval (Task 7) + the approval-state
bookkeeping (_approval_state) that Task 8's callback router consumes.
"""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


async def _connected_adapter(server) -> BGOSAdapter:
    server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"id": 7, "agent_route": "hades"}]},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    return adapter


async def test_send_exec_approval_posts_expected_payload(mock_bgos_server):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 777})

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.send_exec_approval(
            chat_id=11,
            command="rm -rf node_modules",
            session_key="sess-abc-123",
            description="Hades wants to wipe node_modules. Proceed?",
            metadata={"thread_id": "t1"},
        )
        assert result.message_id == 777

        req = mock_bgos_server.last_request("POST", "/api/v1/messages")
        body = req.json_body

        # Wire format matches backend CreateMessageDto: camelCase `messageType`
        # and `approvalMeta`. The `approvalMeta` contents stay snake_case
        # because the backend stores them verbatim as JSONB and the adapter
        # controls the schema (session_key, approval_id, etc.).
        assert body["chatId"] == 11
        assert body["messageType"] == "approval_request"
        assert body["sender"] == "assistant"
        assert body["text"] == "Hades wants to wipe node_modules. Proceed?"

        meta = body["approvalMeta"]
        assert meta["command"] == "rm -rf node_modules"
        assert meta["session_key"] == "sess-abc-123"
        approval_id = meta["approval_id"]
        assert isinstance(approval_id, int) and approval_id >= 1
        assert meta["metadata"] == {"thread_id": "t1"}

        # 4 buttons, Telegram-parity order and callback_data.
        # Option keys are camelCase on the wire (text + callbackData) to
        # match backend CreateMessageOptionDto.
        labels_choices = [(o["text"], o["callbackData"]) for o in body["options"]]
        assert labels_choices == [
            ("Allow once",         f"ea:once:{approval_id}"),
            ("Allow for session",  f"ea:session:{approval_id}"),
            ("Always allow",       f"ea:always:{approval_id}"),
            ("Deny",               f"ea:deny:{approval_id}"),
        ]
        # 2×2 layout
        row_indices = [o["row_index"] for o in body["options"]]
        assert row_indices == [0, 0, 1, 1]
        # Styling cues
        styles = [o["style"] for o in body["options"]]
        assert styles == ["success", "success", "default", "danger"]

        # State stash — keyed by approval_id, maps to session_key
        assert adapter._approval_state[approval_id] == "sess-abc-123"
    finally:
        await adapter.disconnect()


async def test_approval_id_monotonic_per_adapter(mock_bgos_server):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 900})

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.send_exec_approval(chat_id=1, command="a", session_key="s1", description="d")
        await adapter.send_exec_approval(chat_id=1, command="b", session_key="s2", description="d")
        await adapter.send_exec_approval(chat_id=1, command="c", session_key="s3", description="d")

        ids = sorted(adapter._approval_state.keys())
        assert ids == [1, 2, 3]
        assert adapter._approval_state[1] == "s1"
        assert adapter._approval_state[2] == "s2"
        assert adapter._approval_state[3] == "s3"
    finally:
        await adapter.disconnect()


async def test_metadata_optional(mock_bgos_server):
    """metadata defaults to an empty dict in approval_meta when None."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 901})

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.send_exec_approval(
            chat_id=1, command="ls", session_key="sX", description="Hm?",
        )
        req = mock_bgos_server.last_request("POST", "/api/v1/messages")
        assert req.json_body["approvalMeta"]["metadata"] == {}
    finally:
        await adapter.disconnect()


async def test_approval_state_not_stashed_when_post_fails(mock_bgos_server):
    """If the BGOS POST fails, we must NOT leak a phantom pending approval —
    the gateway will retry via the plain-text fallback."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(500, text="boom")

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        with pytest.raises(Exception):
            await adapter.send_exec_approval(
                chat_id=1, command="x", session_key="sY", description="?",
            )
        assert adapter._approval_state == {}
    finally:
        await adapter.disconnect()
