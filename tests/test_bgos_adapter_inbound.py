"""Tests for BGOSAdapter's inbound pipeline (Task 5).

`inbound_message` WS events → MessageEvent → self.handle_message(event).
Also verifies the reconnect backfill path: on WS reconnect, fetch_inbound_since
is called with the last seen message_id, and each returned message is fed
through the same translation path.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter, MessageEvent
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


def _make_adapter() -> BGOSAdapter:
    """Lightweight adapter for unit-testing inbound paths without spinning
    up the mock backend. Tests monkeypatch handle_message / api / ws."""
    return BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))


async def test_inbound_message_translates_and_handles(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # Wait for WS connection + room join to settle
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        await mock_bgos_server.emit_to_room(
            "assistant:7",
            "inbound_message",
            {
                "chat_id": 11, "message_id": 200, "text": "hello world",
                "user_id": "user_1", "assistant_id": 7, "message_type": "standard",
            },
        )
        # Standard text now flows through adaptive batching — short texts
        # flush after ≤0.24s, so we wait past that window.
        await asyncio.sleep(0.35)

        assert len(handled) == 1
        event = handled[0]
        assert event.platform == "bgos"
        assert event.chat_id == 11
        assert event.message_id == 200
        assert event.user_id == "user_1"
        assert event.assistant_id == 7
        assert event.agent_route == "hades"
        assert event.text == "hello world"
        assert event.files == []
        assert event.message_type == "standard"
        assert event.command_name is None
        assert event.command_args is None

        # Retry cache populated
        assert adapter._state.last_user_text_by_chat[11] == "hello world"
    finally:
        await adapter.disconnect()


async def test_inbound_for_unknown_assistant_is_dropped(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # Deliver inbound directly (bypass room routing) with unknown assistant
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 201, "text": "ignored",
            "user_id": "u", "assistant_id": 999, "message_type": "standard",
        })
        assert handled == []
    finally:
        await adapter.disconnect()


async def test_inbound_carries_command_metadata_for_slash_commands(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 500, "text": "/help",
            "user_id": "u", "assistant_id": 7,
            "message_type": "slash_command", "command_name": "help", "command_args": "",
        })
        assert len(handled) == 1
        ev = handled[0]
        assert ev.message_type == "slash_command"
        assert ev.command_name == "help"
        assert ev.command_args == ""
    finally:
        await adapter.disconnect()


async def test_reconnect_triggers_backfill(mock_bgos_server, monkeypatch):
    """After reconnect the adapter calls fetch_inbound_since(last_id) and
    feeds each returned message through the translation pipeline."""
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {
            "messages": [
                {"chat_id": 11, "message_id": 300, "text": "while-you-were-out-1",
                 "user_id": "u", "assistant_id": 7, "message_type": "standard"},
                {"chat_id": 11, "message_id": 301, "text": "while-you-were-out-2",
                 "user_id": "u", "assistant_id": 7, "message_type": "standard"},
            ],
        },
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # connect() schedules a first-connect backfill(0); drain and reset
        # so we're only asserting against the explicit reconnect call.
        await asyncio.sleep(0.15)
        handled.clear()

        # Simulate reconnect: fire the _on_reconnect hook directly (in
        # production the WS client invokes it after a successful re-handshake)
        await adapter._run_backfill(250)

        assert [ev.message_id for ev in handled] == [300, 301]
        assert [ev.text for ev in handled] == [
            "while-you-were-out-1",
            "while-you-were-out-2",
        ]

        last_inbound = [
            r for r in mock_bgos_server.requests
            if r.method == "GET" and r.path == "/api/v1/integrations/inbound"
        ][-1]
        assert last_inbound.query["since_message_id"] == "250"
    finally:
        await adapter.disconnect()


async def test_reconnect_with_empty_backfill_noop(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {"messages": []},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # connect() fires a first-connect backfill using the cursor from
        # $HERMES_HOME/bgos_last_id. The autouse _isolate_hermes_home
        # fixture ensures that file starts absent → cursor=0. Mock returns
        # empty → handled stays []. Drain any scheduled task before
        # the explicit call.
        await asyncio.sleep(0.1)
        await adapter._run_backfill(100)
        assert handled == []
    finally:
        await adapter.disconnect()


async def test_last_id_persists_across_connects(mock_bgos_server, monkeypatch, tmp_path):
    """bgos_last_id file advances as messages are processed; subsequent
    connect() uses the saved cursor, not 0 — preventing history replay
    on every Hermes restart."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {"messages": []},
    )

    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    async def _noop(event): return None
    monkeypatch.setattr(adapter, "handle_message", _noop)

    await adapter.connect()
    try:
        await asyncio.sleep(0.1)
        # Simulate processing of a high-id message (as would happen on WS)
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 9999, "text": "hi",
            "user_id": "u", "assistant_id": 7, "message_type": "standard",
        })
        # File should now contain 9999
        last_id_file = tmp_path / "hermes_home" / "bgos_last_id"
        assert last_id_file.read_text().strip() == "9999"
    finally:
        await adapter.disconnect()

    # A fresh adapter with the same HERMES_HOME should load cursor=9999
    adapter2 = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    assert adapter2._load_last_id() == 9999

    # Older message ids don't regress the cursor
    adapter2._save_last_id(100)
    assert adapter2._load_last_id() == 9999


# -----------------------------------------------------------------------------
# Adaptive text batching (Chunk 4). Rapid successive plain-text fragments
# from the same chat coalesce into one merged dispatch so the agent doesn't
# emit N separate replies for a mobile-split message. Mirrors Telegram at
# gateway/platforms/telegram.py:3803-3859.
# -----------------------------------------------------------------------------


async def test_rapid_text_messages_are_batched():
    """Three rapid messages in the same chat → one merged dispatch."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 0.05  # speed up tests
    received: list[str] = []

    async def capture(event):
        # Both flat MessageEvent and gateway MessageEvent expose .text
        received.append(event.text)

    adapter.handle_message = capture
    for i, piece in enumerate(("part one. ", "part two. ", "part three.")):
        await adapter._handle_inbound({
            "assistant_id": 7,
            "chat_id": 42,
            "message_id": 1000 + i,
            "user_id": "u",
            "text": piece,
            "files": [],
            "message_type": "standard",
        })
    # Wait past the adaptive flush window for short messages (~0.05s)
    await asyncio.sleep(0.15)
    assert len(received) == 1
    assert received[0] == "part one. part two. part three."


async def test_slash_command_flushes_immediately():
    """Slash commands bypass batching so /new, /retry land in order."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 5.0  # would otherwise block 5s
    received: list[str] = []

    async def capture(event):
        received.append(event.text)

    adapter.handle_message = capture
    # Use a non-bridge-local slash command so the forwarding path runs
    # (where slash commands flow to the agent rather than short-circuiting).
    await adapter._handle_inbound({
        "assistant_id": 7,
        "chat_id": 42,
        "message_id": 1,
        "user_id": "u",
        "text": "/help",
        "files": [],
        "message_type": "slash_command",
        "command_name": "help",
        "command_args": "",
    })
    # No need to wait — slash commands flush synchronously
    assert len(received) == 1


async def test_messages_with_files_bypass_batching():
    """Attachments must flush immediately so the user sees the image-
    plus-caption interleaved correctly."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 5.0
    received: list[Any] = []

    async def capture(event):
        received.append(event)

    adapter.handle_message = capture
    await adapter._handle_inbound({
        "assistant_id": 7,
        "chat_id": 42,
        "message_id": 1,
        "user_id": "u",
        "text": "see this",
        "files": [{"filename": "a.png", "mime": "image/png", "url": "https://x"}],
        "message_type": "standard",
    })
    # No wait
    assert len(received) == 1


async def test_wait_for_reply_ws_echo_is_dropped_after_cursor_advance(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    adapter = _make_adapter()
    adapter._state.set_route(885, "default")
    adapter._record_consumed_peer_wait_reply({
        "conversationId": 73,
        "sideThreadChatId": 953,
        "sentMessageId": 8868,
        "returnedReplyMessageId": 8870,
        "targetAssistantId": 894,
    })
    received: list[Any] = []
    async def capture(event):
        received.append(event)
    adapter.handle_message = capture
    await adapter._handle_inbound({
        "assistantId": 885,
        "chatId": 953,
        "messageId": 8870,
        "userId": "user_1",
        "text": "Athena already replied through waitForReply",
        "messageType": "standard",
        "replyToId": 8868,
        "peerConversationId": 73,
        "turnState": "expecting_reply",
    })
    assert received == []
    assert (tmp_path / "hermes_home" / "bgos_last_id").read_text().strip() == "8870"
async def test_wait_for_reply_poll_echo_is_dropped_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    first = _make_adapter()
    first._record_consumed_peer_wait_reply({
        "conversationId": 73,
        "sideThreadChatId": 953,
        "sentMessageId": 8868,
        "returnedReplyMessageId": 8870,
        "targetAssistantId": 894,
    })
    adapter = _make_adapter()
    adapter._state.set_route(885, "default")
    received: list[Any] = []
    async def capture(event):
        received.append(event)
    adapter.handle_message = capture
    await adapter._handle_inbound({
        "assistant_id": 885,
        "chat_id": 953,
        "message_id": 8870,
        "user_id": "user_1",
        "text": "replayed reply",
        "message_type": "standard",
        "reply_to_id": 8868,
        "peer_conversation_id": 73,
    }, batchable=False)
    assert received == []
async def test_wait_for_reply_false_peer_reply_still_delivers():
    adapter = _make_adapter()
    adapter._state.set_route(885, "default")
    received: list[Any] = []
    async def capture(event):
        received.append(event)
    adapter.handle_message = capture
    await adapter._handle_inbound({
        "assistant_id": 885,
        "chat_id": 953,
        "message_id": 8871,
        "user_id": "user_1",
        "text": "new async peer turn",
        "message_type": "standard",
        "reply_to_id": 8868,
        "peer_conversation_id": 73,
    }, batchable=False)
    assert len(received) == 1
    assert received[0].text == "new async peer turn"
async def test_different_chats_batched_independently():
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 0.05
    received: list[tuple[int, str]] = []

    async def capture(event):
        # Pull chat_id from either source (gateway event) or flat event
        chat_id = (
            int(event.source.chat_id)
            if hasattr(event, "source") and getattr(event.source, "chat_id", None)
            else getattr(event, "chat_id", None)
        )
        received.append((chat_id, event.text))

    adapter.handle_message = capture
    # Two interleaved fragments per chat
    for chat_id, mid in [(1, 100), (2, 200), (1, 101), (2, 201)]:
        await adapter._handle_inbound({
            "assistant_id": 7,
            "chat_id": chat_id,
            "message_id": mid,
            "user_id": "u",
            "text": "frag ",
            "files": [],
            "message_type": "standard",
        })
    await asyncio.sleep(0.15)
    # 2 chats each get 1 dispatch with merged text
    assert len(received) == 2
    by_chat = dict(received)
    assert by_chat[1] == "frag frag "
    assert by_chat[2] == "frag frag "


async def test_disconnect_cancels_pending_text_batches():
    """Pending flush tasks must be cancelled cleanly on disconnect."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 5.0  # long, so task definitely pending

    async def capture(event):
        pass

    adapter.handle_message = capture
    await adapter._handle_inbound({
        "assistant_id": 7,
        "chat_id": 42,
        "message_id": 1,
        "user_id": "u",
        "text": "queued",
        "files": [],
        "message_type": "standard",
    })
    assert 42 in adapter._pending_text_tasks
    pending = adapter._pending_text_tasks[42]
    assert not pending.done()
    await adapter.disconnect()
    assert pending.done()
    assert adapter._pending_text_batches == {}
    assert adapter._pending_text_tasks == {}


async def test_retry_cache_uses_merged_text():
    """After batching merges several fragments, /retry should replay
    the merged text — not just the last fragment."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 0.05

    async def capture(event):
        pass

    adapter.handle_message = capture
    for i, piece in enumerate(("alpha ", "bravo ", "charlie")):
        await adapter._handle_inbound({
            "assistant_id": 7,
            "chat_id": 42,
            "message_id": 1000 + i,
            "user_id": "u",
            "text": piece,
            "files": [],
            "message_type": "standard",
        })
    await asyncio.sleep(0.15)
    assert adapter._state.last_user_text_by_chat[42] == "alpha bravo charlie"


# -----------------------------------------------------------------------------
# camelCase inbound payloads (0.5.3). The BGOS backend's WS inbound_message
# event uses camelCase keys (assistantId, chatId, messageId, messageType)
# while the REST `/api/v1/integrations/inbound` endpoint still returns
# snake_case. The adapter has to absorb both. Caught live on kc's server
# 2026-05-13 when every WS message was silently dropped at the
# `assistant_id is None` check and only the REST poll loop's 5-second
# fallback was delivering anything.
# -----------------------------------------------------------------------------


async def test_camelcase_inbound_dispatches():
    """A WS-shape camelCase payload must reach handle_message just like
    snake_case does. Concretely: assistantId / chatId / messageId /
    userId / messageType — exactly the keys the live backend emits."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    received: list[Any] = []

    async def capture(event):
        received.append(event)

    adapter.handle_message = capture
    adapter._text_batch_window = 0.05  # speed up batching flush

    await adapter._handle_inbound({
        "assistantId": 7,
        "userId": "user_42",
        "chatId": 830,
        "messageId": 6041,
        "text": "test 1",
        "files": [],
        "messageType": "standard",
    })
    await asyncio.sleep(0.15)
    assert len(received) == 1
    assert received[0].text == "test 1"


async def test_snake_case_takes_precedence_over_camel_alias():
    """If a payload contains BOTH (e.g. an over-eager translator upstream),
    the snake_case value wins. Guards against any future normalization
    in the backend that double-emits keys."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    received: list[Any] = []

    async def capture(event):
        received.append(event)

    adapter.handle_message = capture
    adapter._text_batch_window = 0.05

    await adapter._handle_inbound({
        "assistant_id": 7,           # winner
        "assistantId": 999,          # ignored (would route to unknown assistant)
        "chat_id": 100,              # winner
        "chatId": 200,               # ignored
        "message_id": 5000,          # winner
        "messageId": 5001,           # ignored
        "user_id": "real_user",
        "userId": "wrong_user",
        "text": "ok",
        "files": [],
        "message_type": "standard",
        "messageType": "approval_request",  # ignored
    })
    await asyncio.sleep(0.15)
    assert len(received) == 1
    # If the camelCase had won, this would have routed to assistant 999
    # (unknown) and dropped → received would be empty.


async def test_camelcase_slash_command_routes_to_bridge_local(mock_bgos_server):
    """Slash commands also have to honor the camelCase shape, since the
    same WS event channel delivers them. Use a bridge-local command
    (/status) so we can verify routing without needing an agent."""
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 999})
    adapter._state.set_route(7, "default")

    try:
        await adapter._handle_inbound({
            "assistantId": 7,
            "userId": "u",
            "chatId": 42,
            "messageId": 100,
            "text": "/status",
            "files": [],
            "messageType": "slash_command",
            "commandName": "status",
            "commandArgs": "",
        })
        # Bridge-local /status posts an ack via POST /api/v1/messages
        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        assert len(posts) == 1
        body = posts[0].json_body
        assert body["chatId"] == 42
        assert "BGOS adapter status" in body["text"]
    finally:
        # Close the httpx client so the test doesn't leak the socket
        # into the next test (which would trip pytest -W error on an
        # unraisable ResourceWarning).
        await adapter.disconnect()


async def test_bridge_local_slash_command_advances_cursor(mock_bgos_server, tmp_path, monkeypatch):
    """Regression for the 0.5.7 hotfix: bridge-local slash commands
    (/new, /status, /retry, /resume, /help) must advance the inbound
    cursor BEFORE the bridge-local short-circuit returns.

    Live-server symptom caught 2026-05-15: a single /new produced 50+
    "Conversation reset" acks. Root cause: cursor wasn't saved on the
    bridge-local branch, so the REST poll re-fetched the same /new from
    `inbound?since_message_id=<stale>` every 5s and re-dispatched it
    forever. The fix moves _save_last_id ahead of the slash-command
    routing block so every accepted event advances the cursor."""
    # _save_last_id persists to ~/.hermes/bgos_last_id by default; pin
    # it to an isolated tmpdir so the test (a) doesn't pollute the dev
    # machine's real cursor and (b) starts from a known clean baseline.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 999})
    adapter._state.set_route(7, "default")

    try:
        # Cursor file doesn't exist yet → reads as 0.
        assert adapter._load_last_id() == 0

        await adapter._handle_inbound({
            "assistant_id": 7,
            "user_id": "u",
            "chat_id": 42,
            "message_id": 500,
            "text": "/new",
            "files": [],
            "message_type": "slash_command",
            "command_name": "new",
            "command_args": "",
        })

        # The cursor MUST advance, otherwise the REST poll's
        # since_message_id stays at 0 and re-fetches /new on every
        # 5s tick → infinite "Conversation reset" loop.
        assert adapter._load_last_id() == 500
    finally:
        await adapter.disconnect()


async def test_camelcase_files_inbound():
    """Inbound camelCase with attachments. files[] payload elements are
    NOT touched by _normalize_inbound_payload (file entries use their own
    field names like fileName/fileMimeType handled by _format_inbound_files
    separately); we only verify the top-level keys translate."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    received: list[Any] = []

    async def capture(event):
        received.append(event)

    adapter.handle_message = capture

    await adapter._handle_inbound({
        "assistantId": 7,
        "userId": "u",
        "chatId": 42,
        "messageId": 100,
        "text": "look at this",
        "files": [{"filename": "a.png", "mime": "image/png",
                   "url": "https://s3.example/a.png"}],
        "messageType": "standard",
    })
    # File-bearing messages bypass batching → flush immediate, no sleep
    assert len(received) == 1


async def test_normalize_inbound_payload_is_zero_copy_for_snake_only():
    """Hot-path optimization: when no camelCase aliases are present, the
    helper returns the input dict object unchanged (no allocation)."""
    from hermes_channel_bgos.bgos_adapter import _normalize_inbound_payload
    original = {
        "assistant_id": 7, "chat_id": 1, "message_id": 1,
        "user_id": "u", "text": "x", "files": [],
        "message_type": "standard",
    }
    result = _normalize_inbound_payload(original)
    assert result is original  # same object, not a copy


async def test_normalize_inbound_payload_does_not_mutate_input():
    """When aliases ARE present, the helper returns a NEW dict so callers
    can rely on the input being untouched (e.g. for logging)."""
    from hermes_channel_bgos.bgos_adapter import _normalize_inbound_payload
    original = {"assistantId": 7, "chatId": 1, "messageId": 1}
    result = _normalize_inbound_payload(original)
    assert result is not original
    assert "assistant_id" not in original  # input unchanged
    assert result["assistant_id"] == 7


# -----------------------------------------------------------------------------
# Hot-refresh on unknown assistant_id (v0.8.0). When a message arrives for an
# assistant the adapter doesn't know — almost always because the user exposed a
# new agent in BGOS *after* the gateway started — the adapter re-fetches whoami,
# rebinds, and retries the lookup once instead of dropping until restart.
# -----------------------------------------------------------------------------


class _FakeWs:
    def __init__(self):
        self.bound: list[int] | None = None
        self.unbound: list[int] = []

    def bind_assistants(self, ids):
        self.bound = list(ids)

    def unbind_assistant(self, aid):
        self.unbound.append(aid)


async def test_hot_refresh_recovers_unknown_assistant(monkeypatch):
    adapter = _make_adapter()
    adapter._state.set_route(7, "hades")
    fake_ws = _FakeWs()
    adapter._ws = fake_ws
    adapter._text_batch_window = 0.05

    async def fake_whoami():
        return {
            "pairing_id": 1, "user_id": "owner",
            "assistants": [
                {"assistant_id": 7, "agent_route": "hades"},
                {"assistant_id": 892, "agent_route": "default"},
            ],
        }
    monkeypatch.setattr(adapter._api, "whoami", fake_whoami)

    handled: list[Any] = []

    async def capture(event):
        handled.append(event)
    adapter.handle_message = capture

    await adapter._handle_inbound({
        "assistant_id": 892, "chat_id": 5, "message_id": 1000,
        "user_id": "owner", "text": "hello", "files": [],
        "message_type": "standard",
    })
    await asyncio.sleep(0.15)

    assert adapter._state.get_route(892) == "default"
    assert fake_ws.bound is not None and 892 in fake_ws.bound
    assert len(handled) == 1
    assert handled[0].text == "hello"
    await adapter._api.close()


async def test_hot_refresh_still_unknown_is_dropped(monkeypatch):
    adapter = _make_adapter()
    adapter._state.set_route(7, "hades")
    adapter._ws = _FakeWs()

    calls: list[int] = []

    async def fake_whoami():
        calls.append(1)
        return {"pairing_id": 1, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]}
    monkeypatch.setattr(adapter._api, "whoami", fake_whoami)

    handled: list[Any] = []

    async def capture(event):
        handled.append(event)
    adapter.handle_message = capture

    await adapter._handle_inbound({
        "assistant_id": 999, "chat_id": 5, "message_id": 1,
        "user_id": "u", "text": "x", "files": [],
        "message_type": "standard",
    })
    assert handled == []
    assert len(calls) == 1  # refresh attempted exactly once
    await adapter._api.close()


async def test_hot_refresh_cooldown_limits_whoami(monkeypatch):
    adapter = _make_adapter()
    adapter._state.set_route(7, "hades")
    adapter._ws = _FakeWs()
    adapter._scope_refresh_cooldown = 60.0  # long → 2nd refresh gated

    calls: list[int] = []

    async def fake_whoami():
        calls.append(1)
        return {"pairing_id": 1, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]}
    monkeypatch.setattr(adapter._api, "whoami", fake_whoami)

    async def noop(event):
        pass
    adapter.handle_message = noop

    for mid in (1, 2):
        await adapter._handle_inbound({
            "assistant_id": 999, "chat_id": 5, "message_id": mid,
            "user_id": "u", "text": "x", "files": [],
            "message_type": "standard",
        })
    assert len(calls) == 1  # second inbound's refresh blocked by cooldown
    await adapter._api.close()
