"""App control ([[BGOS_UI]]) and Work Stream ([[BGOS_TASK]]) markers.

Board row 019fc652-952b: Hermes agents could not steer the HOAI call screen or
settle a Work Stream task the way the Claude Code channel can. These pin the
behaviour of both markers end to end: the parse (valid and malformed), the
exact wire call each one makes (same routes, pairing auth), the strip from
visible text, and that neither can break the reply it rides on.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from hermes_channel_bgos.app_control_marker import (
    TASK_RESULT_MAX,
    UI_ACTION_KINDS,
    UI_MAX_BLOCKS,
    UI_TARGET_MAX,
    TaskResultRequest,
    UiActionRequest,
    parse_task_block,
    parse_ui_blocks,
    strip_app_control_markers,
)
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.voice_rpc import (
    VoiceConfig,
    VoiceRpcDeps,
    VoiceRpcFrame,
    VoiceRpcHandler,
)

UI_PATH = "/api/v1/integrations/voice-sessions/{}/ui-action"
TASK_PATH = "/api/v1/integrations/voice-tasks/{}/result"


def _task(body: dict | str) -> str:
    raw = body if isinstance(body, str) else json.dumps(body)
    return f"[[BGOS_TASK]]{raw}[[/BGOS_TASK]]"


# ── [[BGOS_UI]] parse ───────────────────────────────────────────────────────


class TestParseUiBlocks:
    def test_absent_marker_leaves_text_untouched(self):
        text = "A normal reply."
        assert parse_ui_blocks(text) == (text, [], [])

    def test_empty_input_is_safe(self):
        assert parse_ui_blocks("") == ("", [], [])

    def test_bare_action_is_extracted_and_stripped(self):
        cleaned, reqs, errs = parse_ui_blocks(
            "Opening it.\n[[BGOS_UI]]show_work_stream[[/BGOS_UI]]"
        )
        assert cleaned == "Opening it."
        assert reqs == [UiActionRequest(action="show_work_stream")]
        assert errs == []

    def test_action_with_target(self):
        _, reqs, _ = parse_ui_blocks("[[BGOS_UI]] expand_card | invoice run [[/BGOS_UI]]")
        assert reqs == [UiActionRequest(action="expand_card", target="invoice run")]

    def test_every_backend_action_is_accepted(self):
        text = "".join(f"[[BGOS_UI]]{a}[[/BGOS_UI]]" for a in UI_ACTION_KINDS[:UI_MAX_BLOCKS])
        _, reqs, errs = parse_ui_blocks(text)
        assert [r.action for r in reqs] == list(UI_ACTION_KINDS[:UI_MAX_BLOCKS])
        assert errs == []
        for action in UI_ACTION_KINDS:
            _, reqs, errs = parse_ui_blocks(f"[[BGOS_UI]]{action}[[/BGOS_UI]]")
            assert reqs == [UiActionRequest(action=action)] and errs == []

    def test_whitelist_matches_the_backend_dto(self):
        # backend/src/dto/voice/voice-session.dto.ts VOICE_UI_ACTION_KINDS
        assert UI_ACTION_KINDS == (
            "show_work_stream", "hide_work_stream", "open_transcript",
            "close_transcript", "expand_card", "show_card_steps",
            "open_chat", "close_chat",
        )

    def test_tag_and_action_are_case_insensitive(self):
        _, reqs, _ = parse_ui_blocks("[[bgos_ui]]Show_Work_Stream[[/bgos_ui]]")
        assert reqs == [UiActionRequest(action="show_work_stream")]

    def test_unknown_action_is_stripped_not_executed(self):
        cleaned, reqs, errs = parse_ui_blocks("Hi [[BGOS_UI]]delete_everything[[/BGOS_UI]]")
        assert cleaned == "Hi"
        assert reqs == []
        assert len(errs) == 1 and "unknown ui action" in errs[0]

    def test_empty_block_is_an_error(self):
        cleaned, reqs, errs = parse_ui_blocks("[[BGOS_UI]]  [[/BGOS_UI]]")
        assert (cleaned, reqs) == ("", [])
        assert errs == ["empty [[BGOS_UI]] block"]

    def test_multiline_target_is_refused(self):
        _, reqs, errs = parse_ui_blocks("[[BGOS_UI]]expand_card | a\nb[[/BGOS_UI]]")
        assert reqs == [] and errs

    def test_json_body_is_refused_as_unknown_action(self):
        _, reqs, errs = parse_ui_blocks('[[BGOS_UI]]{"action":"open_chat"}[[/BGOS_UI]]')
        assert reqs == [] and errs

    def test_target_is_capped_to_the_backend_limit(self):
        _, reqs, _ = parse_ui_blocks(f"[[BGOS_UI]]expand_card | {'x' * 900}[[/BGOS_UI]]")
        assert len(reqs[0].target) == UI_TARGET_MAX

    def test_blank_target_becomes_none(self):
        _, reqs, _ = parse_ui_blocks("[[BGOS_UI]]open_chat |   [[/BGOS_UI]]")
        assert reqs == [UiActionRequest(action="open_chat", target=None)]

    def test_runaway_blocks_past_the_cap_are_not_executed(self):
        text = "[[BGOS_UI]]open_chat[[/BGOS_UI]]" * (UI_MAX_BLOCKS + 2)
        cleaned, reqs, errs = parse_ui_blocks(text)
        assert cleaned == ""
        assert len(reqs) == UI_MAX_BLOCKS
        assert len(errs) == 2

    def test_fenced_example_is_documentation_not_a_call(self):
        text = "Use it like:\n```\n[[BGOS_UI]]open_chat[[/BGOS_UI]]\n```"
        assert parse_ui_blocks(text) == (text, [], [])

    def test_inline_code_span_is_documentation_not_a_call(self):
        text = "Write `[[BGOS_UI]]open_chat[[/BGOS_UI]]` to peek at the chat."
        assert parse_ui_blocks(text) == (text, [], [])
        task = "Like `" + _task({"taskId": "1", "result": "a"}) + "`"
        assert parse_task_block(task) == (task, None, [])

    def test_unclosed_block_is_left_alone(self):
        text = "[[BGOS_UI]]open_chat"
        assert parse_ui_blocks(text) == (text, [], [])


# ── [[BGOS_TASK]] parse ─────────────────────────────────────────────────────


class TestParseTaskBlock:
    def test_absent_marker_leaves_text_untouched(self):
        assert parse_task_block("plain") == ("plain", None, [])
        assert parse_task_block("") == ("", None, [])

    def test_done_result(self):
        cleaned, req, errs = parse_task_block(
            "Done.\n" + _task({"taskId": "42", "result": "Booked for 9am."})
        )
        assert cleaned == "Done."
        assert req == TaskResultRequest(task_id="42", result="Booked for 9am.")
        assert errs == []
        assert req.wire_body() == {"ok": True, "payload": {"text": "Booked for 9am."}}

    def test_failed_result_matches_complete_voice_task_body(self):
        _, req, _ = parse_task_block(_task({"taskId": "7", "result": "No access.", "failed": True}))
        assert req.wire_body() == {
            "ok": False,
            "error": {"code": "DISPATCH_FAILED", "message": "No access."},
        }

    def test_integer_task_id_is_accepted(self):
        _, req, _ = parse_task_block(_task({"taskId": 99, "result": "ok"}))
        assert req.task_id == "99"

    @pytest.mark.parametrize(
        "body, fragment",
        [
            ("{not json", "not valid JSON"),
            ("[1,2]", "JSON object"),
            ({"taskId": "t-1", "result": "x"}, "numeric voice task id"),
            ({"taskId": "1/../2", "result": "x"}, "numeric voice task id"),
            ({"taskId": True, "result": "x"}, "numeric voice task id"),
            ({"result": "x"}, "numeric voice task id"),
            ({"taskId": "1", "result": "   "}, "non-empty string"),
            ({"taskId": "1", "result": 5}, "non-empty string"),
            ({"taskId": "1", "result": "x", "failed": "yes"}, "true or false"),
            ({"taskId": "1", "result": "x", "sessionId": "9"}, "unsupported fields"),
        ],
    )
    def test_malformed_block_is_stripped_and_not_executed(self, body, fragment):
        cleaned, req, errs = parse_task_block("Summary. " + _task(body))
        assert cleaned == "Summary."
        assert req is None
        assert len(errs) == 1 and fragment in errs[0]

    def test_result_is_capped(self):
        _, req, _ = parse_task_block(_task({"taskId": "1", "result": "y" * 9000}))
        assert len(req.result) == TASK_RESULT_MAX

    def test_only_the_first_block_is_honored(self):
        cleaned, req, errs = parse_task_block(
            _task({"taskId": "1", "result": "a"}) + " mid " + _task({"taskId": "2", "result": "b"})
        )
        assert cleaned == "mid"
        assert req.task_id == "1"
        assert len(errs) == 1 and "extra" in errs[0]

    def test_malformed_first_block_does_not_fall_through_to_the_second(self):
        _, req, errs = parse_task_block(_task("{bad") + _task({"taskId": "2", "result": "b"}))
        assert req is None
        assert len(errs) == 2

    def test_fenced_example_is_not_a_call(self):
        text = "~~~\n" + _task({"taskId": "1", "result": "a"}) + "\n~~~"
        assert parse_task_block(text) == (text, None, [])


class TestStripForStreaming:
    def test_complete_blocks_are_removed(self):
        text = "Hi [[BGOS_UI]]open_chat[[/BGOS_UI]] there " + _task({"taskId": "1", "result": "a"})
        assert strip_app_control_markers(text) == "Hi  there"

    def test_a_block_still_streaming_is_hidden_on_interim_edits(self):
        f = strip_app_control_markers
        assert f("Opening now [[BGOS_UI]]show_wo", hide_open_tail=True) == "Opening now"
        assert f('Ok [[BGOS_TASK]]{"taskId":', hide_open_tail=True) == "Ok"

    def test_an_unclosed_tag_in_final_text_is_prose_and_stays(self):
        text = "Use the [[BGOS_UI]] marker to open panels. Anything else?"
        assert strip_app_control_markers(text) == text

    def test_tail_hiding_ignores_code(self):
        text = "Example:\n```\n[[BGOS_UI]]open_chat\n```\nDone."
        assert strip_app_control_markers(text, hide_open_tail=True) == text
        inline = "Write `[[BGOS_UI]]` then the action"
        assert strip_app_control_markers(inline, hide_open_tail=True) == inline

    def test_tail_hiding_leaves_a_closed_last_block_alone(self):
        assert strip_app_control_markers(
            "a [[BGOS_UI]]open_chat[[/BGOS_UI]] b", hide_open_tail=True,
        ) == "a  b"

    def test_plain_text_is_untouched(self):
        assert strip_app_control_markers("[[BGOS_BUTTONS]]x | y[[/BGOS_BUTTONS]]") == (
            "[[BGOS_BUTTONS]]x | y[[/BGOS_BUTTONS]]"
        )
        assert strip_app_control_markers("") == ""


# ── adapter wiring (mock backend) ──────────────────────────────────────────



async def _adapter(mock_bgos_server) -> BGOSAdapter:
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 300})
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    adapter._state.record_inbound_chat(11)

    class _NoopHandler:
        async def handle(self, frame):
            return None

    adapter._voice_rpc = _NoopHandler()  # type: ignore[assignment]
    return adapter


async def _voice_frame(adapter: BGOSAdapter, op: str, session: Any, chat: int = 11) -> None:
    await adapter._handle_voice_rpc(
        {
            "rpcId": f"rpc-{op}",
            "op": op,
            "assistantId": 7,
            "agentRoute": "hades",
            "chatId": chat,
            "payload": {"voiceSessionId": session, "taskId": "5", "args": {"question": "q"}},
        }
    )
    await asyncio.sleep(0)


def _requests(server, method: str, path: str) -> list:
    return [r for r in server.requests if r.method == method and r.path == path]


@pytest.mark.asyncio
async def test_ui_marker_posts_to_the_live_session_with_pairing_auth(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("POST", UI_PATH.format(31)).respond(201, {"accepted": True})
    try:
        await _voice_frame(adapter, "consult", "31")
        result = await adapter.send(
            chat_id=11,
            content="Opening it.\n[[BGOS_UI]]expand_card | invoice[[/BGOS_UI]]",
        )
        assert result.success
        wire = mock_bgos_server.last_request("POST", UI_PATH.format(31))
        assert wire.headers["X-BGOS-Pairing"] == "pair_xyz"
        body = wire.json_body
        assert body["action"] == "expand_card"
        assert body["target"] == "invoice"
        assert len(body["actionId"]) == 32
        assert body["ts"].endswith("+00:00")
        assert body["agentName"] == "Hades"
        msg = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert msg["text"] == "Opening it."
        assert "BGOS_UI" not in json.dumps(msg)
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_dispatch_frame_also_teaches_the_session(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("POST", UI_PATH.format(77)).respond(201, {"accepted": True})
    try:
        await _voice_frame(adapter, "dispatch", "77")
        await adapter.send(chat_id=11, content="Here. [[BGOS_UI]]show_work_stream[[/BGOS_UI]]")
        assert mock_bgos_server.last_request("POST", UI_PATH.format(77)).json_body["action"] == (
            "show_work_stream"
        )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("op, session", [("mint", "31"), ("consult", "31x"), ("consult", 31), ("consult", None)])
async def test_session_is_learned_only_from_a_wellformed_consult_or_dispatch(
    mock_bgos_server, op, session,
):
    adapter = await _adapter(mock_bgos_server)
    try:
        await _voice_frame(adapter, op, session)
        assert adapter._voice_session_by_chat == {}
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_ui_marker_without_a_live_call_sends_nothing_but_the_reply(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    try:
        result = await adapter.send(chat_id=11, content="Sure. [[BGOS_UI]]open_chat[[/BGOS_UI]]")
        assert result.success
        assert not [r for r in mock_bgos_server.requests if "ui-action" in r.path]
        assert mock_bgos_server.last_request("POST", "/api/v1/messages").json_body["text"] == "Sure."
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_session_is_per_chat(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(12)
    try:
        await _voice_frame(adapter, "consult", "31", chat=12)
        await adapter.send(chat_id=11, content="x [[BGOS_UI]]open_chat[[/BGOS_UI]]")
        assert not [r for r in mock_bgos_server.requests if "ui-action" in r.path]
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_malformed_ui_marker_is_stripped_and_not_sent(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    try:
        await _voice_frame(adapter, "consult", "31")
        await adapter.send(chat_id=11, content="Hmm. [[BGOS_UI]]navigate | /settings[[/BGOS_UI]]")
        assert not [r for r in mock_bgos_server.requests if "ui-action" in r.path]
        assert mock_bgos_server.last_request("POST", "/api/v1/messages").json_body["text"] == "Hmm."
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["tidy", "everything"])
async def test_marker_only_reply_posts_no_empty_bubble(mock_bgos_server, style):
    # "everything" skips the quiet path's own empty-text guard, so this is the
    # style where the marker's early return is what keeps the bubble out.
    from hermes_channel_bgos.quiet_mode import EVERYTHING

    adapter = await _adapter(mock_bgos_server)
    if style == "everything":
        adapter._chat_style_for = lambda chat_key: EVERYTHING  # type: ignore[method-assign]
    mock_bgos_server.on("POST", UI_PATH.format(31)).respond(201, {"accepted": True})
    try:
        await _voice_frame(adapter, "consult", "31")
        result = await adapter.send(chat_id=11, content="[[BGOS_UI]]open_transcript[[/BGOS_UI]]")
        assert result.success
        assert _requests(mock_bgos_server, "POST", "/api/v1/messages") == []
        assert len(_requests(mock_bgos_server, "POST", UI_PATH.format(31))) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, body", [(201, {"accepted": False}), (500, {"error": "boom"})])
async def test_ui_failure_or_drop_never_breaks_the_reply(mock_bgos_server, status, body):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("POST", UI_PATH.format(31)).respond(status, body)
    try:
        await _voice_frame(adapter, "consult", "31")
        result = await adapter.send(chat_id=11, content="Done. [[BGOS_UI]]close_chat[[/BGOS_UI]]")
        assert result.success
        assert mock_bgos_server.last_request("POST", "/api/v1/messages").json_body["text"] == "Done."
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_task_marker_posts_the_complete_voice_task_body(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("POST", TASK_PATH.format(42)).respond(201, {"ok": True})
    try:
        result = await adapter.send(
            chat_id=11,
            content="I could not book it. "
            + _task({"taskId": "42", "result": "Calendar is read only.", "failed": True}),
        )
        assert result.success
        wire = mock_bgos_server.last_request("POST", TASK_PATH.format(42))
        assert wire.headers["X-BGOS-Pairing"] == "pair_xyz"
        assert wire.json_body == {
            "ok": False,
            "error": {"code": "DISPATCH_FAILED", "message": "Calendar is read only."},
        }
        assert adapter._task_settled_by_marker("42")
        msg = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert msg["text"] == "I could not book it."
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_failed_task_post_leaves_the_automatic_report_as_fallback(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("POST", TASK_PATH.format(42)).respond(500, {"error": "down"})
    try:
        result = await adapter.send(chat_id=11, content="ok " + _task({"taskId": "42", "result": "x"}))
        assert result.success
        assert not adapter._task_settled_by_marker("42")
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_malformed_task_marker_posts_nothing(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    try:
        await adapter.send(chat_id=11, content="ok " + _task({"taskId": "abc", "result": "x"}))
        assert not [r for r in mock_bgos_server.requests if "voice-tasks" in r.path]
        assert mock_bgos_server.last_request("POST", "/api/v1/messages").json_body["text"] == "ok"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_interim_streaming_edits_hide_markers_and_never_execute_them(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("PATCH", "/api/v1/messages/300").respond(200, {"id": 300})
    try:
        await _voice_frame(adapter, "consult", "31")
        await adapter.edit_message(
            chat_id=11, message_id=300,
            content="Opening [[BGOS_UI]]open_chat[[/BGOS_UI]] " + _task({"taskId": "1", "result": "x"}),
        )
        await adapter.edit_message(chat_id=11, message_id=300, content="Opening [[BGOS_UI]]open_ch")
        await asyncio.sleep(adapter._edit_throttle_seconds + 0.3)
        patches = _requests(mock_bgos_server, "PATCH", "/api/v1/messages/300")
        assert patches
        for p in patches:
            assert "BGOS_" not in json.dumps(p.json_body)
        assert not [r for r in mock_bgos_server.requests if "ui-action" in r.path or "voice-tasks" in r.path]
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_a_streamed_reply_runs_its_markers_on_the_finalize_edit(mock_bgos_server):
    # Without a fresh-final preference the gateway delivers the final text as
    # edit_message(finalize=True), never a second send().
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("PATCH", "/api/v1/messages/300").respond(200, {"id": 300})
    mock_bgos_server.on("POST", UI_PATH.format(31)).respond(201, {"accepted": True})
    mock_bgos_server.on("POST", TASK_PATH.format(5)).respond(201, {"ok": True})
    try:
        await _voice_frame(adapter, "dispatch", "31")
        await adapter.edit_message(
            chat_id=11, message_id=300, finalize=True,
            content="Could not book. [[BGOS_UI]]show_work_stream[[/BGOS_UI]] "
            + _task({"taskId": "5", "result": "Closed.", "failed": True}),
        )
        assert len(_requests(mock_bgos_server, "POST", UI_PATH.format(31))) == 1
        task = mock_bgos_server.last_request("POST", TASK_PATH.format(5)).json_body
        assert task["ok"] is False
        assert adapter._task_settled_by_marker("5")
        patch = mock_bgos_server.last_request("PATCH", "/api/v1/messages/300").json_body
        assert patch["text"] == "Could not book."
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_preview_send_then_final_edit_runs_each_block_once(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("PATCH", "/api/v1/messages/300").respond(200, {"id": 300})
    mock_bgos_server.on("POST", UI_PATH.format(31)).respond(201, {"accepted": True})
    try:
        await _voice_frame(adapter, "consult", "31")
        block = "[[BGOS_UI]]open_transcript[[/BGOS_UI]]"
        await adapter.send(chat_id=11, content="Opening. " + block)
        await adapter.edit_message(
            chat_id=11, message_id=300, finalize=True, content="Opening. " + block + " Done.",
        )
        # A fallback fresh send of the same final text must not re-run it.
        await adapter.send(chat_id=11, content="Opening. " + block + " Done.")
        assert len(_requests(mock_bgos_server, "POST", UI_PATH.format(31))) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_a_stale_session_is_forgotten(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    try:
        await _voice_frame(adapter, "consult", "31")
        sid, seen = adapter._voice_session_by_chat[11]
        adapter._voice_session_by_chat[11] = (sid, seen - adapter._VOICE_SESSION_TTL_SECONDS - 1)
        await adapter.send(chat_id=11, content="x [[BGOS_UI]]open_chat[[/BGOS_UI]]")
        assert not [r for r in mock_bgos_server.requests if "ui-action" in r.path]
        assert 11 not in adapter._voice_session_by_chat
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_marker_only_task_reply_feeds_the_voice_capture(mock_bgos_server):
    adapter = await _adapter(mock_bgos_server)
    mock_bgos_server.on("POST", TASK_PATH.format(8)).respond(500, {"error": "down"})
    offered: list[tuple[int, str]] = []
    adapter._offer_voice_reply = lambda chat, text: offered.append((chat, text))  # type: ignore[method-assign]
    try:
        await adapter.send(chat_id=11, content=_task({"taskId": "8", "result": "Booked for 9am."}))
        assert offered == [(11, "Booked for 9am.")]
    finally:
        await adapter.disconnect()


# ── dispatch runner skip ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("settled, expected_posts", [(True, 0), (False, 1)])
async def test_dispatch_runner_skips_a_task_the_marker_settled(settled, expected_posts):
    posts: list[tuple[str, dict]] = []

    async def post_voice_task_result(task_id, body):
        posts.append((task_id, body))

    async def noop(*_a, **_k):
        return None

    async def run_brain_turn(chat_id, text, deadline):
        return "summary"

    deps = VoiceRpcDeps(
        post_ack=noop,
        post_result=noop,
        post_voice_task_result=post_voice_task_result,
        run_brain_turn=run_brain_turn,
        voice_config=lambda aid: VoiceConfig(
            openai_api_key="", model="m", voice="v", persona="", agent_name="A",
        ),
        task_already_reported=lambda tid: settled and tid == "9",
    )
    handler = VoiceRpcHandler(deps)
    frame = VoiceRpcFrame(
        rpc_id="r", op="dispatch", assistant_id=7, agent_route="hades", chat_id=11,
        payload={"taskId": "9", "args": {"question": "do it"}, "confirmed": True},
    )
    await handler._run_dispatch(frame, "9")
    assert len(posts) == expected_posts


# ── standalone (out-of-process) send ───────────────────────────────────────


@pytest.mark.asyncio
async def test_standalone_send_strips_the_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")
    from hermes_channel_bgos import plugin as plugin_mod

    captured: dict = {}

    class FakeApi:
        def __init__(self, config):
            pass

        async def get_chat(self, chat_id):
            raise RuntimeError("no chat lookup in this test")

        async def post_message(self, *, chat_id, text, **kw):
            captured["text"] = text
            return {"id": 1}

        async def close(self):
            pass

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)
    result = await plugin_mod.standalone_send(
        None, "830",
        "Cron ran. [[BGOS_UI]]open_chat[[/BGOS_UI]] " + _task({"taskId": "1", "result": "x"}),
    )
    assert result["success"] is True
    assert captured["text"] == "Cron ran."
    await plugin_mod.standalone_send(None, "830", "Use the [[BGOS_UI]] marker on a call.")
    assert captured["text"] == "Use the [[BGOS_UI]] marker on a call."
