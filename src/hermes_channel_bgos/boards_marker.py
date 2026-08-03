"""Pure functions for the `[[BGOS_BOARDS]]` marker round trip (Agent Boards).

Every other Hermes capability marker ([[BGOS_ASK]], [[BGOS_BUTTONS]],
[[BGOS_CALL]], MEDIA:) is fire and forget. Boards is not: `boards_query` must
return rows TO the agent mid task, and the only inbound path to a Hermes agent
is a message. The protocol therefore has two pure halves, both here, both free
of I/O so they unit test cleanly:

REQUEST half. The agent embeds one JSON object per block in its normal reply:

    [[BGOS_BOARDS]]{"op":"query","board":"Tasks","reqId":"q1","limit":20}[[/BGOS_BOARDS]]

`parse_boards_blocks` strips every block from the visible text (the user must
never see marker syntax) and returns well formed blocks as `BoardsRequest`
and malformed ones as `BoardsParseError`. Unlike the mission marker, invalid
JSON is NOT silently ignored: the agent is waiting on data, so a malformed
block must come back as an error section or the round trip strands.
`plan_request` maps a request to the exact REST call on the agent family
routes (`/api/v1/integrations/assistants/:id/boards...`); the adapter's api
client prefixes that root, executes, and never re-derives paths.

RESULT half. The adapter dispatches ONE synthetic system turn back into the
agent's session (same mechanism as the voice lane's `_dispatch_voice_turn`).
`build_result_turn` renders it: a fixed provenance header, then one
`### reqId=<id> op=<op>` section per call in request order. Backend bodies,
markdown and denials alike, pass through VERBATIM: the boards denial wording
is a leak proof contract (see backend boards-access.filter.ts) and must never
be paraphrased by a channel.

Ops mirror the Claude Code tool roster exactly so the capability canon reads
the same across channels. Spec:
BGOS/docs/superpowers/specs/2026-08-03-hermes-boards-marker-design.md
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

# Same delimiter family as the other agent emitted markers, same regex
# discipline as _BUTTONS_BLOCK_RE in bgos_adapter.py.
BOARDS_BLOCK_RE = re.compile(
    r"\[\[BGOS_BOARDS\]\](.*?)\[\[/BGOS_BOARDS\]\]",
    re.IGNORECASE | re.DOTALL,
)

# Fenced code protection, mirrors the MEDIA: parser: a documented example
# inside ``` or ~~~ must render, not fire a real call.
_CODE_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)

# The fixed first line of every result turn. The canon tells the agent that a
# turn starting with this is the adapter answering its own requests, not the
# user speaking.
BOARDS_RESULT_HEADER = "[BGOS boards result]"

# The provenance paragraph under the header. One place so the adapter and the
# tests never drift on wording.
BOARDS_RESULT_PREAMBLE = (
    "This is a system message from the BGOS adapter, not the user. It answers "
    "the [[BGOS_BOARDS]] requests in your previous reply. Do not thank the "
    "user for it. Continue the task; anything you want the user to see must "
    "be a normal reply."
)

# One reply carries at most this many board calls. More almost certainly
# means a runaway generation; the excess blocks are stripped and answered
# with an error section rather than executed.
BOARDS_MAX_BLOCKS = 5

# Consecutive board result turns per chat before the adapter refuses further
# calls until a real inbound message arrives. Chained queries are intended
# (a result turn may contain new blocks); an unbounded ping pong is not.
BOARDS_LOOP_GUARD_LIMIT = 6

# The op roster, mirroring the 12 Claude Code boards_* tools name for name.
BOARDS_OPS = frozenset(
    {
        "list",
        "describe",
        "create",
        "update_schema",
        "query",
        "get_row",
        "insert",
        "update",
        "attach",
        "search",
        "changes",
        "grant",
    }
)

# Required payload fields per op, checked at parse time so a bad block never
# reaches the REST lane and 404s confusingly. `op` itself is implicit.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "list": (),
    "describe": ("board",),
    "create": ("name",),
    "update_schema": ("board", "action"),
    "query": ("board",),
    "get_row": ("board", "row"),
    "insert": ("board", "cells"),
    "update": ("board", "row", "cells"),
    "attach": ("board", "row", "path"),
    "search": ("board", "query"),
    "changes": ("board",),
    "grant": ("board", "assistantId", "role"),
}

# Ops whose endpoint accepts the ?format= knob (the read family). Writes have
# no format and get params=None.
_FORMAT_OPS = frozenset({"list", "describe", "query", "get_row", "search", "changes"})

# update_schema fans out to three REST shapes on `action`.
_SCHEMA_ACTIONS: dict[str, str] = {
    "add_field": "POST",
    "update_field": "PATCH",
    "delete_field": "DELETE",
}

# Body keys forwarded per op (everything else in the payload is dropped, the
# transport keys op/reqId/board/row/format never belong in a request body).
_BODY_KEYS: dict[str, tuple[str, ...]] = {
    "create": ("name", "description", "fields"),
    "query": ("conditions", "conjunction", "sorts", "search", "limit", "cursor",
              "clientToday"),
    "insert": ("cells",),
    "update": ("cells",),
    "search": ("query", "limit"),
    "grant": ("assistantId", "role"),
}


@dataclass
class BoardsRequest:
    """One well formed board call the agent asked for. `args` is the parsed
    JSON payload minus nothing: `plan_request` picks what it needs."""

    req_id: str
    op: str
    args: dict


@dataclass
class BoardsParseError:
    """One block that could not become a request. Stripped from the visible
    text like a good block, and answered with an error section so the agent
    learns what was wrong instead of waiting forever."""

    req_id: str
    raw: str
    message: str


@dataclass
class RestPlan:
    """The exact REST call for a request. `path` is RELATIVE to the boards
    root (`/api/v1/integrations/assistants/:id/boards`); the api client owns
    the prefix. `kind` is "rest" for the generic lane or "attach" for the
    file upload flow the adapter executes itself."""

    method: str
    path: str
    json: dict | None
    params: dict | None
    kind: str = "rest"


@dataclass
class BoardsCallResult:
    """One executed (or refused) call, ready to render. `body` is whatever
    the backend answered, VERBATIM, or a locally minted
    {"error": ..., "message": ...} dict for adapter side failures."""

    req_id: str
    op: str
    ok: bool
    status: int
    body: Any


def _mint_req_id() -> str:
    return f"b-{uuid.uuid4().hex[:8]}"


def _grammar_reminder() -> str:
    return (
        "Expected one JSON object per block: "
        '[[BGOS_BOARDS]]{"op":"query","board":"<id or exact name>",'
        '"reqId":"q1"}[[/BGOS_BOARDS]] with op one of: '
        + ", ".join(sorted(BOARDS_OPS))
        + "."
    )


def _parse_one_block(raw: str) -> BoardsRequest | BoardsParseError:
    """Parse the inside of one block into a request or an error. Pure."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return BoardsParseError(
            req_id=_mint_req_id(),
            raw=raw,
            message="invalid JSON. " + _grammar_reminder(),
        )
    if not isinstance(payload, dict):
        return BoardsParseError(
            req_id=_mint_req_id(),
            raw=raw,
            message="payload must be a JSON object. " + _grammar_reminder(),
        )
    raw_req_id = payload.get("reqId")
    req_id = str(raw_req_id) if raw_req_id is not None else _mint_req_id()
    op = payload.get("op")
    if not isinstance(op, str) or op not in BOARDS_OPS:
        return BoardsParseError(
            req_id=req_id,
            raw=raw,
            message=f"unknown op {op!r}. " + _grammar_reminder(),
        )
    missing = [
        field
        for field in _REQUIRED_FIELDS[op]
        if payload.get(field) in (None, "")
    ]
    if op == "update_schema":
        action = payload.get("action")
        if action is not None and action not in _SCHEMA_ACTIONS:
            return BoardsParseError(
                req_id=req_id,
                raw=raw,
                message=(
                    f"unknown update_schema action {action!r}; expected one of "
                    + ", ".join(sorted(_SCHEMA_ACTIONS))
                    + "."
                ),
            )
        if action in ("update_field", "delete_field") and not payload.get("fieldKey"):
            missing.append("fieldKey")
        if action in ("add_field", "update_field"):
            field = payload.get("field")
            if not field:
                missing.append("field")
            elif not isinstance(field, dict):
                # A string here used to surface later as a misleading
                # transport_error from plan_request; tell the agent the
                # real problem at parse time instead.
                return BoardsParseError(
                    req_id=req_id,
                    raw=raw,
                    message=(
                        "field must be a JSON object like "
                        '{"label":"Owner","type":"text"}.'
                    ),
                )
    if missing:
        return BoardsParseError(
            req_id=req_id,
            raw=raw,
            message=(
                f"op {op!r} is missing required field(s): "
                + ", ".join(missing)
                + "."
            ),
        )
    args = {k: v for k, v in payload.items() if k not in ("op", "reqId")}
    return BoardsRequest(req_id=req_id, op=op, args=args)


def parse_boards_blocks(
    content: str,
) -> tuple[str, list[BoardsRequest], list[BoardsParseError]]:
    """Extract every `[[BGOS_BOARDS]]...[[/BGOS_BOARDS]]` block from agent
    text.

    Returns `(cleaned_text, requests, errors)`. Every matched block, well
    formed or not, is removed from the visible text (blank line noise
    collapsed); blocks inside code fences are documentation and stay put.
    Blocks beyond BOARDS_MAX_BLOCKS become errors rather than calls.
    """
    if not content or "BGOS_BOARDS" not in content.upper():
        return content, [], []
    segments = _CODE_FENCE_RE.split(content)
    requests: list[BoardsRequest] = []
    errors: list[BoardsParseError] = []
    matched_any = False
    for idx in range(0, len(segments), 2):
        seg = segments[idx]
        if "BGOS_BOARDS" not in seg.upper():
            continue
        for m in BOARDS_BLOCK_RE.finditer(seg):
            matched_any = True
            raw = (m.group(1) or "").strip()
            parsed = _parse_one_block(raw)
            if isinstance(parsed, BoardsRequest):
                if len(requests) < BOARDS_MAX_BLOCKS:
                    requests.append(parsed)
                else:
                    errors.append(
                        BoardsParseError(
                            req_id=parsed.req_id,
                            raw=raw,
                            message=(
                                f"more than {BOARDS_MAX_BLOCKS} board calls in "
                                "one reply; this one was not executed. Batch "
                                "fewer calls per turn."
                            ),
                        )
                    )
            else:
                errors.append(parsed)
        segments[idx] = BOARDS_BLOCK_RE.sub("", seg)
    if not matched_any:
        return content, [], []
    cleaned = "".join(segments)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, requests, errors


def _seg(value: Any) -> str:
    """URL path segment for a board or row identifier."""
    return quote(str(value), safe="")


def _body_for(op: str, args: dict) -> dict | None:
    keys = _BODY_KEYS.get(op, ())
    body = {k: args[k] for k in keys if k in args and args[k] is not None}
    return body or None


def _params_for(op: str, args: dict) -> dict | None:
    if op not in _FORMAT_OPS:
        return None
    fmt = args.get("format")
    params: dict[str, Any] = {
        "format": "json" if fmt == "json" else "markdown",
    }
    if op == "changes" and args.get("since") is not None:
        params["since"] = str(args["since"])
    return params


def plan_request(req: BoardsRequest) -> RestPlan:
    """Map one validated request to its REST call. Validation happened at
    parse time, so every op reaching here is known and complete."""
    op = req.op
    args = req.args
    board = _seg(args.get("board", ""))
    if op == "attach":
        return RestPlan(method="POST", path="", json=None, params=None,
                        kind="attach")
    if op == "list":
        return RestPlan("GET", "", None, _params_for(op, args))
    if op == "describe":
        return RestPlan("GET", f"/{board}/describe", None, _params_for(op, args))
    if op == "create":
        return RestPlan("POST", "", _body_for(op, args), None)
    if op == "query":
        return RestPlan(
            "POST", f"/{board}/rows/query", _body_for(op, args),
            _params_for(op, args),
        )
    if op == "get_row":
        row = _seg(args["row"])
        return RestPlan("GET", f"/{board}/rows/{row}", None, _params_for(op, args))
    if op == "insert":
        return RestPlan("POST", f"/{board}/rows", _body_for(op, args), None)
    if op == "update":
        row = _seg(args["row"])
        return RestPlan("PATCH", f"/{board}/rows/{row}", _body_for(op, args), None)
    if op == "search":
        return RestPlan("POST", f"/{board}/search", _body_for(op, args),
                        _params_for(op, args))
    if op == "changes":
        return RestPlan("GET", f"/{board}/changes", None, _params_for(op, args))
    if op == "grant":
        return RestPlan("POST", f"/{board}/grants", _body_for(op, args), None)
    # update_schema: three REST shapes on `action`.
    action = args["action"]
    method = _SCHEMA_ACTIONS[action]
    if action == "add_field":
        return RestPlan(method, f"/{board}/fields", dict(args["field"]), None)
    field_key = _seg(args["fieldKey"])
    if action == "update_field":
        return RestPlan(method, f"/{board}/fields/{field_key}",
                        dict(args["field"]), None)
    return RestPlan(method, f"/{board}/fields/{field_key}", None, None)


def _render_body(body: Any) -> str:
    """One result section's body text. A markdown answer renders bare (that
    is what the format=markdown contract is FOR); everything else renders as
    compact JSON, verbatim in content."""
    if isinstance(body, dict) and isinstance(body.get("markdown"), str):
        return body["markdown"]
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body)
    except (TypeError, ValueError):
        return repr(body)


def build_result_turn(results: list[BoardsCallResult]) -> str:
    """Render the synthetic system turn answering one reply's board calls.

    Section order equals request order; each section's header carries the
    echoed reqId so the agent can correlate a result to the request that
    asked for it. Bodies are verbatim (see _render_body).
    """
    parts = [BOARDS_RESULT_HEADER, BOARDS_RESULT_PREAMBLE]
    for r in results:
        outcome = "ok" if r.ok else f"error status={r.status}"
        parts.append(f"### reqId={r.req_id} op={r.op} {outcome}")
        parts.append(_render_body(r.body))
    return "\n\n".join(parts)
