"""Adapter wiring for the [[BGOS_PEER]] marker round trip."""
from __future__ import annotations

import asyncio
import json

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.peer_marker import (
    PEER_LOOP_GUARD_LIMIT,
    PEER_RESULT_HEADER,
)

pytestmark = pytest.mark.asyncio


async def _connected_adapter(server) -> BGOSAdapter:
    server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {
            "pairing_id": 42,
            "assistants": [{"assistant_id": 7, "agent_route": "hades"}],
        },
    )
    adapter = BGOSAdapter(
        BgosConfig(base_url=server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    adapter._state.record_inbound_chat(11)
    return adapter


def _capture_turns(adapter, monkeypatch) -> list:
    handled: list = []

    async def fake_handle(event):
        handled.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return handled


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_marker_is_stripped_and_new_visible_message_anchors_send(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200, {"assistantId": 7},
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 900}},
    )
    mock_bgos_server.on("POST", "/api/v1/peers/894/send").respond(
        200,
        {
            "status": "sent",
            "conversationId": 73,
            "messageId": 901,
            "reply": {"messageId": 902, "text": "Ready"},
        },
    )
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            "I am checking with Athena.\n"
            "[[BGOS_PEER]]"
            '{"op":"send_to_peer","target":894,"text":"Are you ready?",'
            '"waitForReply":true,"timeoutSeconds":20,"reqId":"s1"}'
            "[[/BGOS_PEER]]",
        )
        await _wait_for(lambda: handled)

        visible = mock_bgos_server.last_request("POST", "/api/v1/send-message")
        assert visible.json_body["text"] == "I am checking with Athena."
        assert "BGOS_PEER" not in visible.json_body["text"]

        peer = mock_bgos_server.last_request("POST", "/api/v1/peers/894/send")
        assert peer.headers["X-Caller-Assistant-Id"] == "7"
        assert peer.json_body["text"] == "Are you ready?"
        assert peer.json_body["parentMessageId"] == 900
        assert peer.json_body["waitForReply"] is True
        assert peer.json_body["timeoutSeconds"] == 20

        assert len(handled) == 1
        assert handled[0].text.startswith(PEER_RESULT_HEADER)
        assert "reqId=s1 op=send_to_peer ok" in handled[0].text
        assert "Ready" in handled[0].text
        assert adapter._state.consumed_peer_wait_replies
    finally:
        await adapter.disconnect()


async def test_marker_only_send_uses_prior_assistant_message_as_anchor(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200, {"assistantId": 7},
    )
    mock_bgos_server.on("POST", "/api/v1/peers/894/send").respond(
        200, {"status": "sent", "messageId": 901},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.last_assistant_message_by_chat[11] = 8800
    handled = _capture_turns(adapter, monkeypatch)
    try:
        result = await adapter.send(
            11,
            "[[BGOS_PEER]]"
            '{"op":"send_to_peer","target":894,"text":"Ping",'
            '"reqId":"s1"}'
            "[[/BGOS_PEER]]",
        )
        assert result.success is True
        await _wait_for(lambda: handled)

        visible_posts = [
            request
            for request in mock_bgos_server.requests
            if request.method == "POST"
            and request.path in ("/api/v1/messages", "/api/v1/send-message")
        ]
        assert visible_posts == []
        peer = mock_bgos_server.last_request("POST", "/api/v1/peers/894/send")
        assert peer.json_body["parentMessageId"] == 8800
        assert len(handled) == 1
    finally:
        await adapter.disconnect()


async def test_send_without_any_visible_anchor_returns_local_error(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200, {"assistantId": 7},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            "[[BGOS_PEER]]"
            '{"op":"send_to_peer","target":894,"text":"Ping",'
            '"reqId":"s1"}'
            "[[/BGOS_PEER]]",
        )
        await _wait_for(lambda: handled)

        assert "no_parent_message" in handled[0].text
        peer_posts = [
            request
            for request in mock_bgos_server.requests
            if request.method == "POST" and request.path.startswith("/api/v1/peers")
        ]
        assert peer_posts == []
    finally:
        await adapter.disconnect()


async def test_exact_peer_name_is_resolved_through_roster(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200, {"assistantId": 7},
    )
    mock_bgos_server.on("GET", "/api/v1/peers").respond(
        200,
        [
            {"assistantId": 894, "name": "Athena", "introduced": True},
            {"assistantId": 895, "name": "Apollo", "introduced": True},
        ],
    )
    mock_bgos_server.on("GET", "/api/v1/peers/894/status").respond(
        200, {"online": True},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            "[[BGOS_PEER]]"
            '{"op":"peer_status","target":"Athena","reqId":"p1"}'
            "[[/BGOS_PEER]]",
        )
        await _wait_for(lambda: handled)

        roster = mock_bgos_server.last_request("GET", "/api/v1/peers")
        assert roster.headers["X-Caller-Assistant-Id"] == "7"
        mock_bgos_server.last_request("GET", "/api/v1/peers/894/status")
        assert "reqId=p1 op=peer_status ok" in handled[0].text
    finally:
        await adapter.disconnect()


async def test_backend_typed_error_body_reaches_agent_verbatim(
    mock_bgos_server, monkeypatch,
):
    denial = {
        "error": "contact_unavailable",
        "message": "This contact is paused by its owner.",
        "retryAt": "2026-08-10T00:00:00Z",
    }
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200, {"assistantId": 7},
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 900}},
    )
    mock_bgos_server.on("POST", "/api/v1/peers/894/send").respond(
        403, denial,
    )
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            "Checking.\n[[BGOS_PEER]]"
            '{"op":"send_to_peer","target":894,"text":"Ping",'
            '"reqId":"d1"}'
            "[[/BGOS_PEER]]",
        )
        await _wait_for(lambda: handled)

        text = handled[0].text
        assert "reqId=d1 op=send_to_peer error status=403" in text
        assert json.dumps(denial) in text
    finally:
        await adapter.disconnect()


async def test_malformed_block_is_answered_without_peer_rest_call(
    mock_bgos_server, monkeypatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(11, "[[BGOS_PEER]]{oops[[/BGOS_PEER]]")
        await _wait_for(lambda: handled)

        assert handled[0].text.startswith(PEER_RESULT_HEADER)
        assert "malformed_request" in handled[0].text
        assert "invalid JSON" in handled[0].text
        peer_hits = [
            request
            for request in mock_bgos_server.requests
            if request.path.startswith("/api/v1/peers")
        ]
        assert peer_hits == []
    finally:
        await adapter.disconnect()


async def test_multiple_blocks_dispatch_exactly_one_ordered_result_turn(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200, {"assistantId": 7},
    )
    mock_bgos_server.on("GET", "/api/v1/peers").respond(
        200, [{"assistantId": 894, "name": "Athena"}],
    )
    mock_bgos_server.on("GET", "/api/v1/peers/894/status").respond(
        200, {"online": True},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            '[[BGOS_PEER]]{"op":"list_peers","reqId":"l1"}[[/BGOS_PEER]]'
            '[[BGOS_PEER]]{"op":"peer_status","target":894,'
            '"reqId":"p1"}[[/BGOS_PEER]]',
        )
        await _wait_for(lambda: handled)

        assert len(handled) == 1
        list_at = handled[0].text.index("reqId=l1 op=list_peers ok")
        status_at = handled[0].text.index("reqId=p1 op=peer_status ok")
        assert list_at < status_at
    finally:
        await adapter.disconnect()


async def test_complete_peer_thread_uses_existing_close_helper(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200, {"assistantId": 7},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/peers/conversations/close",
    ).respond(200, {"status": "completed"})
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            "[[BGOS_PEER]]"
            '{"op":"complete_peer_thread","target":894,'
            '"summary":"Athena confirmed readiness.","reqId":"c1"}'
            "[[/BGOS_PEER]]",
        )
        await _wait_for(lambda: handled)

        request = mock_bgos_server.last_request(
            "POST", "/api/v1/peers/conversations/close",
        )
        assert request.headers["X-Caller-Assistant-Id"] == "7"
        assert request.json_body == {
            "peerAssistantId": 894,
            "summary": "Athena confirmed readiness.",
        }
        assert "reqId=c1 op=complete_peer_thread ok" in handled[0].text
    finally:
        await adapter.disconnect()


async def test_loop_guard_refuses_and_real_inbound_resets_it(
    mock_bgos_server, monkeypatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        adapter._peer_consecutive_results[11] = PEER_LOOP_GUARD_LIMIT
        await adapter.send(
            11,
            '[[BGOS_PEER]]{"op":"list_peers","reqId":"l1"}[[/BGOS_PEER]]',
        )
        await _wait_for(lambda: handled)
        assert "loop_guard" in handled[0].text
        assert all(
            request.path != "/api/v1/peers"
            for request in mock_bgos_server.requests
        )

        await adapter._handle_inbound(
            {
                "assistant_id": 7,
                "chat_id": 11,
                "message_id": 5,
                "text": "hi",
                "user_id": "user_1",
            },
            batchable=False,
        )
        assert 11 not in adapter._peer_consecutive_results
    finally:
        await adapter.disconnect()


async def test_button_click_resets_peer_loop_guard(
    mock_bgos_server, monkeypatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    _capture_turns(adapter, monkeypatch)
    try:
        adapter._peer_consecutive_results[11] = PEER_LOOP_GUARD_LIMIT
        await adapter._handle_inbound_click(
            {
                "assistantId": 7,
                "chatId": 11,
                "messageId": 900,
                "userId": "user_1",
                "buttonText": "Yes",
                "callbackData": "yes",
            }
        )
        assert 11 not in adapter._peer_consecutive_results
    finally:
        await adapter.disconnect()
