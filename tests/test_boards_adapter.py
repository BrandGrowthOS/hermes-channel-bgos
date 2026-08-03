"""Adapter wiring for the [[BGOS_BOARDS]] round trip.

The pure protocol lives in boards_marker.py (tests/test_boards_marker.py).
These tests pin the adapter's half: send() strips the marker and posts only
the residual text; the REST call runs against the agent-family boards route
with the chat's assistant; the result comes back to the agent as ONE
synthetic system turn through handle_message (the voice-lane mechanism), a
denial body verbatim inside it; a malformed block never reaches REST; and
the loop guard stops a marker ping pong until real inbound arrives.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.boards_marker import (
    BOARDS_LOOP_GUARD_LIMIT,
    BOARDS_RESULT_HEADER,
)
from hermes_channel_bgos.config import BgosConfig

pytestmark = pytest.mark.asyncio

_QUERY_BLOCK = (
    '[[BGOS_BOARDS]]{"op":"query","board":"Tasks","reqId":"q1","limit":5}'
    "[[/BGOS_BOARDS]]"
)


async def _connected_adapter(server) -> BGOSAdapter:
    server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
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


async def test_marker_is_stripped_and_result_turn_dispatched(
    mock_bgos_server, monkeypatch,
):
    """The full happy path: residual text posts without marker syntax, the
    boards route is called with the chat's assistant, and the agent receives
    a system turn opening with the provenance header and carrying the
    backend's markdown, correlated by reqId."""
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 900}},
    )
    boards_path = "/api/v1/integrations/assistants/7/boards/Tasks/rows/query"
    mock_bgos_server.on("POST", boards_path).respond(
        200, {"markdown": "| key | Title |\n| ab12cd34 | Fix login |"},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(11, "Checking the board.\n" + _QUERY_BLOCK)

        posted = mock_bgos_server.last_request("POST", "/api/v1/send-message")
        assert posted.json_body["text"] == "Checking the board."
        assert "BGOS_BOARDS" not in posted.json_body["text"]

        await _wait_for(lambda: handled)
        turn_text = handled[0].text
        assert turn_text.startswith(BOARDS_RESULT_HEADER)
        assert "reqId=q1 op=query ok" in turn_text
        assert "| ab12cd34 | Fix login |" in turn_text

        boards_req = mock_bgos_server.last_request("POST", boards_path)
        assert boards_req.headers["X-BGOS-Pairing"] == "pair_xyz"
        assert boards_req.json_body == {"limit": 5}
    finally:
        await adapter.disconnect()


async def test_marker_only_reply_posts_no_visible_message(
    mock_bgos_server, monkeypatch,
):
    """A reply that is nothing but a board call must not post an empty
    bubble to the user."""
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    boards_path = "/api/v1/integrations/assistants/7/boards"
    mock_bgos_server.on("GET", boards_path).respond(200, {"markdown": "| Board |"})
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        result = await adapter.send(
            11, '[[BGOS_BOARDS]]{"op":"list","reqId":"l1"}[[/BGOS_BOARDS]]',
        )
        assert result.success is True
        await _wait_for(lambda: handled)
        sends = [
            r for r in mock_bgos_server.requests
            if r.method == "POST" and r.path == "/api/v1/send-message"
        ]
        assert sends == []
        assert "reqId=l1 op=list ok" in handled[0].text
    finally:
        await adapter.disconnect()


async def test_backend_denial_body_reaches_the_agent_verbatim(
    mock_bgos_server, monkeypatch,
):
    """The boards denial contract: {error, message} byte for byte, never a
    paraphrase, with the status on the section header."""
    denial = {
        "error": "not_found_board",
        "message": "No board by that name is visible to you.",
    }
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/assistants/7/boards/Secret/rows/query",
    ).respond(404, denial)
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            '[[BGOS_BOARDS]]{"op":"query","board":"Secret","reqId":"d1"}'
            "[[/BGOS_BOARDS]]",
        )
        await _wait_for(lambda: handled)
        turn_text = handled[0].text
        assert "reqId=d1 op=query error status=404" in turn_text
        assert json.dumps(denial) in turn_text
    finally:
        await adapter.disconnect()


async def test_malformed_block_is_answered_without_any_rest_call(
    mock_bgos_server, monkeypatch,
):
    """Invalid JSON: stripped from the visible text, answered with a
    malformed_request section, and the boards route never hit. Silence
    would strand an agent that is waiting on data."""
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(11, "[[BGOS_BOARDS]]{oops[[/BGOS_BOARDS]]")
        await _wait_for(lambda: handled)
        turn_text = handled[0].text
        assert turn_text.startswith(BOARDS_RESULT_HEADER)
        assert "malformed_request" in turn_text
        assert "invalid JSON" in turn_text
        boards_hits = [
            r for r in mock_bgos_server.requests if "/boards" in r.path
        ]
        assert boards_hits == []
    finally:
        await adapter.disconnect()


async def test_loop_guard_refuses_at_the_limit_then_goes_quiet(
    mock_bgos_server, monkeypatch,
):
    """At the limit: one refusal turn naming loop_guard and no REST call.
    Past the limit: no further synthetic turns at all until real inbound."""
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        adapter._boards_consecutive_results[11] = BOARDS_LOOP_GUARD_LIMIT
        await adapter.send(11, _QUERY_BLOCK)
        await _wait_for(lambda: handled)
        assert "loop_guard" in handled[0].text
        boards_hits = [
            r for r in mock_bgos_server.requests if "/boards" in r.path
        ]
        assert boards_hits == []

        handled.clear()
        await adapter.send(11, _QUERY_BLOCK)
        await asyncio.sleep(0.1)
        assert handled == []
    finally:
        await adapter.disconnect()


async def test_real_inbound_resets_the_loop_guard(mock_bgos_server, monkeypatch):
    adapter = await _connected_adapter(mock_bgos_server)
    _capture_turns(adapter, monkeypatch)
    try:
        adapter._boards_consecutive_results[11] = BOARDS_LOOP_GUARD_LIMIT
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
        assert 11 not in adapter._boards_consecutive_results
    finally:
        await adapter.disconnect()


async def test_attach_small_file_goes_inline_base64(
    mock_bgos_server, monkeypatch, tmp_path,
):
    """attach with a local path at or under the inline threshold is one POST
    carrying content_base64 (snake_case per the shipped tool contract)."""
    report = tmp_path / "report.txt"
    report.write_text("quarterly numbers")
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    attach_path = (
        "/api/v1/integrations/assistants/7/boards/Tasks/rows/ab12cd34/attachments"
    )
    mock_bgos_server.on("POST", attach_path).respond(
        200, {"attachmentId": "0198aaaa-1111-7222-8333-444455556666", "ok": True},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            "[[BGOS_BOARDS]]"
            + json.dumps(
                {
                    "op": "attach",
                    "board": "Tasks",
                    "row": "ab12cd34",
                    "path": str(report),
                    "reqId": "a1",
                }
            )
            + "[[/BGOS_BOARDS]]",
        )
        await _wait_for(lambda: handled)
        assert "reqId=a1 op=attach ok" in handled[0].text
        req = mock_bgos_server.last_request("POST", attach_path)
        assert req.json_body["name"] == "report.txt"
        assert req.json_body["size"] == len("quarterly numbers")
        import base64

        assert (
            base64.b64decode(req.json_body["content_base64"]).decode()
            == "quarterly numbers"
        )
    finally:
        await adapter.disconnect()


async def test_loop_guard_arms_itself_through_the_real_path(
    mock_bgos_server, monkeypatch,
):
    """Mutation guard for the counter increment (review finding 2026-08-03):
    the limit is reached by real dispatched result turns, not by a test
    preloading the counter. Delete the increment on the success path and
    this test fails."""
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    boards_path = "/api/v1/integrations/assistants/7/boards"
    mock_bgos_server.on("GET", boards_path).respond(200, {"markdown": "| Board |"})
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        for i in range(BOARDS_LOOP_GUARD_LIMIT):
            await adapter.send(
                11,
                '[[BGOS_BOARDS]]{"op":"list","reqId":"l%d"}[[/BGOS_BOARDS]]' % i,
            )
            await _wait_for(lambda: handled)
            assert "op=list ok" in handled[0].text
            handled.clear()
        assert adapter._boards_consecutive_results[11] == BOARDS_LOOP_GUARD_LIMIT

        board_hits_before = len(
            [r for r in mock_bgos_server.requests if r.path == boards_path]
        )
        await adapter.send(
            11, '[[BGOS_BOARDS]]{"op":"list","reqId":"over"}[[/BGOS_BOARDS]]',
        )
        await _wait_for(lambda: handled)
        assert "loop_guard" in handled[0].text
        board_hits_after = len(
            [r for r in mock_bgos_server.requests if r.path == boards_path]
        )
        assert board_hits_after == board_hits_before
    finally:
        await adapter.disconnect()


async def test_attach_large_file_uses_the_presigned_flow(
    mock_bgos_server, monkeypatch, tmp_path,
):
    """Above the inline threshold: create meta, PUT the bytes to the
    presigned URL, complete, and the result keeps the attachmentId for
    parity with the inline path."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (600 * 1024))
    attachment_id = "0198bbbb-2222-7333-8444-555566667777"
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    attach_path = (
        "/api/v1/integrations/assistants/7/boards/Tasks/rows/ab12cd34/attachments"
    )
    complete_path = (
        f"/api/v1/integrations/assistants/7/boards/Tasks/attachments/{attachment_id}/complete"
    )
    adapter = await _connected_adapter(mock_bgos_server)
    mock_bgos_server.on("POST", attach_path).respond(
        200,
        {
            "attachmentId": attachment_id,
            "uploadUrl": mock_bgos_server.url + "/s3-upload",
        },
    )
    mock_bgos_server.on("PUT", "/s3-upload").respond(200, {})
    mock_bgos_server.on("POST", complete_path).respond(200, {"ok": True})
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            json.dumps(
                {
                    "op": "attach",
                    "board": "Tasks",
                    "row": "ab12cd34",
                    "path": str(big),
                    "reqId": "big1",
                }
            ).join(["[[BGOS_BOARDS]]", "[[/BGOS_BOARDS]]"]),
        )
        await _wait_for(lambda: handled)
        turn_text = handled[0].text
        assert "reqId=big1 op=attach ok" in turn_text
        assert attachment_id in turn_text
        meta_req = mock_bgos_server.last_request("POST", attach_path)
        assert "content_base64" not in meta_req.json_body
        assert meta_req.json_body["size"] == 600 * 1024
        put_req = mock_bgos_server.last_request("PUT", "/s3-upload")
        assert len(put_req.body) == 600 * 1024
        mock_bgos_server.last_request("POST", complete_path)
    finally:
        await adapter.disconnect()


async def test_attach_over_the_cap_is_refused_locally(
    mock_bgos_server, monkeypatch, tmp_path,
):
    """A file over 25 MB is refused before any bytes are read or any REST
    call is made."""
    huge = tmp_path / "huge.bin"
    with huge.open("wb") as f:
        f.seek(25 * 1024 * 1024)
        f.write(b"x")
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(
            11,
            json.dumps(
                {
                    "op": "attach",
                    "board": "Tasks",
                    "row": "ab12cd34",
                    "path": str(huge),
                    "reqId": "h1",
                }
            ).join(["[[BGOS_BOARDS]]", "[[/BGOS_BOARDS]]"]),
        )
        await _wait_for(lambda: handled)
        assert "file_too_large" in handled[0].text
        boards_hits = [
            r for r in mock_bgos_server.requests if "/boards" in r.path
        ]
        assert boards_hits == []
    finally:
        await adapter.disconnect()


async def test_unresolvable_assistant_answers_no_assistant(
    mock_bgos_server, monkeypatch,
):
    """No GET /chats route (older backend): the call is not executed and the
    agent is told exactly why, instead of a silent drop."""
    adapter = await _connected_adapter(mock_bgos_server)
    handled = _capture_turns(adapter, monkeypatch)
    try:
        await adapter.send(11, _QUERY_BLOCK)
        await _wait_for(lambda: handled)
        assert "no_assistant" in handled[0].text
        boards_hits = [
            r for r in mock_bgos_server.requests if "/boards" in r.path
        ]
        assert boards_hits == []
    finally:
        await adapter.disconnect()


async def test_button_click_resets_the_loop_guard(mock_bgos_server, monkeypatch):
    """A tap is real user activity (review finding 2026-08-03): it re-arms
    board calls exactly like a typed message."""
    adapter = await _connected_adapter(mock_bgos_server)
    _capture_turns(adapter, monkeypatch)
    try:
        adapter._boards_consecutive_results[11] = BOARDS_LOOP_GUARD_LIMIT
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
        assert 11 not in adapter._boards_consecutive_results
    finally:
        await adapter.disconnect()
