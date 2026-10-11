"""Pure parsers for the app control and Work Stream markers (board row
019fc652-952b, "Build it for Hermes", 2026-10-11).

Hermes agents have no tool surface, so the two capabilities the Claude Code
channel reaches through backend routes are expressed as reply markers the
adapter turns into the SAME wire calls, authenticated as this pairing
(X-BGOS-Pairing). Both are fire and forget: nothing comes back to the agent,
so unlike [[BGOS_BOARDS]] there is no result turn, and a malformed block is
stripped, logged and not executed.

APP CONTROL. One whitelisted in call UI action per block:

    [[BGOS_UI]]show_work_stream[[/BGOS_UI]]
    [[BGOS_UI]]expand_card | invoice[[/BGOS_UI]]

becomes POST /api/v1/integrations/voice-sessions/:sessionId/ui-action
{action, target?, agentName?, actionId, ts}. The whitelist mirrors the
backend's VOICE_UI_ACTION_KINDS (backend/src/dto/voice/voice-session.dto.ts);
the backend enforces it again (DTO @IsIn) and the app a third time. The
session id is not the agent's to name: the adapter learns it from the
`voiceSessionId` the backend puts on every consult and dispatch frame for
that chat, so the agent can only steer the call it is actually on.

WORK STREAM. The explicit result for a voice task, the marker twin of the
Claude Code `complete_voice_task` tool:

    [[BGOS_TASK]]{"taskId":"123","result":"Booked for 9am.","failed":false}[[/BGOS_TASK]]

becomes POST /api/v1/integrations/voice-tasks/:taskId/result with
{ok:true, payload:{text}} or {ok:false, error:{code:"DISPATCH_FAILED",
message}}. Hermes already reports a dispatch run's reply automatically; the
marker is how an agent says a task FAILED, or keeps its Work Stream result
separate from its spoken summary. The backend keeps the first result for a
task and ignores the rest, and the adapter skips its automatic report for a
task the marker already settled.

Blocks inside ``` or ~~~ fences, or inside an inline `code span`, are
documentation and stay put.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Same delimiter family and regex discipline as the other agent markers.
UI_BLOCK_RE = re.compile(
    r"\[\[BGOS_UI\]\](.*?)\[\[/BGOS_UI\]\]",
    re.IGNORECASE | re.DOTALL,
)
TASK_BLOCK_RE = re.compile(
    r"\[\[BGOS_TASK\]\](.*?)\[\[/BGOS_TASK\]\]",
    re.IGNORECASE | re.DOTALL,
)

# Fenced blocks AND inline code spans: these markers have side effects, so an
# agent explaining the syntax in `backticks` must not fire a real action.
_CODE_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.DOTALL)

# Mirror of the backend VOICE_UI_ACTION_KINDS whitelist, in its order.
UI_ACTION_KINDS = (
    "show_work_stream",
    "hide_work_stream",
    "open_transcript",
    "close_transcript",
    "expand_card",
    "show_card_steps",
    "open_chat",
    "close_chat",
)
_UI_ACTION_SET = frozenset(UI_ACTION_KINDS)

# Backend VoiceUiActionDto caps target at 300 chars.
UI_TARGET_MAX = 300

# A reply that flips the screen more than this many times is a runaway
# generation, not a request; the excess blocks are stripped, not executed.
UI_MAX_BLOCKS = 3

# voice_tasks.id is a pg bigint: anything else would 500 inside the backend
# instead of being refused, so it never leaves the adapter.
_TASK_ID_RE = re.compile(r"^\d{1,19}$")

# Generous cap on the Work Stream result text; the card shows a summary.
TASK_RESULT_MAX = 4000

_TASK_FIELDS = frozenset({"taskId", "result", "failed"})


@dataclass(frozen=True)
class UiActionRequest:
    action: str
    target: str | None = None


@dataclass(frozen=True)
class TaskResultRequest:
    task_id: str
    result: str
    failed: bool = False

    def wire_body(self) -> dict:
        """The exact body complete_voice_task POSTs for the same arguments."""
        if self.failed:
            return {
                "ok": False,
                "error": {"code": "DISPATCH_FAILED", "message": self.result},
            }
        return {"ok": True, "payload": {"text": self.result}}


def _strip_blocks(
    content: str, block_re: re.Pattern[str], tag: str,
) -> tuple[str, list[str]]:
    """Remove every unfenced block matching `block_re`, returning the cleaned
    text and the raw bodies in order. `(content, [])` when none matched."""
    if not content or tag not in content.upper():
        return content, []
    segments = _CODE_FENCE_RE.split(content)
    bodies: list[str] = []
    for idx in range(0, len(segments), 2):
        seg = segments[idx]
        if tag not in seg.upper():
            continue
        found = [m.group(1) or "" for m in block_re.finditer(seg)]
        if found:
            bodies.extend(found)
            segments[idx] = block_re.sub("", seg)
    if not bodies:
        return content, []
    cleaned = "".join(segments)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, bodies


def _parse_ui_body(raw: str) -> UiActionRequest | str:
    body = raw.strip()
    if not body:
        return "empty [[BGOS_UI]] block"
    action, sep, target = body.partition("|")
    action = action.strip().lower()
    if action not in _UI_ACTION_SET:
        return f"unknown ui action {action[:40]!r}"
    target = target.strip() if sep else ""
    if "\n" in target:
        return "ui target must be one line"
    if len(target) > UI_TARGET_MAX:
        target = target[:UI_TARGET_MAX].rstrip()
    return UiActionRequest(action=action, target=target or None)


def parse_ui_blocks(content: str) -> tuple[str, list[UiActionRequest], list[str]]:
    """Extract every `[[BGOS_UI]]action[ | target][[/BGOS_UI]]` block.

    Returns `(cleaned_text, requests, errors)`. Every block, valid or not, is
    stripped from the visible text. Invalid blocks and blocks past
    UI_MAX_BLOCKS become error strings (for the log), never requests.
    """
    cleaned, bodies = _strip_blocks(content, UI_BLOCK_RE, "BGOS_UI")
    requests: list[UiActionRequest] = []
    errors: list[str] = []
    for raw in bodies:
        parsed = _parse_ui_body(raw)
        if isinstance(parsed, str):
            errors.append(parsed)
        elif len(requests) >= UI_MAX_BLOCKS:
            errors.append(
                f"more than {UI_MAX_BLOCKS} ui actions in one reply; "
                f"{parsed.action} not executed"
            )
        else:
            requests.append(parsed)
    return cleaned, requests, errors


def _parse_task_body(raw: str) -> TaskResultRequest | str:
    body = raw.strip()
    try:
        data = json.loads(body)
    except ValueError:
        return "[[BGOS_TASK]] body is not valid JSON"
    if not isinstance(data, dict):
        return "[[BGOS_TASK]] body must be a JSON object"
    unknown = set(data) - _TASK_FIELDS
    if unknown:
        return f"[[BGOS_TASK]] has unsupported fields {sorted(unknown)}"
    task_id = data.get("taskId")
    if isinstance(task_id, int) and not isinstance(task_id, bool):
        task_id = str(task_id)
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id.strip()):
        return "[[BGOS_TASK]] taskId must be the numeric voice task id"
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        return "[[BGOS_TASK]] result must be a non-empty string"
    failed = data.get("failed", False)
    if not isinstance(failed, bool):
        return "[[BGOS_TASK]] failed must be true or false"
    result = result.strip()
    if len(result) > TASK_RESULT_MAX:
        result = result[:TASK_RESULT_MAX].rstrip()
    return TaskResultRequest(task_id=task_id.strip(), result=result, failed=failed)


def parse_task_block(content: str) -> tuple[str, TaskResultRequest | None, list[str]]:
    """Extract the `[[BGOS_TASK]]{...}[[/BGOS_TASK]]` block.

    Returns `(cleaned_text, request_or_None, errors)`. Every block is
    stripped; only the FIRST block is honored, because one dispatch turn
    settles one task and a second result would be ignored by the backend
    anyway. A malformed first block is an error, not a fallback to the next:
    the agent's intent was that block.
    """
    cleaned, bodies = _strip_blocks(content, TASK_BLOCK_RE, "BGOS_TASK")
    if not bodies:
        return content, None, []
    errors: list[str] = []
    parsed = _parse_task_body(bodies[0])
    request: TaskResultRequest | None = None
    if isinstance(parsed, str):
        errors.append(parsed)
    else:
        request = parsed
    if len(bodies) > 1:
        errors.append(
            f"{len(bodies) - 1} extra [[BGOS_TASK]] block(s) ignored; one task "
            "result per reply"
        )
    return cleaned, request, errors


# A block still being streamed: an open tag at the end with no close tag after
# it, in the last segment outside code.
_OPEN_TAG_RE = re.compile(r"\[\[BGOS_(?:UI|TASK)\]\]", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"\[\[/BGOS_(?:UI|TASK)\]\]", re.IGNORECASE)


def strip_app_control_markers(content: str, *, hide_open_tail: bool = False) -> str:
    """Remove both marker kinds without executing them.

    Complete blocks outside code are always removed. `hide_open_tail` also
    hides a block whose close tag has not streamed in yet; only an interim
    streaming edit sets it, because on final text an unclosed tag is prose
    (e.g. "use the [[BGOS_UI]] marker") and must stay visible.
    """
    if not content or "BGOS_" not in content.upper():
        return content
    content, _ = _strip_blocks(content, UI_BLOCK_RE, "BGOS_UI")
    content, _ = _strip_blocks(content, TASK_BLOCK_RE, "BGOS_TASK")
    if not hide_open_tail:
        return content
    segments = _CODE_FENCE_RE.split(content)
    if len(segments) % 2 == 0:
        return content
    last = segments[-1]
    opens = list(_OPEN_TAG_RE.finditer(last))
    if not opens:
        return content
    tail_start = opens[-1].start()
    if _CLOSE_TAG_RE.search(last, tail_start):
        return content
    segments[-1] = last[:tail_start]
    return "".join(segments).rstrip()
