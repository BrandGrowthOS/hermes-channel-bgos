"""Pure protocol tests for the [[BGOS_PEER]] marker round trip."""
import json
import re

import pytest

from hermes_channel_bgos.peer_marker import (
    PEER_LOOP_GUARD_LIMIT,
    PEER_MAX_BLOCKS,
    PEER_OPS,
    PEER_RESULT_HEADER,
    PeerCallResult,
    PeerRequest,
    build_result_turn,
    parse_peer_blocks,
    plan_request,
)


def _block(payload: dict) -> str:
    return f"[[BGOS_PEER]]{json.dumps(payload)}[[/BGOS_PEER]]"


class TestParsePeerBlocks:
    def test_absent_marker_leaves_text_untouched(self):
        text = "A normal answer with no peer operation."
        cleaned, requests, errors = parse_peer_blocks(text)
        assert cleaned == text
        assert requests == []
        assert errors == []

    def test_valid_send_is_parsed_and_stripped_from_visible_text(self):
        text = (
            "I will ask Hades.\n"
            + _block(
                {
                    "op": "send_to_peer",
                    "target": "Hades",
                    "text": "Please inspect the deploy.",
                    "waitForReply": True,
                    "timeoutSeconds": 30,
                    "reqId": "send-1",
                }
            )
        )
        cleaned, requests, errors = parse_peer_blocks(text)
        assert cleaned == "I will ask Hades."
        assert "BGOS_PEER" not in cleaned
        assert errors == []
        assert len(requests) == 1
        assert requests[0].req_id == "send-1"
        assert requests[0].op == "send_to_peer"
        assert requests[0].args == {
            "target": "Hades",
            "text": "Please inspect the deploy.",
            "waitForReply": True,
            "timeoutSeconds": 30,
        }

    def test_malformed_json_is_an_error_and_is_still_stripped(self):
        text = "Before\n[[BGOS_PEER]]{oops[[/BGOS_PEER]]\nAfter"
        cleaned, requests, errors = parse_peer_blocks(text)
        assert requests == []
        assert len(errors) == 1
        assert "invalid JSON" in errors[0].message
        assert "BGOS_PEER" not in cleaned
        assert "Before" in cleaned
        assert "After" in cleaned

    def test_unclosed_block_is_an_error_and_cannot_leak_marker_or_payload(self):
        text = 'Visible first.\n[[BGOS_PEER]]{"op":"list_peers","reqId":"lost"}'
        cleaned, requests, errors = parse_peer_blocks(text)
        assert cleaned == "Visible first."
        assert requests == []
        assert len(errors) == 1
        assert "missing closing" in errors[0].message
        assert "BGOS_PEER" not in cleaned
        assert "list_peers" not in cleaned

    def test_orphan_closing_marker_is_redacted_silently(self):
        text = "Before [[/BGOS_PEER]] after"
        cleaned, requests, errors = parse_peer_blocks(text)
        assert cleaned == "Before  after"
        assert requests == []
        assert errors == []
        assert "BGOS_PEER" not in cleaned

    def test_bare_mention_keeps_prose_and_sends_no_error(self):
        # First live specimen (Athena, 2026-08-10): quoting the marker name
        # while describing guidance ate the rest of her message and fired a
        # malformed_request round trip. A mention is not an attempt.
        text = (
            "My guidance describes these capabilities:\n"
            "- `[[BGOS_PEER]]` for peer operations\n"
            "- boards and the call marker as before."
        )
        cleaned, requests, errors = parse_peer_blocks(text)
        assert requests == []
        assert errors == []
        assert "BGOS_PEER" not in cleaned
        assert "for peer operations" in cleaned
        assert "boards and the call marker as before." in cleaned

    def test_mention_then_real_block_still_executes(self):
        text = (
            "The [[BGOS_PEER]] marker works like this.\n"
            '[[BGOS_PEER]]{"op":"list_peers","reqId":"ok1"}[[/BGOS_PEER]]'
        )
        cleaned, requests, errors = parse_peer_blocks(text)
        assert len(requests) == 1
        assert requests[0].req_id == "ok1"
        assert errors == []
        assert "BGOS_PEER" not in cleaned
        assert "marker works like this." in cleaned

    def test_multiple_blocks_keep_request_order_and_strip_all_markers(self):
        text = "\n".join(
            [
                "Checking with the team.",
                _block({"op": "list_peers", "reqId": "one"}),
                _block(
                    {
                        "op": "peer_status",
                        "target": 17,
                        "reqId": "two",
                    }
                ),
                _block(
                    {
                        "op": "complete_peer_thread",
                        "target": "Hades",
                        "summary": "Deploy checked.",
                        "reqId": "three",
                    }
                ),
            ]
        )
        cleaned, requests, errors = parse_peer_blocks(text)
        assert cleaned == "Checking with the team."
        assert errors == []
        assert [request.req_id for request in requests] == ["one", "two", "three"]
        assert [request.op for request in requests] == [
            "list_peers",
            "peer_status",
            "complete_peer_thread",
        ]

    def test_missing_req_id_gets_an_attributable_minted_id(self):
        _, requests, errors = parse_peer_blocks(_block({"op": "list_peers"}))
        assert errors == []
        assert re.fullmatch(r"p-[0-9a-f]{8}", requests[0].req_id)

    def test_numeric_string_target_is_normalised_to_an_id(self):
        _, requests, errors = parse_peer_blocks(
            _block({"op": "peer_status", "target": "894", "reqId": "s"})
        )
        assert errors == []
        assert requests[0].args["target"] == 894

    def test_non_object_payload_is_an_error(self):
        cleaned, requests, errors = parse_peer_blocks(
            "[[BGOS_PEER]][1, 2][[/BGOS_PEER]]"
        )
        assert cleaned == ""
        assert requests == []
        assert len(errors) == 1
        assert "JSON object" in errors[0].message

    def test_code_fenced_example_stays_visible_and_does_not_execute(self):
        fenced = (
            "Syntax example:\n```\n"
            + _block({"op": "list_peers", "reqId": "example"})
            + "\n```"
        )
        cleaned, requests, errors = parse_peer_blocks(fenced)
        assert cleaned == fenced
        assert requests == []
        assert errors == []

    def test_excess_blocks_are_stripped_and_answered_as_errors(self):
        text = "\n".join(
            _block({"op": "list_peers", "reqId": f"r{index}"})
            for index in range(PEER_MAX_BLOCKS + 2)
        )
        cleaned, requests, errors = parse_peer_blocks(text)
        assert cleaned == ""
        assert len(requests) == PEER_MAX_BLOCKS
        assert [error.req_id for error in errors] == ["r5", "r6"]

    @pytest.mark.parametrize(
        "payload,message",
        [
            ({"op": "unknown"}, "unknown op"),
            ({"op": "peer_status"}, "target"),
            ({"op": "peer_status", "target": 0}, "positive assistant id"),
            (
                {"op": "send_to_peer", "target": "Hades"},
                "non-empty string field text",
            ),
            (
                {
                    "op": "send_to_peer",
                    "target": "Hades",
                    "text": "hello",
                    "waitForReply": "yes",
                },
                "boolean",
            ),
            (
                {
                    "op": "send_to_peer",
                    "target": "Hades",
                    "text": "hello",
                    "timeoutSeconds": 51,
                },
                "1 to 50",
            ),
            (
                {
                    "op": "complete_peer_thread",
                    "target": "Hades",
                    "summary": {"done": True},
                },
                "summary",
            ),
        ],
    )
    def test_invalid_request_shape_returns_an_error(self, payload, message):
        _, requests, errors = parse_peer_blocks(_block(payload))
        assert requests == []
        assert len(errors) == 1
        assert message in errors[0].message


class TestPlanRequest:
    def _plan(self, op: str, **args):
        return plan_request(PeerRequest(req_id="r", op=op, args=args))

    def test_ops_match_the_cross_channel_roster(self):
        assert PEER_OPS == frozenset(
            {
                "list_peers",
                "peer_status",
                "send_to_peer",
                "complete_peer_thread",
            }
        )

    def test_list_peers_maps_to_existing_helper(self):
        plan = self._plan("list_peers")
        assert plan.helper == "list_peers"
        assert plan.kwargs == {}
        assert plan.target is None
        assert plan.target_kwarg is None

    def test_peer_status_maps_target_to_helper_argument(self):
        plan = self._plan("peer_status", target=894)
        assert plan.helper == "peer_status"
        assert plan.kwargs == {}
        assert plan.target == 894
        assert plan.target_kwarg == "peer_assistant_id"

    def test_plan_normalises_numeric_string_target(self):
        plan = self._plan("peer_status", target="894")
        assert plan.target == 894

    def test_send_to_peer_maps_all_marker_arguments(self):
        plan = self._plan(
            "send_to_peer",
            target="Hades",
            text="Inspect the deploy.",
            waitForReply=True,
            timeoutSeconds=25,
        )
        assert plan.helper == "send_peer"
        assert plan.target == "Hades"
        assert plan.target_kwarg == "target_assistant_id"
        assert plan.kwargs == {
            "text": "Inspect the deploy.",
            "wait_for_reply": True,
            "timeout_seconds": 25,
        }
        assert "parent_message_id" not in plan.kwargs

    def test_send_to_peer_defaults_to_nonblocking(self):
        plan = self._plan("send_to_peer", target=894, text="Heads up.")
        assert plan.kwargs == {"text": "Heads up.", "wait_for_reply": False}

    def test_complete_peer_thread_maps_to_close_helper(self):
        plan = self._plan(
            "complete_peer_thread",
            target=894,
            summary="Deploy checked.",
        )
        assert plan.helper == "close_peer_conversation"
        assert plan.target == 894
        assert plan.target_kwarg == "peer_assistant_id"
        assert plan.kwargs == {"summary": "Deploy checked."}

    def test_complete_peer_thread_omits_absent_summary(self):
        plan = self._plan("complete_peer_thread", target=894)
        assert plan.kwargs == {}


class TestBuildResultTurn:
    def test_opens_with_fixed_provenance_header(self):
        turn = build_result_turn(
            [
                PeerCallResult(
                    req_id="p1",
                    op="list_peers",
                    ok=True,
                    status=200,
                    body=[{"assistantId": 894, "name": "Hades"}],
                )
            ]
        )
        assert turn.startswith(PEER_RESULT_HEADER)

    @pytest.mark.parametrize(
        "error",
        [
            "contact_unavailable",
            "initiate_not_allowed",
            "cap_exceeded",
            "turn_limit_reached",
            "files_not_allowed",
        ],
    )
    def test_typed_backend_error_body_is_verbatim(self, error):
        body = {
            "error": error,
            "message": f"backend contract wording for {error}",
            "resetAt": "2026-08-10T00:00:00Z",
        }
        turn = build_result_turn(
            [
                PeerCallResult(
                    req_id="deny",
                    op="send_to_peer",
                    ok=False,
                    status=403,
                    body=body,
                )
            ]
        )
        assert "reqId=deny op=send_to_peer error status=403" in turn
        assert json.dumps(body) in turn

    def test_results_render_in_request_order_under_their_req_ids(self):
        turn = build_result_turn(
            [
                PeerCallResult(
                    req_id="first",
                    op="peer_status",
                    ok=True,
                    status=200,
                    body={"online": True},
                ),
                PeerCallResult(
                    req_id="second",
                    op="send_to_peer",
                    ok=True,
                    status=200,
                    body={"status": "sent", "reply": {"text": "Done"}},
                ),
                PeerCallResult(
                    req_id="third",
                    op="complete_peer_thread",
                    ok=True,
                    status=200,
                    body={"closed": True},
                ),
            ]
        )
        first = turn.index("reqId=first")
        second = turn.index("reqId=second")
        third = turn.index("reqId=third")
        assert first < second < third

    def test_string_backend_body_is_not_rewritten(self):
        body = "backend supplied denial wording"
        turn = build_result_turn(
            [
                PeerCallResult(
                    req_id="plain",
                    op="peer_status",
                    ok=False,
                    status=503,
                    body=body,
                )
            ]
        )
        assert body in turn

    def test_loop_guard_limit_matches_boards_safety_shape(self):
        assert 1 < PEER_LOOP_GUARD_LIMIT <= 20
