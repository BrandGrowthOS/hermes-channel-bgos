"""Tests for the agent-facing `[[BGOS_EVENT]]` event-card marker.

Covers the outbound eventMeta passthrough (Gap 1): the block posts
messageType="event" with the JSON object passed VERBATIM as `eventMeta`,
malformed/invalid blocks are rejected with a clear error (and nothing is
POSTed), and plain messages are unaffected. Also covers the standalone
(cron / out-of-process) send path's parity.
"""
from __future__ import annotations

import json

import pytest

from hermes_channel_bgos.bgos_adapter import (
    BGOSAdapter,
    InvalidEventMeta,
    _parse_event_block,
)
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio

EVENT_META = {
    "source": "agent",
    "title": "Sleep logged",
    "peek": "7h 40m",
    "payload": {"kind": "health_tracker_card", "hours": 7.67, "quality": "good"},
}


def _event_block(meta: dict) -> str:
    return "[[BGOS_EVENT]]\n" + json.dumps(meta) + "\n[[/BGOS_EVENT]]"


async def _connected_adapter(mock_bgos_server) -> BGOSAdapter:
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    # Server-authoritative addressing: seed chat 11 as received inbound.
    adapter._state.record_inbound_chat(11)
    return adapter


# -----------------------------------------------------------------------------
# Adapter send() path
# -----------------------------------------------------------------------------


async def test_send_event_block_posts_event_message_with_event_meta(mock_bgos_server):
    """The block is stripped from the visible text; messageType flips to
    "event" and the JSON object lands VERBATIM as `eventMeta` in the POST
    body (payload untouched, extra keys preserved)."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 300})
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        meta = dict(EVENT_META, extraKey="kept-verbatim")
        result = await adapter.send(
            chat_id=11, content="Here is your sleep card.\n\n" + _event_block(meta),
        )
        assert result.success is True
        assert result.message_id == "300"

        body = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert body["messageType"] == "event"
        assert body["eventMeta"] == meta
        assert body["eventMeta"]["payload"] == EVENT_META["payload"]
        assert body["text"] == "Here is your sleep card."
        assert "[[BGOS_EVENT]]" not in body["text"]
    finally:
        await adapter.disconnect()


async def test_send_event_block_only_falls_back_to_title_text(mock_bgos_server):
    """A block-only reply still posts a non-empty legacy `text` (the title)
    so older clients that don't render cards show something."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 301})
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.send(chat_id=11, content=_event_block(EVENT_META))
        assert result.success is True

        body = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert body["messageType"] == "event"
        assert body["eventMeta"] == EVENT_META
        assert body["text"] == "Sleep logged"
    finally:
        await adapter.disconnect()


async def test_send_event_block_missing_title_rejected(mock_bgos_server):
    """Validation failure -> SendResult(success=False) with a clear error;
    nothing is POSTed (no marker text leaks into the chat)."""
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.send(
            chat_id=11,
            content=_event_block({"source": "agent", "payload": {"kind": "x"}}),
        )
        assert result.success is False
        assert "invalid_event_meta" in result.error
        assert "title" in result.error
        posted = [
            r for r in mock_bgos_server.requests
            if r.method == "POST" and r.path == "/api/v1/messages"
        ]
        assert posted == []
    finally:
        await adapter.disconnect()


async def test_send_event_block_bad_json_rejected(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.send(
            chat_id=11,
            content="[[BGOS_EVENT]]{not valid json}[[/BGOS_EVENT]]",
        )
        assert result.success is False
        assert "invalid_event_meta" in result.error
        posted = [
            r for r in mock_bgos_server.requests
            if r.method == "POST" and r.path == "/api/v1/messages"
        ]
        assert posted == []
    finally:
        await adapter.disconnect()


async def test_send_plain_message_unaffected(mock_bgos_server):
    """No block -> messageType stays "standard" and no eventMeta key at all."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 302})
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.send(chat_id=11, content="just a normal reply")
        assert result.success is True

        body = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert body["messageType"] == "standard"
        assert "eventMeta" not in body
        assert body["text"] == "just a normal reply"
    finally:
        await adapter.disconnect()


# -----------------------------------------------------------------------------
# Parser unit behavior
# -----------------------------------------------------------------------------


async def test_parse_event_block_returns_meta_verbatim():
    text = "before\n" + _event_block(EVENT_META) + "\nafter"
    cleaned, meta = _parse_event_block(text)
    assert meta == EVENT_META
    # The payload object is passed through untouched (never mutated).
    assert meta["payload"] == {
        "kind": "health_tracker_card", "hours": 7.67, "quality": "good",
    }
    assert "BGOS_EVENT" not in cleaned
    assert "before" in cleaned and "after" in cleaned


async def test_parse_event_block_no_block_is_noop():
    cleaned, meta = _parse_event_block("plain text, no marker")
    assert cleaned == "plain text, no marker"
    assert meta is None


async def test_parse_event_block_rejects_empty_source():
    with pytest.raises(InvalidEventMeta, match="source"):
        _parse_event_block(_event_block({"source": "  ", "title": "T"}))


async def test_parse_event_block_rejects_non_string_peek():
    with pytest.raises(InvalidEventMeta, match="peek"):
        _parse_event_block(
            _event_block({"source": "agent", "title": "T", "peek": 5}),
        )


async def test_parse_event_block_rejects_non_object_payload():
    with pytest.raises(InvalidEventMeta, match="payload"):
        _parse_event_block(
            _event_block({"source": "agent", "title": "T", "payload": [1, 2]}),
        )


async def test_parse_event_block_rejects_unclosed_block():
    with pytest.raises(InvalidEventMeta, match="malformed"):
        _parse_event_block('[[BGOS_EVENT]]{"source": "agent", "title": "T"}')


# -----------------------------------------------------------------------------
# Standalone (cron / out-of-process) send path parity
# -----------------------------------------------------------------------------


async def test_standalone_send_event_block_passes_event_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    from hermes_channel_bgos import plugin as plugin_mod

    captured = {}

    class FakeApi:
        def __init__(self, config):
            pass

        async def post_message(self, *, chat_id, text, **kw):
            captured["chat_id"] = chat_id
            captured["text"] = text
            captured["kw"] = kw
            return {"id": 4321}

        async def close(self):
            pass

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(
        None, "830", "Card below.\n" + _event_block(EVENT_META),
    )
    assert result["success"] is True
    assert captured["text"] == "Card below."
    assert captured["kw"]["message_type"] == "event"
    assert captured["kw"]["event_meta"] == EVENT_META


async def test_standalone_send_invalid_event_block_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    from hermes_channel_bgos import plugin as plugin_mod

    class FakeApi:
        def __init__(self, config):
            pass

        async def post_message(self, **kw):  # pragma: no cover - must not run
            raise AssertionError("must not POST an invalid event block")

        async def close(self):
            pass

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(
        None, "830", _event_block({"title": "no source here"}),
    )
    assert "error" in result
    assert "invalid_event_meta" in result["error"]
