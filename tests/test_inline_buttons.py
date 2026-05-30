"""Tests for the [[BGOS_BUTTONS]] marker → inline-options rendering and the
inbound_click → synthetic MessageEvent click-delivery path.

Contract recap (see PLATFORM_HINTS["bgos"] + docs/bgos-agent-capabilities.md):
- Agent writes `[[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]` anywhere in the reply.
- Lines inside: `Label | value` (pipe-separated), one per line, max 6.
- Adapter strips the block and posts options + renderMode='inline'.
- User tap → backend emits inbound_click on assistant:<id> room → adapter
  synthesizes a user MessageEvent with text = clicked button label, dispatches
  to handle_message.
"""
from __future__ import annotations

import asyncio

import pytest

from hermes_channel_bgos.bgos_adapter import (
    BGOSAdapter,
    MessageEvent,
    _parse_buttons_block,
)
from hermes_channel_bgos.config import BgosConfig


# Module-level asyncio mark applies to the integration tests below. Parser
# unit tests are sync; pytest's asyncio plugin would warn on sync-marked-
# async if we applied the mark globally, so we mark the async block only.


# ---------------------------------------------------------------------------
# Parser unit tests — pure function, no adapter setup needed.
# ---------------------------------------------------------------------------


def test_parse_buttons_basic_three_options():
    content = (
        "Pick one of these:\n"
        "\n"
        "[[BGOS_BUTTONS]]\n"
        "- Option A | value_a\n"
        "- Option B | value_b\n"
        "- Option C | value_c\n"
        "[[/BGOS_BUTTONS]]\n"
    )
    text, options, mode = _parse_buttons_block(content)
    assert text == "Pick one of these:"
    assert options == [
        {"text": "Option A", "callbackData": "value_a"},
        {"text": "Option B", "callbackData": "value_b"},
        {"text": "Option C", "callbackData": "value_c"},
    ]
    assert mode == "inline"


def test_parse_buttons_no_block_returns_unchanged():
    content = "Just a plain reply, no buttons."
    text, options, mode = _parse_buttons_block(content)
    assert text == content
    assert options == []
    assert mode is None


def test_parse_buttons_without_bullets():
    content = (
        "Choose:\n"
        "[[BGOS_BUTTONS]]\n"
        "Foo | foo\n"
        "Bar | bar\n"
        "[[/BGOS_BUTTONS]]"
    )
    text, options, _ = _parse_buttons_block(content)
    assert text == "Choose:"
    assert [o["callbackData"] for o in options] == ["foo", "bar"]


def test_parse_buttons_case_insensitive_tags():
    content = (
        "[[bgos_buttons]]\n"
        "A | a\n"
        "[[/bgos_buttons]]"
    )
    _, options, _ = _parse_buttons_block(content)
    assert options == [{"text": "A", "callbackData": "a"}]


def test_parse_buttons_skips_malformed_lines():
    content = (
        "[[BGOS_BUTTONS]]\n"
        "Valid | valid_value\n"
        "missing pipe here\n"
        "   | only_value\n"
        "only_label |   \n"
        "Another | another_value\n"
        "[[/BGOS_BUTTONS]]"
    )
    _, options, _ = _parse_buttons_block(content)
    assert options == [
        {"text": "Valid", "callbackData": "valid_value"},
        {"text": "Another", "callbackData": "another_value"},
    ]


def test_parse_buttons_truncates_over_six():
    lines = "\n".join(f"Opt{i} | v{i}" for i in range(10))
    content = f"[[BGOS_BUTTONS]]\n{lines}\n[[/BGOS_BUTTONS]]"
    _, options, _ = _parse_buttons_block(content)
    assert len(options) == 6
    # First six kept, rest dropped.
    assert options[0]["callbackData"] == "v0"
    assert options[5]["callbackData"] == "v5"


def test_parse_buttons_modal_mode():
    content = (
        "[[BGOS_BUTTONS mode=modal]]\n"
        "Yes | yes\n"
        "No | no\n"
        "[[/BGOS_BUTTONS]]"
    )
    _, options, mode = _parse_buttons_block(content)
    assert mode == "modal"
    assert len(options) == 2


def test_parse_buttons_empty_block_produces_no_options():
    content = "Text before\n[[BGOS_BUTTONS]]\n\n[[/BGOS_BUTTONS]]\nText after"
    text, options, mode = _parse_buttons_block(content)
    assert options == []
    assert mode is None
    # Block was stripped even though empty.
    assert "[[BGOS_BUTTONS]]" not in text
    assert "[[/BGOS_BUTTONS]]" not in text


# ---------------------------------------------------------------------------
# Integration — adapter.send() posts the right body. Async from here down.
# asyncio_mode=auto in pyproject.toml auto-marks async tests, so no explicit
# pytestmark here.
# ---------------------------------------------------------------------------


async def test_send_with_buttons_posts_options_and_render_mode(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 900})

    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    # Server-authoritative chat addressing: send() requires chat 11 to have
    # been received inbound. Seed it as received.
    adapter._state.record_inbound_chat(11)
    try:
        await adapter.send(
            chat_id=11,
            content=(
                "Pick one:\n\n"
                "[[BGOS_BUTTONS]]\n"
                "- Yes | yes\n"
                "- No | no\n"
                "[[/BGOS_BUTTONS]]\n"
            ),
        )
    finally:
        await adapter.disconnect()

    req = mock_bgos_server.last_request("POST", "/api/v1/messages")
    body = req.json_body
    assert body["chatId"] == 11
    assert body["text"] == "Pick one:"
    assert body["sender"] == "assistant"
    assert body["messageType"] == "standard"
    assert body["renderMode"] == "inline"
    assert body["options"] == [
        {"text": "Yes", "callbackData": "yes"},
        {"text": "No", "callbackData": "no"},
    ]


async def test_send_plain_text_has_no_render_mode(mock_bgos_server):
    """When no buttons block is present, renderMode + options are omitted."""
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 901})

    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    # Server-authoritative chat addressing: send() requires chat 11 to have
    # been received inbound. Seed it as received.
    adapter._state.record_inbound_chat(11)
    try:
        await adapter.send(chat_id=11, content="Just a plain reply.")
    finally:
        await adapter.disconnect()

    req = mock_bgos_server.last_request("POST", "/api/v1/messages")
    body = req.json_body
    assert body["text"] == "Just a plain reply."
    assert "renderMode" not in body
    assert "options" not in body


# ---------------------------------------------------------------------------
# Inbound click → synthetic MessageEvent → handle_message.
# ---------------------------------------------------------------------------


async def test_inbound_click_synthesizes_message_event(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )

    async def fake_handle(event):
        handled.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        await mock_bgos_server.emit_to_room(
            "assistant:7",
            "inbound_click",
            {
                "assistantId": 7,
                "userId": "user_1",
                "chatId": 11,
                "messageId": 500,
                "optionId": 17,
                "callbackData": "yes",
                "buttonText": "Yes",
            },
        )
        await asyncio.sleep(0.2)

        assert len(handled) == 1
        event = handled[0]
        assert event.platform == "bgos"
        assert event.chat_id == 11
        assert event.message_id == 500
        assert event.user_id == "user_1"
        assert event.assistant_id == 7
        assert event.agent_route == "hades"
        # Agent sees the button's visible LABEL as the user's reply.
        assert event.text == "Yes"
    finally:
        await adapter.disconnect()


async def test_inbound_click_custom_sentinel_uses_custom_text(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )

    async def fake_handle(event):
        handled.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)
        await mock_bgos_server.emit_to_room(
            "assistant:7",
            "inbound_click",
            {
                "assistantId": 7,
                "userId": "user_1",
                "chatId": 11,
                "messageId": 501,
                "optionId": None,
                "callbackData": "__custom__",
                "buttonText": "(custom)",
                "customText": "I typed this myself",
            },
        )
        await asyncio.sleep(0.2)
        assert len(handled) == 1
        # Custom sentinel means we prefer the typed text over the button text.
        assert handled[0].text == "I typed this myself"
    finally:
        await adapter.disconnect()


async def test_inbound_click_for_unknown_assistant_is_dropped(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    monkeypatch.setattr(adapter, "handle_message", lambda e: handled.append(e))

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)
        # Emit to the bound room but claim a different assistantId in the payload —
        # adapter should drop because that id isn't in its route map.
        await mock_bgos_server.emit_to_room(
            "assistant:7",
            "inbound_click",
            {
                "assistantId": 999,  # unknown
                "userId": "user_1",
                "chatId": 11,
                "messageId": 502,
                "optionId": 17,
                "callbackData": "yes",
                "buttonText": "Yes",
            },
        )
        await asyncio.sleep(0.2)
        assert handled == []
    finally:
        await adapter.disconnect()
