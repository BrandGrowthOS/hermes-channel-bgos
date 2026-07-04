"""Adapter-side voice integration — the WS voice_rpc wiring, the REST
ack/result/voice-tasks client routes, reply capture off the send/edit
paths, and run_voice_brain_turn's dispatch + settle-window resolution
(the mock BasePlatformAdapter has no _active_sessions, so these tests
exercise the tracking-unavailable fallback the way CI always runs).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter, _VoiceTurnWaiter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.voice_rpc import VoiceRpcError, VoiceRpcTimeout

pytestmark = pytest.mark.asyncio


async def make_adapter(mock_bgos_server) -> BGOSAdapter:
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {
            "pairing_id": 9,
            "user_id": "user_kc",
            "assistants": [{"assistant_id": 894, "agent_route": "default"}],
        },
    )
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz")
    )
    await adapter.connect()
    # Keep the voice polling fast in tests.
    adapter._voice_turn_poll_seconds = 0.02
    adapter._voice_settle_seconds = 0.15
    return adapter


# ── REST client routes ──────────────────────────────────────────────────────


async def test_voice_rpc_rest_routes_hit_pairing_endpoints(mock_bgos_server):
    adapter = await make_adapter(mock_bgos_server)
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/voice-rpc/rpc-7/ack"
    ).respond(200, {"ok": True})
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/voice-rpc/rpc-7/result"
    ).respond(200, {"ok": True})
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/voice-tasks/task-3/result"
    ).respond(200, {"ok": True})
    try:
        await adapter._api.post_voice_rpc_ack("rpc-7")
        await adapter._api.post_voice_rpc_result(
            "rpc-7", {"ok": True, "payload": {"text": "hi"}}
        )
        await adapter._api.post_voice_task_result(
            "task-3", {"ok": True, "payload": {"text": "done"}}
        )
        ack = mock_bgos_server.last_request(
            "POST", "/api/v1/integrations/voice-rpc/rpc-7/ack"
        )
        assert ack.headers.get("X-BGOS-Pairing") == "pair_xyz"
        result = mock_bgos_server.last_request(
            "POST", "/api/v1/integrations/voice-rpc/rpc-7/result"
        )
        assert result.json_body == {"ok": True, "payload": {"text": "hi"}}
        task = mock_bgos_server.last_request(
            "POST", "/api/v1/integrations/voice-tasks/task-3/result"
        )
        assert task.json_body == {"ok": True, "payload": {"text": "done"}}
    finally:
        await adapter.disconnect()


# ── WS frame entry point ────────────────────────────────────────────────────


async def test_handle_voice_rpc_normalizes_and_runs_handler(mock_bgos_server):
    adapter = await make_adapter(mock_bgos_server)
    seen: list[Any] = []

    class FakeHandler:
        async def handle(self, frame):
            seen.append(frame)

    adapter._voice_rpc = FakeHandler()  # type: ignore[assignment]
    try:
        await adapter._handle_voice_rpc(
            {
                "rpcId": "rpc-1",
                "op": "mint",
                "assistantId": 894,
                "agentRoute": "default",
                "chatId": 830,
                "payload": {"recentContext": ""},
            }
        )
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert len(seen) == 1
        assert seen[0].op == "mint"
        # The frame's chatId is backend-originated (authenticated pairing
        # WS) — it must count as a received chat so consult replies aren't
        # rejected as UnknownChatTarget on calls opened before any text.
        assert adapter._state.has_received_chat(830)
    finally:
        await adapter.disconnect()


async def test_handle_voice_rpc_drops_malformed_frames(mock_bgos_server):
    adapter = await make_adapter(mock_bgos_server)
    called: list[Any] = []

    class FakeHandler:
        async def handle(self, frame):
            called.append(frame)

    adapter._voice_rpc = FakeHandler()  # type: ignore[assignment]
    try:
        await adapter._handle_voice_rpc({"op": "mint"})  # no rpcId
        await adapter._handle_voice_rpc({"rpcId": "r", "op": "reboot"})
        await asyncio.sleep(0.05)
        assert called == []
    finally:
        await adapter.disconnect()


async def test_voice_rpc_arrives_over_socketio(mock_bgos_server):
    """End-to-end over the mock Socket.IO server: a voice_rpc emit into the
    pairing room reaches the adapter's normalizer + handler."""
    adapter = await make_adapter(mock_bgos_server)
    seen: list[Any] = []

    class FakeHandler:
        async def handle(self, frame):
            seen.append(frame)

    adapter._voice_rpc = FakeHandler()  # type: ignore[assignment]
    try:
        await asyncio.sleep(0.2)  # let the socket join its rooms
        await mock_bgos_server.emit_to_room(
            "pairing:9",
            "voice_rpc",
            {
                "rpcId": "rpc-ws",
                "op": "consult",
                "assistantId": 894,
                "agentRoute": "default",
                "chatId": 830,
                "payload": {"callId": "c", "name": "hermes_agent_consult",
                            "args": {"question": "hi"}},
            },
        )
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen and seen[0].rpc_id == "rpc-ws"
        assert seen[0].op == "consult"
    finally:
        await adapter.disconnect()


# ── reply capture ───────────────────────────────────────────────────────────


async def test_send_offers_reply_only_when_waiter_pending(mock_bgos_server):
    adapter = await make_adapter(mock_bgos_server)
    mock_bgos_server.on("GET", "/api/v1/chats/830").respond(
        200, {"id": 830, "assistantId": 894}
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 5001}}
    )
    try:
        adapter._state.record_inbound_chat(830)
        # No waiter → no capture bookkeeping.
        await adapter.send(830, "plain reply, nobody listening")
        assert adapter._voice_waiters == {}

        waiter = _VoiceTurnWaiter(dispatched_at=0.0)
        adapter._voice_waiters[830] = [waiter]
        await adapter.send(830, "the spoken answer")
        assert waiter.latest_text == "the spoken answer"

        # A waiter on a different chat must not see this chat's replies.
        other = _VoiceTurnWaiter(dispatched_at=0.0)
        adapter._voice_waiters[831] = [other]
        await adapter.send(830, "another reply")
        assert other.latest_text is None
    finally:
        await adapter.disconnect()


async def test_tool_progress_send_is_never_captured(mock_bgos_server):
    """Emoji tool-progress lines route to the tool_progress card path and
    must never resolve a voice consult as the 'answer'."""
    adapter = await make_adapter(mock_bgos_server)
    mock_bgos_server.on("GET", "/api/v1/chats/830").respond(
        200, {"id": 830, "assistantId": 894}
    )
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 6001})
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 6002}}
    )
    try:
        adapter._state.record_inbound_chat(830)
        waiter = _VoiceTurnWaiter(dispatched_at=0.0)
        adapter._voice_waiters[830] = [waiter]
        await adapter.send(830, "🔧 WebSearch: looking up launch notes")
        assert waiter.latest_text is None
    finally:
        await adapter.disconnect()


async def test_edit_message_offers_latest_streaming_text(mock_bgos_server):
    adapter = await make_adapter(mock_bgos_server)
    mock_bgos_server.on("PATCH", "/api/v1/messages/5001").respond(200, {"id": 5001})
    try:
        adapter._state.record_inbound_chat(830)
        adapter._state.last_user_id_by_chat[830] = "user_kc"
        waiter = _VoiceTurnWaiter(dispatched_at=0.0)
        adapter._voice_waiters[830] = [waiter]
        await adapter.edit_message(830, 5001, "partial answ")
        assert waiter.latest_text == "partial answ"
        await adapter.edit_message(830, 5001, "partial answer, now complete.")
        # Later streaming edits supersede earlier partials.
        assert waiter.latest_text == "partial answer, now complete."
    finally:
        await adapter.disconnect()


# ── run_voice_brain_turn ────────────────────────────────────────────────────


async def test_run_voice_brain_turn_resolves_from_send(mock_bgos_server):
    """Happy path in the mock environment: the turn is dispatched through
    handle_message, the agent's reply comes back via adapter.send(), and
    the settle window resolves it."""
    adapter = await make_adapter(mock_bgos_server)
    mock_bgos_server.on("GET", "/api/v1/chats/830").respond(
        200, {"id": 830, "assistantId": 894}
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 7001}}
    )
    dispatched: list[Any] = []

    async def fake_handle_message(event):
        dispatched.append(event)

        async def reply_later():
            await asyncio.sleep(0.05)
            await adapter.send(830, "Athena's real answer.")

        asyncio.get_running_loop().create_task(reply_later())

    adapter.handle_message = fake_handle_message  # type: ignore[assignment]
    try:
        text = await adapter.run_voice_brain_turn(830, "[voice consult] Q?", 5.0)
        assert text == "Athena's real answer."
        assert len(dispatched) == 1
        assert dispatched[0].text == "[voice consult] Q?"
        assert dispatched[0].chat_id == 830
        # The waiter is cleaned up after resolution.
        assert adapter._voice_waiters == {}
    finally:
        await adapter.disconnect()


async def test_run_voice_brain_turn_times_out_descriptively(mock_bgos_server):
    adapter = await make_adapter(mock_bgos_server)

    async def silent_handle_message(event):
        return None

    adapter.handle_message = silent_handle_message  # type: ignore[assignment]
    try:
        with pytest.raises(VoiceRpcTimeout):
            await adapter.run_voice_brain_turn(830, "[voice consult] Q?", 0.3)
        assert adapter._voice_waiters == {}
    finally:
        await adapter.disconnect()


async def test_run_voice_brain_turn_takes_newest_capture(mock_bgos_server):
    """Streaming supersede semantics end-to-end: multiple sends during the
    turn — the settle window resolves with the LAST one."""
    adapter = await make_adapter(mock_bgos_server)
    mock_bgos_server.on("GET", "/api/v1/chats/830").respond(
        200, {"id": 830, "assistantId": 894}
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 7002}}
    )

    async def fake_handle_message(event):
        async def reply_later():
            await adapter.send(830, "thinking out loud…")
            await asyncio.sleep(0.03)
            await adapter.send(830, "Final: ship it Friday.")

        asyncio.get_running_loop().create_task(reply_later())

    adapter.handle_message = fake_handle_message  # type: ignore[assignment]
    try:
        text = await adapter.run_voice_brain_turn(830, "[voice consult] Q?", 5.0)
        assert text == "Final: ship it Friday."
    finally:
        await adapter.disconnect()


async def test_run_voice_brain_turn_records_chat_as_received(mock_bgos_server):
    """A voice turn on a chat the adapter never saw inbound (call opened
    before any text) must still be able to deliver its reply."""
    adapter = await make_adapter(mock_bgos_server)

    async def silent_handle_message(event):
        return None

    adapter.handle_message = silent_handle_message  # type: ignore[assignment]
    try:
        assert not adapter._state.has_received_chat(999)
        with pytest.raises(VoiceRpcTimeout):
            await adapter.run_voice_brain_turn(999, "[voice consult] Q?", 0.2)
        assert adapter._state.has_received_chat(999)
    finally:
        await adapter.disconnect()


async def test_concurrent_waiters_each_resolve(mock_bgos_server):
    """Two overlapping brain turns on one chat (dispatch + consult) both
    resolve — v1 semantics: each takes the newest capture after its own
    dispatch (documented misattribution edge; the chat holds ground
    truth)."""
    adapter = await make_adapter(mock_bgos_server)
    mock_bgos_server.on("GET", "/api/v1/chats/830").respond(
        200, {"id": 830, "assistantId": 894}
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 7003}}
    )

    async def fake_handle_message(event):
        async def reply_later():
            await asyncio.sleep(0.05)
            await adapter.send(830, f"reply to: {event.text[-12:]}")

        asyncio.get_running_loop().create_task(reply_later())

    adapter.handle_message = fake_handle_message  # type: ignore[assignment]
    try:
        results = await asyncio.gather(
            adapter.run_voice_brain_turn(830, "turn A", 5.0),
            adapter.run_voice_brain_turn(830, "turn B", 5.0),
        )
        assert all(r.startswith("reply to:") for r in results)
        assert adapter._voice_waiters == {}
    finally:
        await adapter.disconnect()


async def test_voice_error_from_empty_turn(mock_bgos_server):
    """EMPTY_REPLY surfaces when the settle window elapses with no capture
    and the deadline hits — via VoiceRpcTimeout, and VoiceRpcError is its
    base (handler maps both to descriptive results)."""
    adapter = await make_adapter(mock_bgos_server)

    async def silent_handle_message(event):
        return None

    adapter.handle_message = silent_handle_message  # type: ignore[assignment]
    try:
        with pytest.raises(VoiceRpcError):
            await adapter.run_voice_brain_turn(830, "turn", 0.2)
    finally:
        await adapter.disconnect()
