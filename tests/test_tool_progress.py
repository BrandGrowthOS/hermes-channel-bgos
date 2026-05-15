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
    assert out == [{
        "icon": "📖",
        "name": "read_file",
        "args": "/etc/hostname",
        "status": "done",
    }]


def test_parse_tool_progress_terminal_single_quoted():
    out = _parse_tool_progress_text("💻 terminal: 'echo hi'")
    assert out is not None and len(out) == 1
    entry = out[0]
    assert entry["icon"] == "💻"
    assert entry["name"] == "terminal"
    assert entry["args"] == "echo hi"


def test_parse_tool_progress_search_files_no_quotes():
    out = _parse_tool_progress_text("🔎 search_files approval|exec_approval")
    assert out is not None and len(out) == 1
    assert out[0]["name"] == "search_files"
    assert out[0]["args"] == "approval|exec_approval"


def test_parse_tool_progress_truncates_long_args():
    long_arg = "x" * 200
    out = _parse_tool_progress_text(f"💻 terminal: \"{long_arg}\"")
    assert out is not None and len(out) == 1
    assert "…" in out[0]["args"]
    assert len(out[0]["args"]) <= 120


def test_parse_tool_progress_multi_line_accumulation():
    """The gateway joins ALL accumulated tool lines with newlines and
    re-sends the full list on every edit_message (upstream
    gateway/run.py:14454). The parser MUST return all entries so the
    card stays in sync — earlier first-line-only behavior dropped every
    tool after the first."""
    text = (
        '📖 read_file: "/etc/os-release"\n'
        '💻 terminal: "df -h /"\n'
        '💻 terminal: "uptime"'
    )
    out = _parse_tool_progress_text(text)
    assert out is not None
    assert [e["name"] for e in out] == ["read_file", "terminal", "terminal"]
    assert [e["args"] for e in out] == ["/etc/os-release", "df -h /", "uptime"]


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


def test_parse_tool_progress_skips_non_matching_lines():
    """Lines that don't match the tool-progress shape are silently
    dropped (e.g. the gateway's "(×3)" dedup tail at
    gateway/run.py:14416). Matching entries are still authoritative."""
    out = _parse_tool_progress_text(
        '📖 read_file: "/etc/hostname"\n'
        "second line that is regular text\n"
        '💻 terminal: "ls"'
    )
    assert out is not None
    assert [e["name"] for e in out] == ["read_file", "terminal"]


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
    """Same agent turn, more tools → PATCH the existing card. The gateway
    re-sends the WHOLE accumulated tool list each edit, so the second
    edit's payload contains both tools."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        # First edit — only the read_file line so far.
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        # Second edit — gateway accumulates and re-sends BOTH lines.
        await adapter.edit_message(
            42, 500,
            '📖 read_file: "/etc/hostname"\n💻 terminal: "uname -a"',
        )

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
async def test_send_first_tool_posts_card(mock_bgos_server):
    """The gateway's progress loop calls `adapter.send()` for the FIRST
    tool of a turn (upstream gateway/run.py:14483-14488). The send()
    intercept must POST it as a tool_progress card just like
    edit_message would."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 7001})

    try:
        result = await adapter.send(42, '📖 read_file: "/etc/hostname"')

        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        assert len(posts) == 1
        body = posts[0].json_body
        assert body["messageType"] == "tool_progress"
        assert body["toolProgress"]["state"] == "running"
        assert [t["name"] for t in body["toolProgress"]["tools"]] == ["read_file"]
        # send() returns the card's message_id so the gateway's
        # `progress_msg_id` points at our card. Subsequent edit_messages
        # target the card directly.
        assert result.message_id == "7001"
        assert adapter._tool_progress_card_id_by_chat[42] == 7001
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_delete_message_finalizes_card_at_card_id(mock_bgos_server):
    """When the gateway deletes the CARD itself (because send() returned
    the card's id as the progress_msg_id), the adapter finalizes to
    state='done' WITHOUT issuing the actual DELETE — the card persists
    as the historical record."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 7001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/7001").respond(200, {"id": 7001})
    mock_bgos_server.on("DELETE", "/api/v1/messages/7001").respond(204, None)

    try:
        # Tool emitted via send() — card_id = 7001.
        await adapter.send(42, '📖 read_file: "/etc/hostname"')
        # Gateway end-of-turn — deletes what it thinks is the progress
        # bubble, which is the card itself.
        await adapter.delete_message(42, 7001)

        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/7001"]
        assert len(patches) == 1
        assert patches[0].json_body["toolProgress"]["state"] == "done"
        # CRITICAL: the card is NOT actually deleted.
        deletes = [r for r in mock_bgos_server.requests
                   if r.method == "DELETE" and r.path == "/api/v1/messages/7001"]
        assert len(deletes) == 0
        assert 42 not in adapter._tool_progress_card_id_by_chat
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
async def test_edit_message_replaces_not_appends(mock_bgos_server):
    """Each gateway edit_message carries the FULL accumulated tool list,
    so the adapter REPLACES tracked tools — no accumulation. Same line
    sent twice still yields one entry."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')

        assert len(adapter._tool_progress_tools_by_chat[42]) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_concurrent_first_tools_post_only_once(mock_bgos_server):
    """Two edit_message coroutines for the same chat racing in the SAME
    throttle window must NOT both POST a fresh card and orphan one.
    Reviewer flag 2026-05-15 — guard via per-chat asyncio.Lock.

    Note on contents: each gateway edit_message carries the FULL
    accumulated tool list (upstream gateway/run.py:14454), so the second
    call here sends a 2-tool payload. The adapter REPLACES tracked tools
    each call, so the final state has 2 tools — but there must still be
    exactly one POST (the lock prevents both racers from creating
    duplicate cards)."""
    import asyncio as _asyncio
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "user_abc"

    try:
        await _asyncio.gather(
            adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"'),
            adapter.edit_message(
                42, 500,
                '📖 read_file: "/etc/hostname"\n💻 terminal: "uname -a"',
            ),
        )

        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        # The key invariant: EXACTLY ONE POST. The second racer must
        # observe the first racer's card_id_by_chat entry under the
        # lock and PATCH instead.
        assert len(posts) == 1, f"expected one POST, got {len(posts)}"
        assert adapter._tool_progress_card_id_by_chat[42] == 9001
        # The final tool list depends on which racer landed last, but
        # since at least one of them carried both tools, the final
        # PATCH'd body has both — or the body of the lone POST has one.
        # Either way the card converges. We just verify no duplicate
        # POST was issued; tool-count accuracy is the gateway's
        # contract.
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


# ---------------------------------------------------------------------------
# userId fallback to pairing owner (0.6.2 fix — caught live 2026-05-15 when
# REST-backfill-seeded chats had no recorded per-chat user_id and tool-
# progress PATCHes returned 400 "userId should not be empty").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_falls_back_to_pairing_user_id(mock_bgos_server):
    """When no per-chat user_id is recorded (REST backfill seed, etc.),
    tool-progress PATCH must use the pairing owner's user_id so the
    backend's ownership check passes."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    # No last_user_id_by_chat entry for chat 42 — simulates a chat seeded
    # entirely from REST backfill (which carries no inbound user_id).
    adapter.pairing_user_id = "pairing_owner_user"

    try:
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        await adapter.edit_message(
            42, 500,
            '📖 read_file: "/etc/hostname"\n💻 terminal: "uname -a"',
        )

        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/9001"]
        assert len(patches) == 1
        assert patches[0].json_body["userId"] == "pairing_owner_user"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_per_chat_user_id_wins_over_pairing_user_id(mock_bgos_server):
    """When BOTH the per-chat recorded user_id AND the pairing owner are
    set, the per-chat one wins — it identifies the actual prompter,
    which is more specific and matches the legacy behavior."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter._state.last_user_id_by_chat[42] = "prompter_user"
    adapter.pairing_user_id = "pairing_owner_user"

    try:
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        await adapter.edit_message(
            42, 500,
            '📖 read_file: "/etc/hostname"\n💻 terminal: "uname -a"',
        )

        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/9001"]
        assert len(patches) == 1
        assert patches[0].json_body["userId"] == "prompter_user"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_finalize_card_falls_back_to_pairing_user_id(mock_bgos_server):
    """delete_message → _finalize_tool_progress_card must use the pairing
    owner as fallback when no per-chat user_id was recorded."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 9001})
    mock_bgos_server.on("PATCH", "/api/v1/messages/9001").respond(200, {"id": 9001})
    adapter._state.set_route(7, "default")
    adapter.pairing_user_id = "pairing_owner_user"

    try:
        # Seed a running card the same way the live adapter would.
        await adapter.edit_message(42, 500, '📖 read_file: "/etc/hostname"')
        # End-of-turn — the gateway tries to DELETE the preview, the adapter
        # intercepts and PATCHes the card to state=done instead.
        await adapter.delete_message(42, 500)

        patches = [r for r in mock_bgos_server.requests
                   if r.method == "PATCH" and r.path == "/api/v1/messages/9001"]
        assert len(patches) == 1
        body = patches[0].json_body
        assert body["userId"] == "pairing_owner_user"
        assert body["toolProgress"]["state"] == "done"
    finally:
        await adapter.disconnect()


def test_patch_user_id_helper_returns_none_when_neither_set():
    """If neither per-chat user_id nor pairing_user_id is set (no connect()
    has been called and no inbound has arrived), the helper returns None.
    PATCH still fails in that case — no worse than before this fix."""
    adapter = BGOSAdapter(BgosConfig(
        base_url="http://localhost:0", pairing_token="pair_xyz",
    ))
    # No state set — fresh adapter, no connect, no inbound.
    assert adapter._patch_user_id_for_chat(42) is None
