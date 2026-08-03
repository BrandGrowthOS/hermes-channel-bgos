"""Pure functions for the [[BGOS_BOARDS]] marker round trip (Agent Boards on
the Hermes channel).

Every other Hermes capability marker is fire and forget. Boards is not: a
query must return rows TO the agent, so the marker layer here is a REQUEST
half (parse + plan) and a RESULT half (correlate + format), both pure. These
tests pin the protocol's behaviour, not its wording:

- a well formed block parses into a BoardsRequest and is stripped;
- a malformed block becomes a BoardsParseError and is STILL stripped (the
  user must never see marker syntax, and silence would strand the agent);
- a denial from the backend is passed through verbatim in the result turn;
- a result is correlated to the request that asked for it by reqId.
"""
import json
import re

import pytest

from hermes_channel_bgos.boards_marker import (
    BOARDS_LOOP_GUARD_LIMIT,
    BOARDS_MAX_BLOCKS,
    BOARDS_OPS,
    BOARDS_RESULT_HEADER,
    BoardsCallResult,
    BoardsParseError,
    BoardsRequest,
    build_result_turn,
    parse_boards_blocks,
    plan_request,
)


def _block(payload: dict) -> str:
    return f"[[BGOS_BOARDS]]{json.dumps(payload)}[[/BGOS_BOARDS]]"


class TestParseBoardsBlocks:
    def test_absent_marker_leaves_text_untouched(self):
        # The overwhelmingly common case: a normal reply must pass through
        # unchanged, with nothing parsed and nothing to report.
        text = "Just a normal reply, no boards involved."
        cleaned, requests, errors = parse_boards_blocks(text)
        assert cleaned == text
        assert requests == []
        assert errors == []

    def test_well_formed_query_block_is_parsed_and_stripped(self):
        text = (
            "Let me check the board.\n"
            + _block({"op": "query", "board": "Tasks", "reqId": "q1", "limit": 20})
        )
        cleaned, requests, errors = parse_boards_blocks(text)
        assert cleaned == "Let me check the board."
        assert errors == []
        assert len(requests) == 1
        req = requests[0]
        assert req.op == "query"
        assert req.req_id == "q1"
        assert req.args["board"] == "Tasks"
        assert req.args["limit"] == 20
        assert "BGOS_BOARDS" not in cleaned

    def test_missing_req_id_gets_a_minted_one(self):
        # Correlation must survive an agent that forgot reqId: the adapter
        # mints one so the result section is still attributable.
        _, requests, _ = parse_boards_blocks(_block({"op": "list"}))
        assert len(requests) == 1
        assert re.fullmatch(r"b-[0-9a-f]{8}", requests[0].req_id)

    def test_invalid_json_is_an_error_and_still_stripped(self):
        text = "Before\n[[BGOS_BOARDS]]{not json}[[/BGOS_BOARDS]]\nAfter"
        cleaned, requests, errors = parse_boards_blocks(text)
        assert requests == []
        assert len(errors) == 1
        assert "BGOS_BOARDS" not in cleaned
        assert "Before" in cleaned and "After" in cleaned

    def test_unknown_op_is_an_error(self):
        _, requests, errors = parse_boards_blocks(
            _block({"op": "drop_table", "board": "Tasks"})
        )
        assert requests == []
        assert len(errors) == 1
        assert "drop_table" in errors[0].message

    def test_missing_required_field_is_an_error(self):
        # update needs board, row and cells; leaving row out must not plan a
        # REST call that would 404 confusingly.
        _, requests, errors = parse_boards_blocks(
            _block({"op": "update", "board": "Tasks", "cells": {"status": "done"}})
        )
        assert requests == []
        assert len(errors) == 1
        assert "row" in errors[0].message

    def test_more_than_max_blocks_truncates_with_errors(self):
        text = "\n".join(
            _block({"op": "list", "reqId": f"r{i}"})
            for i in range(BOARDS_MAX_BLOCKS + 2)
        )
        cleaned, requests, errors = parse_boards_blocks(text)
        assert len(requests) == BOARDS_MAX_BLOCKS
        assert len(errors) == 2
        assert "BGOS_BOARDS" not in cleaned

    def test_code_fenced_example_is_left_intact(self):
        # Documenting the convention to the user must not fire a real call,
        # same discipline as the MEDIA: parser.
        fenced = (
            "Here is the syntax:\n```\n"
            + _block({"op": "list"})
            + "\n```\nNo call intended."
        )
        cleaned, requests, errors = parse_boards_blocks(fenced)
        assert requests == []
        assert errors == []
        assert "[[BGOS_BOARDS]]" in cleaned

    def test_non_string_req_id_is_coerced(self):
        _, requests, _ = parse_boards_blocks(
            _block({"op": "list", "reqId": 7})
        )
        assert requests[0].req_id == "7"

    def test_non_object_payload_is_an_error(self):
        _, requests, errors = parse_boards_blocks(
            "[[BGOS_BOARDS]][1, 2, 3][[/BGOS_BOARDS]]"
        )
        assert requests == []
        assert len(errors) == 1


class TestPlanRequest:
    def _plan(self, op: str, **args):
        return plan_request(BoardsRequest(req_id="r", op=op, args=args))

    def test_the_twelve_ops_are_exactly_the_claude_roster(self):
        assert BOARDS_OPS == frozenset(
            {
                "list", "describe", "create", "update_schema", "query",
                "get_row", "insert", "update", "attach", "search",
                "changes", "grant",
            }
        )

    def test_list(self):
        plan = self._plan("list")
        assert (plan.method, plan.path) == ("GET", "")
        assert plan.params == {"format": "markdown"}
        assert plan.kind == "rest"

    def test_describe(self):
        plan = self._plan("describe", board="Tasks")
        assert (plan.method, plan.path) == ("GET", "/Tasks/describe")
        assert plan.params == {"format": "markdown"}

    def test_board_segment_is_url_quoted(self):
        plan = self._plan("describe", board="Q3 Roadmap")
        assert plan.path == "/Q3%20Roadmap/describe"

    def test_create(self):
        plan = self._plan(
            "create", name="Bugs", fields=[{"label": "Title", "type": "text"}],
        )
        assert (plan.method, plan.path) == ("POST", "")
        assert plan.json == {
            "name": "Bugs",
            "fields": [{"label": "Title", "type": "text"}],
        }

    def test_query_defaults_to_markdown_format(self):
        plan = self._plan("query", board="Tasks", limit=10)
        assert (plan.method, plan.path) == ("POST", "/Tasks/rows/query")
        assert plan.params == {"format": "markdown"}
        assert plan.json == {"limit": 10}

    def test_query_honors_json_format(self):
        plan = self._plan("query", board="Tasks", format="json")
        assert plan.params == {"format": "json"}
        # format is a transport concern, never part of the body.
        assert "format" not in (plan.json or {})

    def test_get_row(self):
        plan = self._plan("get_row", board="Tasks", row="ab12cd34")
        assert (plan.method, plan.path) == ("GET", "/Tasks/rows/ab12cd34")
        assert plan.params == {"format": "markdown"}

    def test_insert(self):
        plan = self._plan("insert", board="Tasks", cells={"title": "x"})
        assert (plan.method, plan.path) == ("POST", "/Tasks/rows")
        assert plan.json == {"cells": {"title": "x"}}

    def test_update(self):
        plan = self._plan(
            "update", board="Tasks", row="ab12cd34", cells={"status": "done"},
        )
        assert (plan.method, plan.path) == ("PATCH", "/Tasks/rows/ab12cd34")
        assert plan.json == {"cells": {"status": "done"}}

    def test_search(self):
        plan = self._plan("search", board="Tasks", query="stale rows", limit=5)
        assert (plan.method, plan.path) == ("POST", "/Tasks/search")
        assert plan.json == {"query": "stale rows", "limit": 5}
        assert plan.params == {"format": "markdown"}

    def test_changes(self):
        plan = self._plan("changes", board="Tasks", since="41")
        assert (plan.method, plan.path) == ("GET", "/Tasks/changes")
        assert plan.params == {"format": "markdown", "since": "41"}

    def test_grant(self):
        plan = self._plan("grant", board="Tasks", assistantId=944, role="read")
        assert (plan.method, plan.path) == ("POST", "/Tasks/grants")
        assert plan.json == {"assistantId": 944, "role": "read"}
        # Writes have no format knob; nothing to ask for.
        assert plan.params is None

    def test_update_schema_add_field(self):
        plan = self._plan(
            "update_schema", board="Tasks", action="add_field",
            field={"label": "Owner", "type": "text"},
        )
        assert (plan.method, plan.path) == ("POST", "/Tasks/fields")
        assert plan.json == {"label": "Owner", "type": "text"}

    def test_update_schema_update_field(self):
        plan = self._plan(
            "update_schema", board="Tasks", action="update_field",
            fieldKey="owner", field={"label": "Assignee"},
        )
        assert (plan.method, plan.path) == ("PATCH", "/Tasks/fields/owner")
        assert plan.json == {"label": "Assignee"}

    def test_update_schema_delete_field(self):
        plan = self._plan(
            "update_schema", board="Tasks", action="delete_field",
            fieldKey="owner",
        )
        assert (plan.method, plan.path) == ("DELETE", "/Tasks/fields/owner")
        assert plan.json is None

    def test_attach_is_a_sentinel_plan(self):
        # The adapter, not the REST lane, executes attach (file read, inline
        # or presigned upload); the plan only flags it.
        plan = self._plan(
            "attach", board="Tasks", row="ab12cd34", path="/tmp/report.pdf",
        )
        assert plan.kind == "attach"


class TestParseValidatesAgainstPlan:
    """Required-field validation happens at parse time so a bad block never
    reaches the REST lane."""

    @pytest.mark.parametrize(
        "payload,missing",
        [
            ({"op": "describe"}, "board"),
            ({"op": "create"}, "name"),
            ({"op": "query"}, "board"),
            ({"op": "get_row", "board": "T"}, "row"),
            ({"op": "insert", "board": "T"}, "cells"),
            ({"op": "attach", "board": "T", "row": "r"}, "path"),
            ({"op": "search", "board": "T"}, "query"),
            ({"op": "changes"}, "board"),
            ({"op": "grant", "board": "T", "role": "read"}, "assistantId"),
            ({"op": "update_schema", "board": "T"}, "action"),
        ],
    )
    def test_missing_field_yields_parse_error(self, payload, missing):
        _, requests, errors = parse_boards_blocks(_block(payload))
        assert requests == []
        assert len(errors) == 1
        assert missing in errors[0].message


class TestBuildResultTurn:
    def test_opens_with_the_provenance_header(self):
        turn = build_result_turn(
            [BoardsCallResult(req_id="q1", op="list", ok=True, status=200,
                              body={"markdown": "| Board |"})]
        )
        assert turn.startswith(BOARDS_RESULT_HEADER)

    def test_ok_result_carries_markdown_body_verbatim(self):
        markdown = "| key | Title |\n| --- | --- |\n| ab12cd34 | Fix login |"
        turn = build_result_turn(
            [BoardsCallResult(req_id="q1", op="query", ok=True, status=200,
                              body={"markdown": markdown})]
        )
        assert "reqId=q1 op=query ok" in turn
        assert markdown in turn

    def test_denial_body_passes_through_verbatim(self):
        # The backend's denial bodies are a leak-proof contract; the result
        # turn must carry them byte for byte, never a paraphrase.
        body = {
            "error": "not_found_board",
            "message": "No board by that name is visible to you.",
        }
        turn = build_result_turn(
            [BoardsCallResult(req_id="w2", op="update", ok=False, status=404,
                              body=body)]
        )
        assert "reqId=w2 op=update error status=404" in turn
        assert json.dumps(body) in turn

    def test_results_correlate_to_their_own_request(self):
        # Two results in one turn: each body must sit under ITS reqId header,
        # in request order.
        turn = build_result_turn(
            [
                BoardsCallResult(req_id="a", op="query", ok=True, status=200,
                                 body={"markdown": "ALPHA-ROWS"}),
                BoardsCallResult(req_id="b", op="get_row", ok=False, status=403,
                                 body={"error": "forbidden_tool",
                                       "message": "read only"}),
            ]
        )
        a_at = turn.index("reqId=a op=query ok")
        alpha_at = turn.index("ALPHA-ROWS")
        b_at = turn.index("reqId=b op=get_row error status=403")
        assert a_at < alpha_at < b_at

    def test_non_dict_string_body_is_rendered_as_text(self):
        turn = build_result_turn(
            [BoardsCallResult(req_id="s", op="describe", ok=True, status=200,
                              body="plain text answer")]
        )
        assert "plain text answer" in turn

    def test_loop_guard_limit_is_sane(self):
        assert 1 < BOARDS_LOOP_GUARD_LIMIT <= 20


class TestUpdateSchemaFieldShape:
    def test_field_must_be_an_object(self):
        # A string field used to slip through parse and blow up inside
        # plan_request as a misleading transport_error (review finding
        # 2026-08-03). The agent must learn it sent the wrong shape.
        _, requests, errors = parse_boards_blocks(
            _block(
                {
                    "op": "update_schema",
                    "board": "Tasks",
                    "action": "add_field",
                    "field": "Status",
                }
            )
        )
        assert requests == []
        assert len(errors) == 1
        assert "field" in errors[0].message
        assert "object" in errors[0].message
