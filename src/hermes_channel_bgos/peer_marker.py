"""Pure functions for the `[[BGOS_PEER]]` marker round trip.

Hermes agents reply with text, so peer operations need an agent-facing marker
that the BGOS adapter can remove, execute, and answer inside the same session.
This module owns both pure halves of that protocol:

REQUEST half. The agent emits one JSON object per block:

    [[BGOS_PEER]]{"op":"send_to_peer","target":"Hades","text":"Check this","reqId":"p1"}[[/BGOS_PEER]]

`parse_peer_blocks` removes every real block from visible text and returns a
`PeerRequest` or `PeerParseError` for each one. Invalid JSON is always an error,
never a silent drop, because the agent is waiting for a result. Code-fenced
examples remain visible and do not execute. `plan_request` names an existing
`BgosApi` helper and carries only marker-derived arguments. The adapter resolves
an exact peer name through `list_peers`, then supplies caller identity and the
visible parent message id required by `send_peer`.

RESULT half. `build_result_turn` creates one synthetic system turn with a fixed
provenance header and one reqId section per result, in the order supplied.
Backend bodies, including denials and typed errors, are rendered without any
channel-authored paraphrase.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any


PEER_BLOCK_RE = re.compile(
    r"\[\[BGOS_PEER\]\](.*?)\[\[/BGOS_PEER\]\]",
    re.IGNORECASE | re.DOTALL,
)

_PEER_OPEN_RE = re.compile(r"\[\[BGOS_PEER\]\]", re.IGNORECASE)
_PEER_CLOSE_RE = re.compile(r"\[\[/BGOS_PEER\]\]", re.IGNORECASE)

_CODE_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)

PEER_RESULT_HEADER = "[BGOS peer result]"

PEER_RESULT_PREAMBLE = (
    "This is a system message from the BGOS adapter, not the user. It answers "
    "the [[BGOS_PEER]] requests in your previous reply. Do not thank the "
    "user for it. Continue the task; anything you want the user to see must "
    "be a normal reply."
)

PEER_MAX_BLOCKS = 5

PEER_LOOP_GUARD_LIMIT = 6

PEER_OPS = frozenset(
    {
        "list_peers",
        "peer_status",
        "send_to_peer",
        "complete_peer_thread",
    }
)


@dataclass
class PeerRequest:
    """One validated peer operation emitted by the agent."""

    req_id: str
    op: str
    args: dict


@dataclass
class PeerParseError:
    """One stripped marker block that could not become a request."""

    req_id: str
    raw: str
    message: str


@dataclass
class PeerApiPlan:
    """A call to an existing `BgosApi` helper.

    `kwargs` contains only arguments derived from the marker. The adapter adds
    `caller_assistant_id` to every call and `parent_message_id` to `send_peer`.
    For targeted calls it resolves `target` to an assistant id and inserts it
    under `target_kwarg`. No REST path is represented or derived here.
    """

    helper: str
    kwargs: dict
    target: int | str | None = None
    target_kwarg: str | None = None


@dataclass
class PeerCallResult:
    """One executed or refused peer call ready for result rendering."""

    req_id: str
    op: str
    ok: bool
    status: int
    body: Any


def _mint_req_id() -> str:
    return f"p-{uuid.uuid4().hex[:8]}"


def _grammar_reminder() -> str:
    return (
        "Expected one JSON object per block: "
        '[[BGOS_PEER]]{"op":"send_to_peer","target":"<positive assistant '
        'id or exact peer name>","text":"<message>","reqId":"p1"}'
        "[[/BGOS_PEER]] with op one of: "
        + ", ".join(sorted(PEER_OPS))
        + "."
    )


def _normalise_target(value: Any) -> int | str | None:
    """Return a positive id or trimmed exact name, otherwise None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    target = value.strip()
    if not target:
        return None
    if target.isdecimal():
        target_id = int(target)
        return target_id if target_id > 0 else None
    return target


def _parse_one_block(raw: str) -> PeerRequest | PeerParseError:
    """Parse one marker body into a request or an attributable error."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return PeerParseError(
            req_id=_mint_req_id(),
            raw=raw,
            message="invalid JSON. " + _grammar_reminder(),
        )
    if not isinstance(payload, dict):
        return PeerParseError(
            req_id=_mint_req_id(),
            raw=raw,
            message="payload must be a JSON object. " + _grammar_reminder(),
        )

    raw_req_id = payload.get("reqId")
    req_id = str(raw_req_id) if raw_req_id is not None else _mint_req_id()
    op = payload.get("op")
    if not isinstance(op, str) or op not in PEER_OPS:
        return PeerParseError(
            req_id=req_id,
            raw=raw,
            message=f"unknown op {op!r}. " + _grammar_reminder(),
        )

    args = {key: value for key, value in payload.items() if key not in ("op", "reqId")}
    if op != "list_peers":
        target = _normalise_target(payload.get("target"))
        if target is None:
            return PeerParseError(
                req_id=req_id,
                raw=raw,
                message=(
                    f"op {op!r} requires target as a positive assistant id "
                    "or exact peer name."
                ),
            )
        args["target"] = target

    if op == "send_to_peer":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return PeerParseError(
                req_id=req_id,
                raw=raw,
                message="op 'send_to_peer' requires non-empty string field text.",
            )
        wait_for_reply = payload.get("waitForReply", False)
        if not isinstance(wait_for_reply, bool):
            return PeerParseError(
                req_id=req_id,
                raw=raw,
                message="waitForReply must be a boolean.",
            )
        args["waitForReply"] = wait_for_reply
        if "timeoutSeconds" in payload:
            timeout_seconds = payload["timeoutSeconds"]
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= 50
            ):
                return PeerParseError(
                    req_id=req_id,
                    raw=raw,
                    message="timeoutSeconds must be an integer from 1 to 50.",
                )

    if op == "complete_peer_thread" and "summary" in payload:
        if not isinstance(payload["summary"], str):
            return PeerParseError(
                req_id=req_id,
                raw=raw,
                message="summary must be a string when provided.",
            )

    return PeerRequest(req_id=req_id, op=op, args=args)


def parse_peer_blocks(
    content: str,
) -> tuple[str, list[PeerRequest], list[PeerParseError]]:
    """Extract peer blocks and strip them from user-visible agent text.

    Returns `(cleaned_text, requests, errors)`. Blocks inside code fences stay
    visible as documentation. Valid blocks beyond `PEER_MAX_BLOCKS` become
    errors rather than calls, and are still stripped.
    """
    if not content or "BGOS_PEER" not in content.upper():
        return content, [], []
    segments = _CODE_FENCE_RE.split(content)
    requests: list[PeerRequest] = []
    errors: list[PeerParseError] = []
    matched_any = False
    for idx in range(0, len(segments), 2):
        segment = segments[idx]
        if "BGOS_PEER" not in segment.upper():
            continue
        for match in PEER_BLOCK_RE.finditer(segment):
            matched_any = True
            raw = (match.group(1) or "").strip()
            parsed = _parse_one_block(raw)
            if isinstance(parsed, PeerRequest):
                if len(requests) < PEER_MAX_BLOCKS:
                    requests.append(parsed)
                else:
                    errors.append(
                        PeerParseError(
                            req_id=parsed.req_id,
                            raw=raw,
                            message=(
                                f"more than {PEER_MAX_BLOCKS} peer calls in "
                                "one reply; this one was not executed. Batch "
                                "fewer calls per turn."
                            ),
                        )
                    )
            else:
                errors.append(parsed)
        remainder = PEER_BLOCK_RE.sub("", segment)
        bare_open = _PEER_OPEN_RE.search(remainder)
        if bare_open is not None:
            matched_any = True
            errors.append(
                PeerParseError(
                    req_id=_mint_req_id(),
                    raw=remainder[bare_open.end():].strip(),
                    message=(
                        "missing closing [[/BGOS_PEER]] marker. "
                        + _grammar_reminder()
                    ),
                )
            )
            # Without a closing delimiter there is no safe way to identify
            # where the private request ends. Keep only text before the open
            # marker so neither syntax nor payload can reach the user.
            remainder = remainder[:bare_open.start()]
        orphan_closes = list(_PEER_CLOSE_RE.finditer(remainder))
        if orphan_closes:
            matched_any = True
            errors.extend(
                PeerParseError(
                    req_id=_mint_req_id(),
                    raw=match.group(0),
                    message=(
                        "closing [[/BGOS_PEER]] marker has no opening marker. "
                        + _grammar_reminder()
                    ),
                )
                for match in orphan_closes
            )
            remainder = _PEER_CLOSE_RE.sub("", remainder)
        segments[idx] = remainder
    if not matched_any:
        return content, [], []
    cleaned = "".join(segments)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, requests, errors


def plan_request(req: PeerRequest) -> PeerApiPlan:
    """Map a validated marker request to an existing `BgosApi` helper."""
    args = req.args
    target = _normalise_target(args.get("target"))
    if req.op == "list_peers":
        return PeerApiPlan(helper="list_peers", kwargs={})
    if req.op == "peer_status":
        return PeerApiPlan(
            helper="peer_status",
            kwargs={},
            target=target,
            target_kwarg="peer_assistant_id",
        )
    if req.op == "send_to_peer":
        kwargs: dict[str, Any] = {
            "text": args["text"],
            "wait_for_reply": bool(args.get("waitForReply", False)),
        }
        if args.get("timeoutSeconds") is not None:
            kwargs["timeout_seconds"] = args["timeoutSeconds"]
        return PeerApiPlan(
            helper="send_peer",
            kwargs=kwargs,
            target=target,
            target_kwarg="target_assistant_id",
        )
    if req.op == "complete_peer_thread":
        kwargs = {}
        if args.get("summary") is not None:
            kwargs["summary"] = args["summary"]
        return PeerApiPlan(
            helper="close_peer_conversation",
            kwargs=kwargs,
            target=target,
            target_kwarg="peer_assistant_id",
        )
    raise ValueError(f"unsupported peer op {req.op!r}")


def _render_body(body: Any) -> str:
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body)
    except (TypeError, ValueError):
        return repr(body)


def build_result_turn(results: list[PeerCallResult]) -> str:
    """Render one provenance-bearing system turn in request order."""
    parts = [PEER_RESULT_HEADER, PEER_RESULT_PREAMBLE]
    for result in results:
        outcome = "ok" if result.ok else f"error status={result.status}"
        parts.append(
            f"### reqId={result.req_id} op={result.op} {outcome}"
        )
        parts.append(_render_body(result.body))
    return "\n\n".join(parts)
