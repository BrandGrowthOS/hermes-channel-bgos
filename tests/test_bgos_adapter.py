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
from tests.mocks.mock_hermes import BasePlatformAdapter


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
        # SendResult.message_id is str (matches fork's type), SendResult.success
        # is the fork's field name (not `ok` as earlier drafts used).
        assert result.message_id == "300"
        assert result.success is True

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


# -----------------------------------------------------------------------------
# edit_message override (Task 1.3) — what UNLOCKS Hermes's gateway-driven
# tool-progress / streaming / typing UX (the gateway probes
# `type(adapter).edit_message is BasePlatformAdapter.edit_message` and
# short-circuits everything when the adapter inherits the base default).
# -----------------------------------------------------------------------------


def _make_adapter() -> BGOSAdapter:
    """Lightweight adapter for unit-testing edit/delete/typing without
    spinning up the mock backend. Tests monkeypatch the api / ws fields."""
    return BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))


async def test_edit_message_calls_patch_message(monkeypatch):
    adapter = _make_adapter()
    captured: dict = {}

    async def fake_patch(message_id, *, text=None, options=None, render_mode=None,
                        approval_meta=None):
        captured["message_id"] = message_id
        captured["text"] = text
        captured["options"] = options
        captured["render_mode"] = render_mode
        return {"id": message_id}

    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)

    result = await adapter.edit_message(chat_id=11, message_id=300, content="updated")

    assert result.success is True
    assert result.message_id == "300"
    assert captured["message_id"] == 300
    assert captured["text"] == "updated"


async def test_edit_message_returns_failure_on_404(monkeypatch):
    """404 / 410 / 4xx means the message is too old or has been deleted —
    return SendResult(success=False) so the gateway falls back to a
    fresh send() instead of crashing."""
    adapter = _make_adapter()

    async def boom(message_id, **_kwargs):
        raise BgosApiError(404, "MESSAGE_NOT_FOUND", {"error": "MESSAGE_NOT_FOUND"})

    monkeypatch.setattr(adapter._api, "patch_message", boom)

    result = await adapter.edit_message(chat_id=11, message_id=300, content="updated")

    assert result.success is False
    assert result.error == "not_editable_404"


async def test_edit_message_parses_buttons_block(monkeypatch):
    """Same [[BGOS_BUTTONS]] marker block as send() — text is stripped,
    options become the keyboard payload on the PATCH."""
    adapter = _make_adapter()
    captured: dict = {}

    async def fake_patch(message_id, *, text=None, options=None, render_mode=None,
                        approval_meta=None):
        captured["text"] = text
        captured["options"] = options
        captured["render_mode"] = render_mode
        return {"id": message_id}

    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)

    content = "Please pick one:\n[[BGOS_BUTTONS]]\nYes | yes\nNo | no\n[[/BGOS_BUTTONS]]"
    await adapter.edit_message(chat_id=11, message_id=300, content=content)

    assert captured["text"] == "Please pick one:"
    assert captured["options"] == [
        {"text": "Yes", "callbackData": "yes"},
        {"text": "No",  "callbackData": "no"},
    ]
    assert captured["render_mode"] == "inline"


async def test_edit_message_drops_options_when_block_absent(monkeypatch):
    """No buttons marker → patch with options=[] so the backend CLEARS
    any prior keyboard. Necessary for the streaming pattern where the
    first send carried buttons and subsequent edits are plain text."""
    adapter = _make_adapter()
    captured: dict = {}

    async def fake_patch(message_id, *, text=None, options=None, render_mode=None,
                        approval_meta=None):
        captured["text"] = text
        captured["options"] = options
        captured["render_mode"] = render_mode
        return {"id": message_id}

    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)

    await adapter.edit_message(chat_id=11, message_id=300, content="plain text")

    assert captured["text"] == "plain text"
    assert captured["options"] == []
    assert captured["render_mode"] is None


async def test_edit_message_overrides_base_unlocks_tool_progress():
    """This is THE check the Hermes gateway makes at gateway/run.py:14370
    to decide whether to drive the tool-progress / streaming / typing UI.
    If this fails, the entire feature degrades to plain send()."""
    assert BGOSAdapter.edit_message is not BasePlatformAdapter.edit_message
