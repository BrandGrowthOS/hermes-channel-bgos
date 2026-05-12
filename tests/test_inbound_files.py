"""Tests for inbound file surfacing — when the user attaches files to a BGOS
message, the adapter inlines them into the agent-visible `text` so vision
models pick up images via markdown image syntax and other files render as
labeled links. See `_format_inbound_files` + `_handle_inbound`.
"""
from __future__ import annotations

import asyncio

import pytest

from hermes_channel_bgos.bgos_adapter import (
    BGOSAdapter,
    MessageEvent,
    _format_inbound_files,
)
from hermes_channel_bgos.config import BgosConfig


# ---------------------------------------------------------------------------
# Pure formatter unit tests.
# ---------------------------------------------------------------------------


def test_format_files_no_files_returns_text_unchanged():
    assert _format_inbound_files("hello", []) == "hello"


def test_format_files_image_renders_as_markdown_image():
    files = [
        {"filename": "shot.png", "mime": "image/png", "url": "https://x/shot.png"},
    ]
    out = _format_inbound_files("look at this", files)
    assert "look at this" in out
    assert "## Attachments from user" in out
    assert "![shot.png](https://x/shot.png)" in out


def test_format_files_document_renders_as_labeled_link():
    files = [
        {"filename": "spec.pdf", "mime": "application/pdf", "url": "https://x/spec.pdf"},
    ]
    out = _format_inbound_files("see attached", files)
    assert "[spec.pdf](https://x/spec.pdf)" in out
    assert "(application/pdf)" in out


def test_format_files_uses_data_uri_when_url_absent():
    files = [
        {
            "filename": "tiny.png",
            "mime": "image/png",
            "dataUri": "data:image/png;base64,AAAA",
        },
    ]
    out = _format_inbound_files("", files)
    assert "![tiny.png](data:image/png;base64,AAAA)" in out
    # Empty user text → placeholder header so agent knows files arrived.
    assert "[User attached 1 file(s)]" in out


def test_format_files_placeholder_for_missing_fetch_path():
    """Old backends might emit files without url or dataUri — surface a
    breadcrumb so the agent at least mentions the attachment instead of
    silently dropping it."""
    files = [{"filename": "x.png", "mime": "image/png"}]
    out = _format_inbound_files("", files)
    assert "x.png" in out
    assert "no fetch URL" in out


def test_format_files_handles_multiple_mixed():
    files = [
        {"filename": "a.png", "mime": "image/png", "url": "https://x/a.png"},
        {"filename": "b.pdf", "mime": "application/pdf", "url": "https://x/b.pdf"},
        {
            "filename": "c.jpg",
            "mime": "image/jpeg",
            "dataUri": "data:image/jpeg;base64,BBBB",
        },
    ]
    out = _format_inbound_files("here", files)
    assert "![a.png](https://x/a.png)" in out
    assert "[b.pdf](https://x/b.pdf)" in out
    assert "![c.jpg](data:image/jpeg;base64,BBBB)" in out


# ---------------------------------------------------------------------------
# Integration — _handle_inbound passes the formatted text through.
# ---------------------------------------------------------------------------


async def test_inbound_with_image_surfaces_markdown_to_agent(
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
            "inbound_message",
            {
                "chat_id": 11,
                "message_id": 600,
                "text": "what's in this picture?",
                "user_id": "user_1",
                "assistant_id": 7,
                "message_type": "standard",
                "files": [
                    {
                        "id": 99,
                        "filename": "selfie.jpg",
                        "mime": "image/jpeg",
                        "url": "https://bgos.test/selfie.jpg?sig=abc",
                    },
                ],
            },
        )
        await asyncio.sleep(0.2)

        assert len(handled) == 1
        event = handled[0]
        # Original user text preserved.
        assert "what's in this picture?" in event.text
        # Markdown image inlined so a vision model picks it up.
        assert "![selfie.jpg](https://bgos.test/selfie.jpg?sig=abc)" in event.text
        # Files list is still on the vendor MessageEvent for plugins that
        # want structured access (unused on the gateway-event path).
        assert len(event.files) == 1
        assert event.files[0]["filename"] == "selfie.jpg"

        # Retry cache picks up the augmented text so /retry replays the
        # full attachment context, not just the typed prompt.
        cached = adapter._state.last_user_text_by_chat[11]
        assert "selfie.jpg" in cached
    finally:
        await adapter.disconnect()


async def test_rest_backfill_surfaces_files(mock_bgos_server, monkeypatch):
    """The REST poll/backfill response includes a `files` array per message
    (since 2026-04-25 backend change). The same `_format_inbound_files`
    pipeline that handles WS push also handles backfilled messages, so
    images sent while WS was down still surface to the agent on the next
    poll tick.
    """
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200,
        {
            "messages": [
                {
                    "id": 700,
                    "message_id": 700,
                    "chat_id": 11,
                    "text": "look at this",
                    "user_id": "user_1",
                    "assistant_id": 7,
                    "message_type": "standard",
                    "files": [
                        {
                            "id": 50,
                            "filename": "photo.jpg",
                            "mime": "image/jpeg",
                            "url": "https://bgos.test/photo.jpg?sig=xyz",
                        },
                    ],
                },
            ],
        },
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    monkeypatch.setattr(adapter, "handle_message", lambda e: handled.append(e))

    await adapter.connect()
    try:
        # connect() schedules a first-connect backfill(0). Wait, then explicitly
        # call again so we hit the new endpoint shape with files.
        await asyncio.sleep(0.15)
        handled.clear()
        await adapter._run_backfill(699)

        assert len(handled) == 1
        ev = handled[0]
        assert "look at this" in ev.text
        assert "![photo.jpg](https://bgos.test/photo.jpg?sig=xyz)" in ev.text
    finally:
        await adapter.disconnect()


async def test_inbound_with_no_files_unchanged(mock_bgos_server, monkeypatch):
    """Regression guard: messages without files must not get the attachment
    suffix appended."""
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
        await mock_bgos_server.emit_to_room(
            "assistant:7",
            "inbound_message",
            {
                "chat_id": 11,
                "message_id": 601,
                "text": "just text",
                "user_id": "user_1",
                "assistant_id": 7,
                "message_type": "standard",
            },
        )
        # Text without files flows through adaptive batching (≤0.24s flush
        # for short text); wait past the window before asserting.
        await asyncio.sleep(0.35)
        assert len(handled) == 1
        assert handled[0].text == "just text"
        assert "Attachments" not in handled[0].text
    finally:
        await adapter.disconnect()
