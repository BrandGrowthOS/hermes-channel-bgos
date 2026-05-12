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


# -----------------------------------------------------------------------------
# Task 2.3 — send_slash_confirm() three-button UI + sc:* callback routing.
# Mirrors gateway/platforms/telegram.py:2119. Used by Hermes's generic
# slash-confirm primitive for commands with non-destructive but expensive
# side effects (current caller: /reload-mcp). Buttons are Approve Once /
# Always Approve / Cancel; callback shape sc:<choice>:<confirm_id>.
# -----------------------------------------------------------------------------


async def test_send_slash_confirm_renders_three_buttons(monkeypatch):
    adapter = _make_adapter()
    captured = []
    async def fake_post(**kw):
        captured.append(kw)
        return {"id": 50}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    result = await adapter.send_slash_confirm(
        chat_id=1, title="Reload MCP?",
        message="This invalidates the provider prompt cache.",
        session_key="sess-1", confirm_id="conf-abc",
    )
    assert result.success is True
    options = captured[0]["options"]
    callbacks = [o["callbackData"] for o in options]
    assert callbacks == [
        "sc:once:conf-abc",
        "sc:always:conf-abc",
        "sc:cancel:conf-abc",
    ]
    assert captured[0]["message_type"] == "slash_confirm"
    assert "Reload MCP?" in captured[0]["text"]
    assert adapter._slash_confirm_state["conf-abc"] == "sess-1"


async def test_send_slash_confirm_without_title(monkeypatch):
    adapter = _make_adapter()
    captured = []
    async def fake_post(**kw):
        captured.append(kw)
        return {"id": 1}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send_slash_confirm(
        chat_id=1, title="", message="just the body",
        session_key="s", confirm_id="c",
    )
    assert captured[0]["text"] == "just the body"


async def test_slash_confirm_callback_resolves(monkeypatch):
    adapter = _make_adapter()
    resolved = []
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_slash_confirm",
        lambda sk, cid, choice: resolved.append((sk, cid, choice)),
    )
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    captured_patches = []
    async def fake_patch(mid, *, text=None, options=None, **kw):
        captured_patches.append({"message_id": mid, "text": text, "options": options})
        return {"id": mid}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    adapter._slash_confirm_state["conf-1"] = "sess-99"
    await adapter._handle_callback({
        "callback_data": "sc:once:conf-1",
        "user_id": "u_1",
        "message_id": 50,
        "chat_id": 1,
    })
    assert resolved == [("sess-99", "conf-1", "once")]
    assert "conf-1" not in adapter._slash_confirm_state
    # Bubble edited
    assert captured_patches[0]["text"].startswith("✅ Approved once")
    assert "u_1" in captured_patches[0]["text"]
    assert captured_patches[0]["options"] == []


async def test_stale_slash_confirm_click_noops(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_slash_confirm",
        lambda sk, cid, choice: pytest.fail("should not resolve stale click"),
    )
    # _slash_confirm_state is empty (stale or already-resolved click)
    await adapter._handle_callback({
        "callback_data": "sc:once:stale-id",
        "user_id": "u_1",
        "message_id": 50,
        "chat_id": 1,
    })
    # Implicit: nothing raised, no resolve called


# -----------------------------------------------------------------------------
# Task 5.1 — send_update_prompt() yes/no inline UI. Mirrors
# gateway/platforms/telegram.py:2006. Called by Hermes's gateway during
# stash-restore / config-migration flows. No adapter-side resolution state
# (unlike approvals / slash-confirms) — Hermes's gateway resolves callbacks
# itself by waiting on a future or sending a follow-up message.
# -----------------------------------------------------------------------------


async def test_send_update_prompt_renders_yes_no(monkeypatch):
    adapter = _make_adapter()
    posts = []
    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    result = await adapter.send_update_prompt(
        chat_id=1, prompt="Restore stashed config?",
    )
    assert result.success is True
    options = posts[0]["options"]
    callbacks = [o["callbackData"] for o in options]
    assert callbacks == ["update_prompt:y", "update_prompt:n"]
    assert "Restore stashed config?" in posts[0]["text"]
    assert "✓ Yes" in options[0]["text"]
    assert "✗ No" in options[1]["text"]


async def test_send_update_prompt_with_default_hint(monkeypatch):
    adapter = _make_adapter()
    posts = []
    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send_update_prompt(
        chat_id=1, prompt="Apply migration?", default_hint="no",
    )
    assert "default: no" in posts[0]["text"]


async def test_send_update_prompt_overrides_base():
    # Override-gate check used by Hermes's gateway: it probes
    # `type(adapter).send_update_prompt is BasePlatformAdapter.send_update_prompt`
    # to decide whether to render the yes/no UI or fall back to plain text.
    from hermes_channel_bgos.bgos_adapter import BGOSAdapter, BasePlatformAdapter
    assert BGOSAdapter.send_update_prompt is not BasePlatformAdapter.send_update_prompt


async def test_send_slash_confirm_overrides_base():
    # Same override-gate as send_update_prompt — verifies Hermes's gateway
    # will route slash-confirm requests to our 3-button override instead of
    # the base-class no-op that returns SendResult(success=False).
    from hermes_channel_bgos.bgos_adapter import BGOSAdapter, BasePlatformAdapter
    assert BGOSAdapter.send_slash_confirm is not BasePlatformAdapter.send_slash_confirm
