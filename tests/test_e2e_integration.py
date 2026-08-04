"""End-to-end pytest suite — cross-cutting flows that exercise multiple
components together against the in-memory MockBgosServer.

Per-task tests in the sibling test files cover unit behavior. These tests
exist to catch regressions where the seams between components (REST ↔ WS,
adapter ↔ approval state ↔ callback router, inbound → handle_message →
outbound send) silently drift.
"""
from __future__ import annotations

import asyncio

import pytest

import hermes_channel_bgos.bgos_adapter as adapter_mod
from hermes_channel_bgos.bgos_adapter import BGOSAdapter, MessageEvent
from hermes_channel_bgos.bgos_api import BgosApi
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


def _seed_whoami(server, assistants=None) -> None:
    server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {
            "pairing_id": 42,
            "assistants": assistants or [{"assistant_id": 7, "agent_route": "hades"}],
        },
    )


async def test_full_pair_then_message_round_trip(mock_bgos_server):
    """Pair → connect → inbound user message → agent replies via send()."""
    # 1. Pairing
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz", "pairing_id": 42},
    )
    # 2. Whoami
    _seed_whoami(mock_bgos_server)
    # 3. Outbound message endpoint
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 301})

    # Simulate the pair CLI's handshake
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token=None))
    paired = await api.pair_exchange(
        code="BGOS-ABCD-EF", device_label="test", integration="hermes",
    )
    await api.close()
    assert paired["pairing_token"] == "pair_xyz"

    # Adapter uses the paired token
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token=paired["pairing_token"],
    ))

    # Fake agent behavior: on any inbound, echo back the text
    async def fake_handle(event: MessageEvent) -> None:
        await adapter.send(chat_id=event.chat_id, content=f"echo: {event.text}")

    adapter.handle_message = fake_handle  # type: ignore[method-assign]

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        # Simulate user message arriving via WS
        await mock_bgos_server.emit_to_room(
            "assistant:7", "inbound_message",
            {"chat_id": 11, "message_id": 200, "text": "hi",
             "user_id": "u1", "assistant_id": 7, "message_type": "standard"},
        )
        # Standard text gets adaptive-batched (≤0.24s flush for short text);
        # wait past the window so the echo POST has time to land.
        await asyncio.sleep(0.35)
    finally:
        await adapter.disconnect()

    posts = [r for r in mock_bgos_server.requests
             if r.method == "POST" and r.path == "/api/v1/messages"]
    assert len(posts) == 1
    assert posts[-1].json_body["text"] == "echo: hi"
    assert posts[-1].headers["X-BGOS-Pairing"] == "pair_xyz"


async def test_approval_round_trip_via_ws(mock_bgos_server, monkeypatch):
    """send_exec_approval posts bubble → WS callback_result arrives →
    _handle_callback routes to resolve_gateway_approval with session_key."""
    # Per-user authz gate (Task 2.2) is fail-closed; the WS callback
    # payload below carries no user_id, so we explicitly bypass.
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    _seed_whoami(mock_bgos_server)
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 555})

    verdicts: list[tuple[str, str]] = []

    def fake_resolve(session_key: str, choice: str) -> None:
        verdicts.append((session_key, choice))

    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", fake_resolve)

    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))
    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        await adapter.send_exec_approval(
            chat_id=11, command="rm -rf node_modules",
            session_key="DANGER-KEY", description="Proceed?",
        )
        # approval_id=1 is the first minted by itertools.count(1)
        await mock_bgos_server.emit_to_room(
            "assistant:7", "callback_result",
            {"message_id": 555, "callback_data": "ea:deny:1"},
        )
        await asyncio.sleep(0.2)
    finally:
        await adapter.disconnect()

    assert verdicts == [("DANGER-KEY", "deny")]
    # Pending state cleaned up
    assert adapter._approval_state == {}


async def test_approval_tap_via_inbound_click_resolves_without_agent_message(mock_bgos_server, monkeypatch):
    """Regression: BGOS may deliver option taps as inbound_click.

    Approval buttons carry `ea:*` callbackData. Those clicks must resolve the
    pending gateway approval directly, not become a normal user message like
    "Always allow" that leaves the dangerous command blocked until timeout.
    """
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    _seed_whoami(mock_bgos_server)
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 556})

    verdicts: list[tuple[str, str]] = []
    agent_calls: list[MessageEvent] = []

    def fake_resolve(session_key: str, choice: str) -> None:
        verdicts.append((session_key, choice))

    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", fake_resolve)

    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        agent_calls.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        await adapter.send_exec_approval(
            chat_id=11, command="bash <(curl -fsSL https://example.invalid/install.sh)",
            session_key="DANGER-KEY", description="Proceed?",
        )
        await mock_bgos_server.emit_to_room(
            "assistant:7", "inbound_click",
            {
                "assistantId": 7,
                "chatId": 11,
                "messageId": 556,
                "optionId": 99,
                "callbackData": "ea:always:1",
                "buttonText": "Always allow",
                "userId": "u1",
            },
        )
        await asyncio.sleep(0.2)
    finally:
        await adapter.disconnect()

    assert verdicts == [("DANGER-KEY", "always")]
    assert agent_calls == []
    assert adapter._approval_state == {}


async def test_bridge_local_roundtrip_through_ws(mock_bgos_server, monkeypatch):
    """Full: WS slash_command inbound → bridge-local intercept → adapter
    posts ack message → agent never involved."""
    _seed_whoami(mock_bgos_server)
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 700})

    agent_calls: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        agent_calls.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        # /new slash command over WS
        await mock_bgos_server.emit_to_room(
            "assistant:7", "inbound_message",
            {"chat_id": 11, "message_id": 501, "text": "/new",
             "user_id": "u1", "assistant_id": 7,
             "message_type": "slash_command", "command_name": "new",
             "command_args": ""},
        )
        await asyncio.sleep(0.2)
    finally:
        await adapter.disconnect()

    # Agent never invoked
    assert agent_calls == []
    # But an ack was posted to BGOS
    posts = [r for r in mock_bgos_server.requests
             if r.method == "POST" and r.path == "/api/v1/messages"]
    assert len(posts) == 1
    assert "reset" in posts[0].json_body["text"].lower()


async def test_retry_replay_flows_to_agent(mock_bgos_server, monkeypatch):
    """Setup: prior user message arrives and caches text; /retry then
    replays it through _handle_inbound → handle_message."""
    _seed_whoami(mock_bgos_server)
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 701})

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        # Real user message first — standard text now flows through
        # adaptive batching (≤0.24s flush for short text); wait past it.
        await mock_bgos_server.emit_to_room(
            "assistant:7", "inbound_message",
            {"chat_id": 11, "message_id": 100, "text": "what's the weather?",
             "user_id": "u", "assistant_id": 7, "message_type": "standard"},
        )
        await asyncio.sleep(0.35)

        # Then /retry — bridge-local intercept replays the cached text
        # through _handle_inbound(batchable=False), so it dispatches
        # synchronously.
        await mock_bgos_server.emit_to_room(
            "assistant:7", "inbound_message",
            {"chat_id": 11, "message_id": 101, "text": "/retry",
             "user_id": "u", "assistant_id": 7,
             "message_type": "slash_command", "command_name": "retry",
             "command_args": ""},
        )
        await asyncio.sleep(0.2)
    finally:
        await adapter.disconnect()

    # Two handle_message calls: original + replay
    texts = [ev.text for ev in handled]
    assert texts == ["what's the weather?", "what's the weather?"]


async def test_reconnect_backfill_replays_missed_messages(mock_bgos_server, monkeypatch):
    """After a reconnect, the adapter pulls missed messages via REST and
    replays them through _handle_inbound."""
    _seed_whoami(mock_bgos_server)
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200,
        {"messages": [
            {"chat_id": 11, "message_id": 305, "text": "missed 1",
             "user_id": "u", "assistant_id": 7, "message_type": "standard"},
            {"chat_id": 11, "message_id": 306, "text": "missed 2",
             "user_id": "u", "assistant_id": 7, "message_type": "standard"},
        ]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # connect() now schedules a first-connect backfill via
        # asyncio.create_task(_run_backfill(0)). Let it drain and reset the
        # handled list so the explicit reconnect call below is what we assert
        # against.
        await asyncio.sleep(0.15)
        handled.clear()
        # The mock /inbound route re-serves the SAME canned ids for every
        # fetch, which a real since-cursor backend never does. Reset the
        # duplicate-delivery guard along with the handled list so this
        # test stays about the backfill mechanism, not the dedup.
        adapter._dispatched_inbound_ids.clear()

        # Simulate reconnect by invoking the backfill path directly with a
        # cursor. (The actual WS-reconnect test is skipped on Windows due to
        # python-socketio's clean-close behavior — see test_bgos_ws.py.)
        await adapter._run_backfill(300)
    finally:
        await adapter.disconnect()

    assert [ev.message_id for ev in handled] == [305, 306]
    # Last call to /integrations/inbound should be the cursor=300 one
    last_inbound = [
        r for r in mock_bgos_server.requests
        if r.method == "GET" and r.path == "/api/v1/integrations/inbound"
    ][-1]
    assert last_inbound.query["since_message_id"] == "300"


async def test_backfill_storm_guard_fast_forwards_without_dispatch(
    mock_bgos_server, monkeypatch, tmp_path,
):
    """A stale cursor must not replay a huge historical BGOS backlog.

    This pins the restart-spam regression: after a restart with an old cursor,
    BGOS can return many old peer smoke-test messages and slash commands. The
    adapter should advance the durable cursor to the newest returned id and
    avoid dispatching them as fresh user turns.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "3")
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200,
        {"messages": [
            {"id": 401, "chat_id": 11, "text": "old 1",
             "user_id": "u", "assistant_id": 7, "message_type": "standard"},
            {"id": 402, "chat_id": 11, "text": "/restart",
             "user_id": "u", "assistant_id": 7, "message_type": "slash_command"},
            {"id": 403, "chat_id": 12, "text": "old 3",
             "user_id": "u", "assistant_id": 7, "message_type": "standard"},
            {"id": 404, "chat_id": 13, "text": "old 4",
             "user_id": "u", "assistant_id": 7, "message_type": "standard"},
        ]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter._run_backfill(300)

    assert handled == []
    assert (tmp_path / "bgos_last_id").read_text() == "404"


async def test_pairing_revoked_mid_session_surfaces_as_401(mock_bgos_server):
    """If the pairing is revoked, the adapter's next whoami returns 401 on
    connect, which propagates out so the caller can clear secrets +
    re-pair."""
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        401, {"error": "PAIRING_REVOKED"},
    )
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_revoked",
    ))
    from hermes_channel_bgos.bgos_api import BgosApiError
    with pytest.raises(BgosApiError) as excinfo:
        await adapter.connect()
    assert excinfo.value.code == "PAIRING_REVOKED"
    # Disconnect after a failed connect must not crash
    await adapter.disconnect()


async def test_multi_assistant_routing(mock_bgos_server, monkeypatch):
    """Two assistants on the same pairing route to the correct agent_route."""
    _seed_whoami(
        mock_bgos_server,
        assistants=[
            {"assistant_id": 7, "agent_route": "hades"},
            {"assistant_id": 8, "agent_route": "ramy"},
        ],
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        await mock_bgos_server.emit_to_room(
            "assistant:7", "inbound_message",
            {"chat_id": 11, "message_id": 1, "text": "to hades",
             "user_id": "u", "assistant_id": 7, "message_type": "standard"},
        )
        await mock_bgos_server.emit_to_room(
            "assistant:8", "inbound_message",
            {"chat_id": 12, "message_id": 2, "text": "to ramy",
             "user_id": "u", "assistant_id": 8, "message_type": "standard"},
        )
        await asyncio.sleep(0.3)
    finally:
        await adapter.disconnect()

    by_route = {ev.agent_route: ev.text for ev in handled}
    assert by_route == {"hades": "to hades", "ramy": "to ramy"}


async def test_a2a_reply_uses_chat_owner_not_addressed_assistant(mock_bgos_server, monkeypatch):
    """A2A inbound events are addressed to this Hermes assistant, but the
    side-thread chat can be owned by the peer assistant. send() must resolve
    chat.assistantId from /chats/:id, otherwise /send-message skips the peer
    bridge and the originating agent times out waiting for a tagged reply.
    """
    _seed_whoami(
        mock_bgos_server,
        assistants=[{"assistant_id": 894, "agent_route": "default"}],
    )
    mock_bgos_server.on("GET", "/api/v1/chats/950").respond(
        200,
        {"id": 950, "assistantId": 872, "kind": "a2a"},
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201,
        {"message": {"id": 9001}},
    )

    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        source = getattr(event, "source", None)
        chat_id = getattr(event, "chat_id", None) or getattr(source, "chat_id")
        await adapter.send(chat_id=chat_id, content=f"ack: {event.text}")

    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)
        await mock_bgos_server.emit_to_room(
            "assistant:894", "inbound_message",
            {
                "chatId": 950,
                "messageId": 8667,
                "text": "Synthetic peer inbound test",
                "userId": "user_1",
                "assistantId": 894,
                "messageType": "standard",
                "peerConversationId": 71,
                "turnState": "expecting_reply",
            },
        )
        await asyncio.sleep(0.35)
    finally:
        await adapter.disconnect()

    posts = [
        r for r in mock_bgos_server.requests
        if r.method == "POST" and r.path == "/api/v1/send-message"
    ]
    assert len(posts) == 1
    assert posts[0].json_body["chatId"] == 950
    assert posts[0].json_body["assistantId"] == 872
    assert posts[0].json_body["text"] == "ack: Synthetic peer inbound test"


async def test_empty_standard_inbound_is_dropped_after_cursor_advances(mock_bgos_server, monkeypatch):
    """Restart reconciliation can emit empty standard events. They should not
    resume Hermes sessions or resend a previous assistant answer.
    """
    _seed_whoami(mock_bgos_server)
    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(
        base_url=mock_bgos_server.url, pairing_token="pair_xyz",
    ))

    async def fake_handle(event: MessageEvent) -> None:
        handled.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)
        await mock_bgos_server.emit_to_room(
            "assistant:7", "inbound_message",
            {
                "chat_id": 11,
                "message_id": 777,
                "text": "",
                "user_id": "u1",
                "assistant_id": 7,
                "message_type": "standard",
                "files": [],
            },
        )
        await asyncio.sleep(0.2)
    finally:
        await adapter.disconnect()

    assert handled == []
    assert adapter._load_last_id() == 777
