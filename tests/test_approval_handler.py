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
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    return adapter


def _make_adapter() -> BGOSAdapter:
    """Lightweight adapter for testing approval-callback paths without
    spinning up the mock backend. Tests monkeypatch the api / ws fields."""
    return BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))


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
        assert result.message_id == "777"  # fork's SendResult typing is str

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


# -----------------------------------------------------------------------------
# Task 2.1 — Approval callback edits the original bubble in place.
# Matches Telegram's UX at gateway/platforms/telegram.py:2533-2537 — once the
# user taps a button, the buttons vanish and the bubble shows the resolution
# (e.g. "Approved once by user_42"). Bypasses the edit_message throttle since
# the resolution is a one-shot per approval — no edit-storm risk.
# -----------------------------------------------------------------------------


async def test_approval_callback_edits_message_in_place(monkeypatch):
    """After resolving an approval, the original bubble should be edited
    to show the choice + user — matches Telegram's UX where buttons
    disappear and the message shows the resolution."""
    adapter = _make_adapter()
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_gateway_approval",
        lambda sk, choice: None,
    )
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")  # needed once Task 2.2 lands
    captured_patches = []
    async def fake_patch(mid, *, text=None, options=None, **kw):
        captured_patches.append({"message_id": mid, "text": text, "options": options})
        return {"id": mid}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    adapter._approval_state[7] = "session-abc"
    await adapter._handle_callback({
        "callback_data": "ea:once:7",
        "user_id": "user_42",
        "message_id": 99,
        "chat_id": 1,
    })
    assert captured_patches[0]["message_id"] == 99
    assert "Approved once" in captured_patches[0]["text"]
    assert "user_42" in captured_patches[0]["text"]
    assert captured_patches[0]["options"] == []  # buttons removed


async def test_approval_callback_edit_uses_camelcase_payload(monkeypatch):
    """Backend sometimes emits callback events with camelCase keys
    (messageId, chatId) — same surface as inbound_click. Honor both."""
    adapter = _make_adapter()
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_gateway_approval",
        lambda sk, choice: None,
    )
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    seen = []
    async def fake_patch(mid, *, text=None, **kw):
        seen.append(mid)
        return {"id": mid}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    adapter._approval_state[8] = "sess-x"
    await adapter._handle_callback({
        "callback_data": "ea:always:8",
        "userId": "user_42",
        "messageId": 200,
        "chatId": 1,
    })
    assert 200 in seen


async def test_approval_callback_edit_swallows_patch_errors(monkeypatch, caplog):
    """If the bubble can't be edited (e.g. deleted), still resolve the
    approval — that already happened. Just log."""
    adapter = _make_adapter()
    resolved = []
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_gateway_approval",
        lambda sk, choice: resolved.append((sk, choice)),
    )
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    async def fake_patch(mid, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    adapter._approval_state[9] = "sess-y"
    await adapter._handle_callback({
        "callback_data": "ea:deny:9",
        "user_id": "u",
        "message_id": 50,
        "chat_id": 1,
    })
    # The approval still resolved — that's the important contract
    assert resolved == [("sess-y", "deny")]
