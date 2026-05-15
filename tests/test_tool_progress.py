"""Tests for the tool_progress message-type emission path (Phase 2:
collapsible side-conversation-style card).

Coverage:
 - `_parse_tool_progress_text` recognizes the gateway's emoji-prefixed
   shapes (`📖 read_file: "/etc/hostname"`, `💻 terminal: 'echo …'`,
   `🔎 search_files "approval|exec_approval"`) and rejects regular text.
 - `edit_message` intercepts tool-progress edits and POSTs a new card on
   first tool, then PATCHes the same card on subsequent tools. The
   message type on the wire is `tool_progress` and the payload includes
   the `tool_progress` field with `state="running"` and the accumulated
   tools list.
 - `delete_message` finalizes the active card (PATCH state="done")
   before actually DELETEing the streaming preview.

Spec: docs/superpowers/specs/2026-05-15-tool-progress-message-type-design.md
"""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_adapter import (
    BGOSAdapter,
    _parse_tool_progress_text,
)
from hermes_channel_bgos.config import BgosConfig


# ---------------------------------------------------------------------------
# Parser unit tests — pure function, no adapter setup needed.
# ---------------------------------------------------------------------------


def test_parse_tool_progress_read_file_double_quoted():
    out = _parse_tool_progress_text('📖 read_file: "/etc/hostname"')
    assert out == {
        "icon": "📖",
        "name": "read_file",
        "args": "/etc/hostname",
        "status": "done",
    }


def test_parse_tool_progress_terminal_single_quoted():
    out = _parse_tool_progress_text("💻 terminal: 'echo hi'")
    assert out is not None
    assert out["icon"] == "💻"
    assert out["name"] == "terminal"
    assert out["args"] == "echo hi"


def test_parse_tool_progress_search_files_no_quotes():
    out = _parse_tool_progress_text("🔎 search_files approval|exec_approval")
    assert out is not None
    assert out["name"] == "search_files"
    assert out["args"] == "approval|exec_approval"


def test_parse_tool_progress_truncates_long_args():
    long_arg = "x" * 200
    out = _parse_tool_progress_text(f"💻 terminal: \"{long_arg}\"")
    assert out is not None
    # 117 chars + ellipsis (one codepoint) = 118-char string, but the
    # contract is "≤120 chars" — we just check the truncation marker is
    # present and the length didn't blow up.
    assert "…" in out["args"]
    assert len(out["args"]) <= 120


def test_parse_tool_progress_rejects_plain_text():
    assert _parse_tool_progress_text("Hostname is n8n-…") is None
    assert _parse_tool_progress_text("") is None
    assert _parse_tool_progress_text(None) is None
    # No emoji prefix → not a tool-progress line.
    assert _parse_tool_progress_text("read_file /etc/hostname") is None


def test_parse_tool_progress_rejects_non_emoji_unicode():
    """Regex tightened 2026-05-15 (reviewer flag): CJK ideographs, accented
    Latin, Arabic, etc. all sit outside the emoji Unicode ranges and must
    NOT be misclassified as tool-progress prefixes."""
    # CJK ideograph + ASCII word that looks like a tool name
    assert _parse_tool_progress_text("中文 message: hello") is None
    # Accented Latin
    assert _parse_tool_progress_text("é hello: world") is None
    # Arabic
    assert _parse_tool_progress_text("ا read_file: /etc/hostname") is None


def test_parse_tool_progress_first_line_only():
    # The gateway may stream multiple lines in one update; only the first
    # line should be parsed (so multi-line previews aren't misinterpreted).
    out = _parse_tool_progress_text(
        '📖 read_file: "/etc/hostname"\n'
        "second line that is regular text"
    )
    assert out is not None
    assert out["name"] == "read_file"


# ---------------------------------------------------------------------------
# edit_message / delete_message integration with a mock backend.
# ---------------------------------------------------------------------------


pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_edit_message_first_tool_posts_card(mock_bgos_server):
    """First tool-progress edit per turn POSTs a new tool_progress card."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')

        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        assert len(posts) == 1
        body = posts[0].json_body
        assert body["chatId"] == 42
        assert body["messageType"] == "tool_progress"
        assert body["toolProgress"]["state"] == "running"
        assert len(body["toolProgress"]["tools"]) == 1
        first = body["toolProgress"]["tools"][0]
        assert first["icon"] == "📖"
        assert first["name"] == "read_file"
        assert first["args"] == "/etc/hostname"
        assert first["status"] == "done"
        # Card id was tracked so delete_message can finalize it later.
        assert adapter._tool_progress_card_id_by_chat[42] == 9001
        assert adapter._tool_progress_preview_to_chat[500] == 42
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_edit_message_subsequent_tool_patches_card(mock_bgos_server):
    """Same agent turn, more tools → PATCH the existing card."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        await adapter.edit_message(42, 500, '💻 terminal: "uname -a"')

        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/9001"]
        assert len(patches) == 1
        body = patches[0].json_body
        assert body["toolProgress"]["state"] == "running"
        tools = body["toolProgress"]["tools"]
        assert [t["name"] for t in tools] == ["read_file", "terminal"]
        # userId must be sent on PATCH (backend's UpdateMessageDto requires it).
        assert body["userId"] == "user_abc"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_delete_message_finalizes_card(mock_bgos_server):
    """When the gateway deletes the streaming preview, the adapter PATCHes
    the active card to state='done' BEFORE issuing the actual DELETE."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    mock_bgos_server.on("DELETE", "/api/v1/messages/500").respond(204, None)
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        await adapter.delete_message(42, 500)

        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/9001"]
        # First patch finalizes the card (after the POST created it).
        assert len(patches) >= 1
        final_body = patches[-1].json_body
        assert final_body["toolProgress"]["state"] == "done"
        # The card-id tracking is cleared.
        assert 42 not in adapter._tool_progress_card_id_by_chat
        # The actual DELETE on the preview still fires.
        deletes = [r for r in mock_bgos_server.requests
                   if r.method == "DELETE" and r.path == "/api/v1/messages/500"]
        assert len(deletes) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_edit_message_dedups_same_tool(mock_bgos_server):
    """Throttle / retry sometimes resends the same tool line; the adapter
    must not append duplicate entries."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        # Same exact line again — must be a no-op on the tools list.
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')

        assert len(adapter._tool_progress_tools_by_chat[42]) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_concurrent_first_tools_post_only_once(mock_bgos_server):
    """Two edit_message coroutines for the same chat racing in the SAME
    throttle window must NOT both POST a fresh card and orphan one.
    Reviewer flag 2026-05-15 — guard via per-chat asyncio.Lock."""
    import asyncio as _asyncio
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    # Both POSTs would return id=9001 if we double-fired — the bug shows
    # up as TWO POSTs to /api/v1/messages instead of one POST + one PATCH.
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await _asyncio.gather(
            adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"'),
            adapter.edit_message(42, 500, '💻 terminal: "uname -a"'),
        )

        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/9001"]
        assert len(posts) == 1, f"expected one POST, got {len(posts)}"
        assert len(patches) == 1, f"expected one PATCH, got {len(patches)}"
        # Card tracking is in a single, stable place.
        assert adapter._tool_progress_card_id_by_chat[42] == 9001
        assert len(adapter._tool_progress_tools_by_chat[42]) == 2
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_disconnect_clears_tool_progress_state(mock_bgos_server):
    """Reviewer flag 2026-05-15: stale tracking across reconnects would
    cause a delete_message on the new session to finalize a ghost card
    from the previous session. disconnect() MUST clear the three dicts."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
    assert adapter._tool_progress_card_id_by_chat[42] == 9001
    assert adapter._tool_progress_tools_by_chat[42]
    assert adapter._tool_progress_preview_to_chat[500] == 42

    await adapter.disconnect()
    assert not adapter._tool_progress_card_id_by_chat
    assert not adapter._tool_progress_tools_by_chat
    assert not adapter._tool_progress_preview_to_chat
    assert not adapter._tool_progress_lock_by_chat


@pytest.mark.asyncio
async def test_edit_message_non_tool_text_falls_through(mock_bgos_server):
    """Regular streaming-edit text (no emoji prefix) must NOT create a card
    — it goes through the standard PATCH-on-the-preview path."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("PATCH", "/api/v1/messages/500").respond(200, {"id": 500})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await adapter.edit_message(42, 500, "Hostname is n8n-…sgp1-01. Linux VPS.")

        # The PATCH targets the preview, NOT a new tool_progress card.
        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/500"]
        assert len(patches) == 1
        # No tool_progress card was created.
        assert 42 not in adapter._tool_progress_card_id_by_chat
        # No POST to /messages either.
        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        assert len(posts) == 0
    finally:
        await adapter.disconnect()
