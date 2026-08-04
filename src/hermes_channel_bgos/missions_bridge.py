"""Bridge Hermes goal-loop notices to BGOS mission cards."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .bgos_api import BgosApiError
from .hermes_profiles import resolve_hermes_home

log = logging.getLogger(__name__)

GoalLineKind = Literal[
    "set",
    "continue",
    "done",
    "cleared",
    "paused",
    "resumed",
    "parked",
]
GoalLineProvenance = Literal["direct", "post_turn"]
MissionId = int | str

_SET_RE = re.compile(
    r"^⊙ Goal set(?:\s+\((?P<budget>\d+)-turn budget\))?"
    r"(?:\s*:\s*(?P<goal>.*)|\s+(?P<goal_plain>.+)|\s*)$"
)
_CONTINUE_RE = re.compile(
    r"^↻ Continuing toward goal(?:\s+\((?P<used>\d+)/(?P<budget>\d+)\))?"
    r"(?:\s*:\s*(?P<reason>.*)|\s+(?P<reason_plain>.+)|\s*)$"
)
_CLEARED_RE = re.compile(r"^✓ Goal cleared\.?$")
_DONE_RE = re.compile(r"^✓ Goal achieved(?:\s*:\s*(?P<reason>.*))?$")
_RESUMED_RE = re.compile(
    r"^▶ Goal resumed:\s*(?P<goal>[^\n]+)"
    r"(?:\nSend any message to continue, or wait \u2014 I'll take the next "
    r"step on the next turn\.)?$"
)
_PAUSED_PREFIX = "⏸ Goal paused"
_PARKED_PREFIX = "⏳ Goal parked"
_TURN_COUNT_RE = re.compile(r"(?P<used>\d+)\s*/\s*(?P<budget>\d+)")
_LEADING_SEPARATOR_RE = re.compile(r"^(?:\(judge\)\s*)?(?::|[\u2013\u2014-])?\s*")
_SET_INSTRUCTIONS = (
    "I'll keep working until the goal is done, you pause/clear it, or the "
    "budget is exhausted.\n"
    "Controls: /goal status \u00b7 /goal pause \u00b7 /goal resume \u00b7 "
    "/goal clear"
)
_DRAFT_FAILURE = (
    "(Couldn't draft a contract \u2014 running as a free-form goal.)"
)
_PARSE_FAILURE_PAUSE_RE = re.compile(
    r"^\u23f8 Goal paused \u2014 the judge model \((?P<count>\d+) turns\) "
    r"isn't returning the required JSON verdict\. Route the judge to a "
    r"stricter model in ~/\.hermes/config\.yaml:\n"
    r"  auxiliary:\n"
    r"    goal_judge:\n"
    r"      provider: openrouter\n"
    r"      model: google/gemini-3-flash-preview\n"
    r"Then /goal resume to continue\.$"
)
_CONTRACT_LINE_RE = re.compile(
    r"^- (?P<label>Outcome|Verification|Constraints|Boundaries|"
    r"Stop when blocked): (?P<value>\S(?:.*\S)?)$"
)
_CONTRACT_LABEL_ORDER = {
    "Outcome": 0,
    "Verification": 1,
    "Constraints": 2,
    "Boundaries": 3,
    "Stop when blocked": 4,
}
_BUDGET_PAUSE_RE = re.compile(
    r"^\u23f8 Goal paused \u2014 \d+/\d+ turns used\. Use /goal resume "
    r"to keep going, or /goal clear to stop\.$"
)
_DIRECT_PARK_RE = re.compile(
    r"^\u23f3 Goal parked on pid \d+(?: \([^\n]+\))?\. "
    r"Loop pauses until it exits\.$"
)
_POST_TURN_PARK_RE = re.compile(
    r"^\u23f3 Goal parked(?: \(judge\))? \u2014 waiting on "
    r"(?:pid \d+|session [^:\n]+|\d+s(?: remaining)?): \S[^\n]*$"
)


@dataclass(frozen=True)
class GoalLine:
    kind: GoalLineKind
    goal_text: str | None = None
    reason: str | None = None
    turns_used: int | None = None
    max_turns: int | None = None
    provenance: GoalLineProvenance | None = None


def _number(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _optional_text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _status_tail(line: str, prefix: str) -> str | None:
    tail = line[len(prefix):].strip()
    tail = _LEADING_SEPARATOR_RE.sub("", tail, count=1).strip()
    return tail or None


def _valid_contract_tail(tail: str) -> bool:
    prefix = "Completion contract:\n"
    if not tail.startswith(prefix):
        return False
    lines = tail[len(prefix):].splitlines()
    if not lines:
        return False
    previous = -1
    for line in lines:
        match = _CONTRACT_LINE_RE.fullmatch(line)
        if match is None:
            return False
        position = _CONTRACT_LABEL_ORDER[match.group("label")]
        if position <= previous:
            return False
        previous = position
    return True


def classify_goal_line(text: str | None) -> GoalLine | None:
    """Classify one complete upstream Hermes goal notice."""
    if not isinstance(text, str) or not text.strip():
        return None
    notice = text.strip()
    lines = notice.splitlines()
    first_line = lines[0].strip()

    match = _SET_RE.fullmatch(first_line)
    if match is not None:
        tail = "\n".join(lines[1:])
        has_instructions = bool(
            tail == _SET_INSTRUCTIONS
            or tail.startswith(f"{_SET_INSTRUCTIONS}\n")
        )
        if has_instructions:
            tail = tail[len(_SET_INSTRUCTIONS):].removeprefix("\n")
        valid_tail = bool(
            not tail
            or (
                has_instructions
                and (tail == _DRAFT_FAILURE or _valid_contract_tail(tail))
            )
        )
        if not valid_tail:
            return None
        goal_text = _optional_text(
            match.group("goal") or match.group("goal_plain")
        )
        budget = _number(match.group("budget"))
        return GoalLine(
            kind="set",
            goal_text=goal_text,
            max_turns=budget,
            provenance=(
                "direct"
                if has_instructions and goal_text is not None and budget is not None
                else None
            ),
        )

    match = _CONTINUE_RE.fullmatch(notice)
    if match is not None:
        return GoalLine(
            kind="continue",
            reason=_optional_text(
                match.group("reason") or match.group("reason_plain")
            ),
            turns_used=_number(match.group("used")),
            max_turns=_number(match.group("budget")),
            provenance=(
                "post_turn"
                if (
                    match.group("used") is not None
                    and match.group("budget") is not None
                    and match.group("reason") is not None
                )
                else None
            ),
        )

    match = _CLEARED_RE.fullmatch(notice)
    if match is not None:
        return GoalLine(
            kind="cleared",
            provenance="direct" if notice == "\u2713 Goal cleared." else None,
        )

    match = _DONE_RE.fullmatch(notice)
    if match is not None:
        return GoalLine(
            kind="done",
            reason=_optional_text(match.group("reason")),
            provenance=(
                "post_turn" if match.group("reason") is not None else None
            ),
        )

    match = _RESUMED_RE.fullmatch(notice)
    if match is not None:
        return GoalLine(
            kind="resumed",
            goal_text=_optional_text(match.group("goal")),
            provenance="direct" if "\n" in notice else None,
        )

    match = _PARSE_FAILURE_PAUSE_RE.fullmatch(notice)
    if match is not None:
        return GoalLine(
            kind="paused",
            reason=(
                "judge model returned unparseable output "
                f"{match.group('count')} turns in a row"
            ),
            provenance="post_turn",
        )

    paused_shape = bool(
        notice == _PAUSED_PREFIX
        or notice.startswith(f"{_PAUSED_PREFIX}:")
        or notice.startswith(f"{_PAUSED_PREFIX} \u2014 ")
    )
    if paused_shape and "\n" not in notice:
        counts = _TURN_COUNT_RE.search(notice)
        return GoalLine(
            kind="paused",
            reason=_status_tail(notice, _PAUSED_PREFIX),
            turns_used=_number(counts.group("used")) if counts else None,
            max_turns=_number(counts.group("budget")) if counts else None,
            provenance=(
                "direct"
                if re.fullmatch(r"\u23f8 Goal paused: \S[^\n]*", notice)
                else "post_turn"
                if _BUDGET_PAUSE_RE.fullmatch(notice)
                else None
            ),
        )

    parked_shape = bool(
        notice == _PARKED_PREFIX
        or notice.startswith(f"{_PARKED_PREFIX}:")
        or notice.startswith(f"{_PARKED_PREFIX} \u2014 ")
        or notice.startswith(f"{_PARKED_PREFIX} (judge) \u2014 ")
        or (
            notice.startswith(f"{_PARKED_PREFIX} on pid ")
            and notice.endswith(". Loop pauses until it exits.")
        )
    )
    if parked_shape and "\n" not in notice:
        counts = _TURN_COUNT_RE.search(notice)
        return GoalLine(
            kind="parked",
            reason=_status_tail(notice, _PARKED_PREFIX),
            turns_used=_number(counts.group("used")) if counts else None,
            max_turns=_number(counts.group("budget")) if counts else None,
            provenance=(
                "direct"
                if _DIRECT_PARK_RE.fullmatch(notice)
                else "post_turn"
                if _POST_TURN_PARK_RE.fullmatch(notice)
                else None
            ),
        )

    return None


@dataclass
class _MissionBinding:
    mission_id: MissionId
    assistant_id: int
    chat_id: int
    status: str
    origin: str | None = None
    revision: int = 0
    session_id: str | None = None
    goal_text: str | None = None
    goal_created_at: float | None = None
    updated_at: str | None = None
    self_patch_updated_at: str | None = None
    paused_by_lane: bool = False
    pending_enforcement: bool = False
    pending_abandon: bool = False


@dataclass(frozen=True)
class _PendingCreateIntent:
    assistant_id: int
    chat_id: int
    session_id: str | None
    goal_text: str | None
    goal_created_at: float | None
    title: str
    created_at: float


@dataclass(eq=False)
class _PatchToken:
    done: asyncio.Event
    settled: asyncio.Event
    snapshot_acks: set[asyncio.Event]


@dataclass(frozen=True)
class _ProgressUpdate:
    assistant_id: int
    mission_id: MissionId
    effort: dict[str, Any] | None
    feed_entry: dict[str, str]


def _load_goal_manager() -> type:
    """Import Hermes only when an app control event needs it."""
    from hermes_cli.goals import GoalManager  # type: ignore

    return GoalManager


class MissionLane:
    """Per chat and assistant mission state derived from goal notices."""

    _OPEN_STATUSES = {"active", "paused"}
    _CONTROL_EVENTS = {
        "mission_paused",
        "mission_resumed",
        "mission_completed",
    }
    _STATE_TRANSITION_EVENTS = _CONTROL_EVENTS | {
        "mission_abandoned",
        "mission_failed",
    }
    _PENDING_CREATE_SECONDS = 120.0
    _PAUSE_REASON = "bgos-mission-paused"
    _STORE_FILENAME = "bgos_mission_bindings.json"

    def __init__(
        self,
        api: Any,
        *,
        assistant_id_resolver: Callable[[int], Any] | None = None,
        session_id_resolver: Callable[[int, int], Any] | None = None,
        goal_manager_factory: Callable[..., Any] | None = None,
        progress_throttle_seconds: float = 3.0,
        binding_store_path: Path | None = None,
    ) -> None:
        self._api = api
        self._assistant_id_resolver = assistant_id_resolver
        self._session_id_resolver = session_id_resolver
        self._goal_manager_factory = goal_manager_factory
        self._progress_throttle_seconds = float(progress_throttle_seconds)
        self._binding_store_path = binding_store_path
        self._bindings: dict[tuple[int, int], _MissionBinding] = {}
        self._binding_key_by_mission: dict[str, tuple[int, int]] = {}
        self._pending_creates: list[_PendingCreateIntent] = []
        self._pending_progress: dict[str, _ProgressUpdate] = {}
        self._pending_progress_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_progress_at: dict[str, float] = {}
        self._progress_locks: dict[str, asyncio.Lock] = {}
        self._goal_line_locks: dict[int, asyncio.Lock] = {}
        self._mission_event_locks: dict[str, asyncio.Lock] = {}
        self._inflight_patches: dict[str, set[_PatchToken]] = {}
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None
        self._reconcile_requested = False
        self._reconcile_owner_task: asyncio.Task[Any] | None = None
        self._closed = False
        self._load_bindings()

    @staticmethod
    async def _resolve(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _mission_key(mission_id: MissionId) -> str:
        return str(mission_id)

    def _is_current(self, binding: _MissionBinding) -> bool:
        key = (binding.chat_id, binding.assistant_id)
        mission_key = self._mission_key(binding.mission_id)
        return (
            self._bindings.get(key) is binding
            and self._binding_key_by_mission.get(mission_key) == key
        )

    def _store_path(self) -> Path:
        if self._binding_store_path is not None:
            return self._binding_store_path
        return resolve_hermes_home().expanduser() / self._STORE_FILENAME

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _revision_value(cls, value: Any) -> float | None:
        numeric = cls._optional_float(value)
        if numeric is not None:
            return numeric
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None

    @classmethod
    def _revision_compare(cls, left: Any, right: Any) -> int | None:
        left_value = cls._revision_value(left)
        right_value = cls._revision_value(right)
        if left_value is None or right_value is None:
            return None
        return (left_value > right_value) - (left_value < right_value)

    @classmethod
    def _latest_revision(cls, *values: Any) -> str | None:
        latest: str | None = None
        for value in values:
            if value is None:
                continue
            text = str(value)
            comparison = cls._revision_compare(text, latest)
            if latest is None or comparison is None or comparison > 0:
                latest = text
        return latest

    @staticmethod
    def _snapshot_updated_at(snapshot: dict[str, Any] | None) -> str | None:
        if not isinstance(snapshot, dict):
            return None
        value = snapshot.get("updatedAt", snapshot.get("updated_at"))
        return str(value) if value is not None and str(value) else None

    def _binding_record(self, binding: _MissionBinding) -> dict[str, Any]:
        return {
            "mission_id": binding.mission_id,
            "assistant_id": binding.assistant_id,
            "chat_id": binding.chat_id,
            "status": binding.status,
            "origin": binding.origin,
            "session_id": binding.session_id,
            "goal_text": binding.goal_text,
            "goal_created_at": binding.goal_created_at,
            "updated_at": binding.updated_at,
            "self_patch_updated_at": binding.self_patch_updated_at,
            "paused_by_lane": binding.paused_by_lane,
            "pending_enforcement": binding.pending_enforcement,
            "pending_abandon": binding.pending_abandon,
        }

    def _intent_record(self, intent: _PendingCreateIntent) -> dict[str, Any]:
        return {
            "assistant_id": intent.assistant_id,
            "chat_id": intent.chat_id,
            "session_id": intent.session_id,
            "goal_text": intent.goal_text,
            "goal_created_at": intent.goal_created_at,
            "title": intent.title,
            "created_at": intent.created_at,
        }

    def _persist_bindings(self) -> None:
        path = self._store_path()
        payload = {
            "bindings": [
                self._binding_record(binding)
                for binding in self._bindings.values()
            ],
            "pending_creates": [
                self._intent_record(intent)
                for intent in self._pending_creates
                if time.time() - intent.created_at <= self._PENDING_CREATE_SECONDS
            ],
        }
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(
                    payload,
                    temporary_file,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
        except (OSError, TypeError, ValueError):
            log.warning("could not persist BGOS mission bindings", exc_info=True)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load_bindings(self) -> None:
        try:
            raw = json.loads(self._store_path().read_text(encoding="utf-8"))
        except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
            return
        if not isinstance(raw, dict):
            return
        rows = raw.get("bindings")
        if isinstance(rows, list):
            for row in rows:
                binding = self._binding_from_record(row)
                if binding is None:
                    continue
                key = (binding.chat_id, binding.assistant_id)
                mission_key = self._mission_key(binding.mission_id)
                self._bindings[key] = binding
                self._binding_key_by_mission[mission_key] = key
        intents = raw.get("pending_creates")
        if isinstance(intents, list):
            now = time.time()
            for row in intents:
                intent = self._intent_from_record(row)
                if (
                    intent is not None
                    and now - intent.created_at <= self._PENDING_CREATE_SECONDS
                ):
                    self._pending_creates.append(intent)

    def _binding_from_record(self, row: Any) -> _MissionBinding | None:
        if not isinstance(row, dict):
            return None
        mission_id = row.get("mission_id")
        assistant_id = self._coerce_int(row.get("assistant_id"))
        chat_id = self._coerce_int(row.get("chat_id"))
        if (
            not isinstance(mission_id, (int, str))
            or not str(mission_id)
            or assistant_id is None
            or chat_id is None
        ):
            return None
        return _MissionBinding(
            mission_id=mission_id,
            assistant_id=assistant_id,
            chat_id=chat_id,
            status=str(row.get("status") or "active"),
            origin=(str(row["origin"]) if row.get("origin") is not None else None),
            session_id=(
                str(row["session_id"])
                if row.get("session_id") is not None
                else None
            ),
            goal_text=(
                str(row["goal_text"])
                if row.get("goal_text") is not None
                else None
            ),
            goal_created_at=self._optional_float(row.get("goal_created_at")),
            updated_at=(
                str(row["updated_at"])
                if row.get("updated_at") is not None
                else None
            ),
            self_patch_updated_at=(
                str(row["self_patch_updated_at"])
                if row.get("self_patch_updated_at") is not None
                else None
            ),
            paused_by_lane=bool(row.get("paused_by_lane")),
            pending_enforcement=bool(row.get("pending_enforcement")),
            pending_abandon=bool(row.get("pending_abandon")),
        )

    def _intent_from_record(self, row: Any) -> _PendingCreateIntent | None:
        if not isinstance(row, dict):
            return None
        assistant_id = self._coerce_int(row.get("assistant_id"))
        chat_id = self._coerce_int(row.get("chat_id"))
        created_at = self._optional_float(row.get("created_at"))
        title = row.get("title")
        if (
            assistant_id is None
            or chat_id is None
            or created_at is None
            or not isinstance(title, str)
            or not title
        ):
            return None
        return _PendingCreateIntent(
            assistant_id=assistant_id,
            chat_id=chat_id,
            session_id=(
                str(row["session_id"])
                if row.get("session_id") is not None
                else None
            ),
            goal_text=(
                str(row["goal_text"])
                if row.get("goal_text") is not None
                else None
            ),
            goal_created_at=self._optional_float(row.get("goal_created_at")),
            title=title,
            created_at=created_at,
        )

    @staticmethod
    def _trim(text: str | None, limit: int) -> str | None:
        cleaned = (text or "").strip()
        return cleaned[:limit] or None

    @staticmethod
    def _snapshot(response: Any) -> dict[str, Any] | None:
        if not isinstance(response, dict):
            return None
        mission = response.get("mission")
        if isinstance(mission, dict):
            return mission
        if response.get("id") is not None:
            return response
        return None

    @staticmethod
    def _snapshot_id(snapshot: dict[str, Any] | None) -> MissionId | None:
        if not isinstance(snapshot, dict):
            return None
        mission_id = snapshot.get("id")
        if isinstance(mission_id, (int, str)) and str(mission_id):
            return mission_id
        return None

    def _manager_factory(self) -> Callable[..., Any]:
        return self._goal_manager_factory or _load_goal_manager()

    @staticmethod
    def _manager_state(manager: Any) -> tuple[bool, Any]:
        try:
            state_declared = (
                "state" in vars(manager) or hasattr(type(manager), "state")
            )
        except TypeError:
            state_declared = hasattr(type(manager), "state")
        state = getattr(manager, "state", None) if state_declared else None
        return state_declared, state

    async def _new_goal_identity(
        self,
        *,
        chat_id: int,
        assistant_id: int,
        goal_text: str | None,
    ) -> tuple[str | None, str | None, float | None]:
        session_id: str | None = None
        if self._session_id_resolver is not None:
            try:
                resolved = await self._resolve(
                    self._session_id_resolver(chat_id, assistant_id)
                )
                if isinstance(resolved, str) and resolved:
                    session_id = resolved
            except Exception:
                log.warning(
                    "mission set session resolution failed chat=%s assistant=%s",
                    chat_id,
                    assistant_id,
                    exc_info=True,
                )
        if session_id is None:
            return None, goal_text, None
        try:
            manager = self._manager_factory()(session_id)
            state_declared, state = self._manager_state(manager)
        except Exception:
            log.warning(
                "mission set goal identity lookup failed session=%s",
                session_id,
                exc_info=True,
            )
            return session_id, goal_text, None
        if not state_declared or state is None:
            return session_id, goal_text, None
        if getattr(state, "status", None) not in self._OPEN_STATUSES:
            log.error(
                "mission set found no open goal identity session=%s",
                session_id,
            )
            return session_id, goal_text, None
        state_goal = getattr(state, "goal", None)
        state_created_at = self._optional_float(
            getattr(state, "created_at", None)
        )
        if goal_text is not None and state_goal != goal_text:
            log.error(
                "mission set goal identity mismatch session=%s",
                session_id,
            )
            return session_id, goal_text, None
        return (
            session_id,
            state_goal if isinstance(state_goal, str) else goal_text,
            state_created_at,
        )

    def _binding_goal_is_live(self, binding: _MissionBinding) -> bool:
        if not binding.session_id:
            return True
        try:
            manager = self._manager_factory()(binding.session_id)
            state_declared, state = self._manager_state(manager)
        except Exception:
            return True
        if not state_declared or state is None:
            return False
        if getattr(state, "status", None) not in {"active", "paused"}:
            return False
        state_goal = getattr(state, "goal", None)
        if binding.goal_text is not None and state_goal != binding.goal_text:
            return False
        state_created_at = self._optional_float(
            getattr(state, "created_at", None)
        )
        if (
            binding.goal_created_at is not None
            and state_created_at != binding.goal_created_at
        ):
            return False
        return True

    def _can_adopt_binding(
        self,
        owner: _MissionBinding | None,
        *,
        session_id: str | None,
        goal_text: str | None,
        goal_created_at: float | None,
    ) -> bool:
        if owner is None:
            return True
        if owner.pending_abandon:
            return False
        if owner.session_id is None or session_id is None:
            return False
        if owner.session_id == session_id:
            if (
                owner.goal_text is None
                or goal_text is None
                or owner.goal_text != goal_text
            ):
                return False
            if (
                owner.goal_created_at is None
                or goal_created_at is None
                or owner.goal_created_at != goal_created_at
            ):
                return False
            return True
        if self._binding_goal_is_live(owner):
            return False
        return True

    async def handle_goal_line(
        self,
        text: str,
        *,
        chat_id: int,
        assistant_id: int | None = None,
    ) -> bool:
        """Apply one classified goal notice without affecting chat delivery."""
        goal_line = classify_goal_line(text)
        if goal_line is None:
            return False

        chat_key = self._coerce_int(chat_id)
        if chat_key is None:
            return True
        assistant_key = self._coerce_int(assistant_id)
        if assistant_key is None and self._assistant_id_resolver is not None:
            try:
                resolved = await self._resolve(
                    self._assistant_id_resolver(chat_key)
                )
                assistant_key = self._coerce_int(resolved)
            except Exception:
                log.warning(
                    "mission assistant resolution failed chat=%s",
                    chat_key,
                    exc_info=True,
                )
        if assistant_key is None:
            log.debug("mission goal line ignored without assistant chat=%s", chat_key)
            return True

        try:
            lock = self._goal_line_locks.setdefault(
                assistant_key,
                asyncio.Lock(),
            )
            async with lock:
                if goal_line.kind == "set":
                    await self._handle_set(chat_key, assistant_key, goal_line)
                    return True

                binding = self._bindings.get((chat_key, assistant_key))
                if binding is None:
                    log.debug(
                        "mission goal line ignored without binding "
                        "chat=%s assistant=%s kind=%s",
                        chat_key,
                        assistant_key,
                        goal_line.kind,
                    )
                    return True

                if goal_line.kind == "continue":
                    await self._handle_continue(binding, goal_line)
                elif goal_line.kind == "done":
                    await self._complete(binding, goal_line.reason)
                elif goal_line.kind == "cleared":
                    await self._abandon(binding)
                elif goal_line.kind == "paused":
                    await self._pause(binding, goal_line.reason)
                elif goal_line.kind == "resumed":
                    await self._resume(binding)
                elif goal_line.kind == "parked":
                    waiting = "Waiting"
                    if goal_line.reason:
                        waiting = f"Waiting: {goal_line.reason}"
                    await self._pause(binding, waiting)
        except Exception:
            log.warning(
                "mission goal line failed chat=%s assistant=%s kind=%s",
                chat_key,
                assistant_key,
                goal_line.kind,
                exc_info=True,
            )
        return True

    async def _handle_set(
        self,
        chat_id: int,
        assistant_id: int,
        goal_line: GoalLine,
    ) -> None:
        await self._flush_assistant_progress(assistant_id)
        title = self._trim(goal_line.goal_text, 200)
        goal_identity_text = _optional_text(goal_line.goal_text)
        session_id, goal_text, goal_created_at = await self._new_goal_identity(
            chat_id=chat_id,
            assistant_id=assistant_id,
            goal_text=goal_identity_text,
        )
        mission_keys_before_get = {
            self._mission_key(binding.mission_id)
            for binding in self._bindings.values()
            if binding.assistant_id == assistant_id
        }
        try:
            response = await self._api.get_active_mission(
                assistant_id=assistant_id,
            )
        except Exception:
            log.warning(
                "mission active lookup failed assistant=%s",
                assistant_id,
                exc_info=True,
            )
            return

        active = self._snapshot(response)
        mission_id = self._snapshot_id(active)
        owner_key = (
            self._binding_key_by_mission.get(self._mission_key(mission_id))
            if mission_id is not None
            else None
        )
        owner = self._bindings.get(owner_key) if owner_key is not None else None
        owner_dropped_during_get = bool(
            mission_id is not None
            and self._mission_key(mission_id) in mission_keys_before_get
            and owner is None
        )
        can_adopt = bool(
            title is not None
            and mission_id is not None
            and not owner_dropped_during_get
            and active is not None
            and active.get("origin") == "derived"
            and active.get("status") in self._OPEN_STATUSES
            and active.get("title") == title
            and self._can_adopt_binding(
                owner,
                session_id=session_id,
                goal_text=goal_text,
                goal_created_at=goal_created_at,
            )
        )
        if can_adopt:
            candidate = self._build_binding(
                active,
                chat_id=chat_id,
                assistant_id=assistant_id,
                session_id=session_id,
                goal_text=goal_text,
                goal_created_at=goal_created_at,
            )
            candidate = self._merge_fresher_binding(candidate, owner)
        else:
            title = title or "Goal"
            kwargs: dict[str, Any] = {
                "assistant_id": assistant_id,
                "title": title,
                "origin": "derived",
                "first_feed_text": "Goal set",
            }
            if goal_line.max_turns is not None:
                kwargs["effort"] = {
                    "used": 0,
                    "budget": goal_line.max_turns,
                    "unit": "turns",
                }
            intent = _PendingCreateIntent(
                assistant_id=assistant_id,
                chat_id=chat_id,
                session_id=session_id,
                goal_text=goal_text,
                goal_created_at=goal_created_at,
                title=title,
                created_at=time.time(),
            )
            self._pending_creates.append(intent)
            self._persist_bindings()
            try:
                response = await self._api.create_mission(**kwargs)
            except Exception as exc:
                if (
                    isinstance(exc, BgosApiError)
                    and not self._create_error_is_ambiguous(exc)
                ):
                    self._remove_pending_create(intent)
                    log.warning(
                        "mission create failed assistant=%s",
                        assistant_id,
                        exc_info=True,
                    )
                    return
                event_binding = self._bindings.get((chat_id, assistant_id))
                if (
                    intent not in self._pending_creates
                    and self._binding_matches_intent(event_binding, intent)
                ):
                    return
                if intent in self._pending_creates:
                    intent = self._refresh_pending_create(intent)
                log.error(
                    "mission create outcome is ambiguous assistant=%s title=%r",
                    assistant_id,
                    title,
                    exc_info=True,
                )
                self.schedule_reconcile()
                return
            created_snapshot = self._snapshot(response)
            created_id = self._snapshot_id(created_snapshot)
            created_key = (
                self._binding_key_by_mission.get(
                    self._mission_key(created_id)
                )
                if created_id is not None
                else None
            )
            created_binding = (
                self._bindings.get(created_key)
                if created_key is not None
                else None
            )
            if intent not in self._pending_creates:
                if self._binding_matches_intent(created_binding, intent):
                    return
                log.error(
                    "mission create event bound a different snapshot "
                    "assistant=%s title=%r",
                    assistant_id,
                    title,
                )
                return
            candidate = self._build_binding(
                created_snapshot,
                chat_id=chat_id,
                assistant_id=assistant_id,
                session_id=session_id,
                goal_text=goal_text,
                goal_created_at=goal_created_at,
            )
            self._remove_pending_create(intent, persist=False)

        if candidate is None:
            self._persist_bindings()
            log.error(
                "mission set returned no usable snapshot assistant=%s",
                assistant_id,
            )
            return
        binding = self._swap_binding(candidate, replace_assistant=True)
        if binding.status == "paused":
            await self._enforce_binding(binding, expected_revision=binding.revision)

    async def _handle_continue(
        self,
        binding: _MissionBinding,
        goal_line: GoalLine,
    ) -> None:
        if binding.status not in self._OPEN_STATUSES:
            return
        if binding.status == "paused" and not await self._resume(binding):
            return

        effort: dict[str, Any] | None = None
        if (
            goal_line.turns_used is not None
            and goal_line.max_turns is not None
        ):
            effort = {
                "used": goal_line.turns_used,
                "budget": goal_line.max_turns,
                "unit": "turns",
            }
        reason = self._trim(goal_line.reason, 182)
        feed_text = "Checking my work"
        if reason:
            feed_text = f"Checking my work: {reason}"
        update = _ProgressUpdate(
            assistant_id=binding.assistant_id,
            mission_id=binding.mission_id,
            effort=effort,
            feed_entry={"kind": "checked", "text": feed_text[:200]},
        )
        await self._queue_progress(update)

    def _build_binding(
        self,
        snapshot: dict[str, Any] | None,
        *,
        chat_id: int,
        assistant_id: int,
        session_id: str | None = None,
        goal_text: str | None = None,
        goal_created_at: float | None = None,
    ) -> _MissionBinding | None:
        mission_id = self._snapshot_id(snapshot)
        if mission_id is None or snapshot is None:
            return None
        return _MissionBinding(
            mission_id=mission_id,
            assistant_id=assistant_id,
            chat_id=chat_id,
            status=str(snapshot.get("status") or "active"),
            origin=(
                str(snapshot["origin"])
                if snapshot.get("origin") is not None
                else None
            ),
            session_id=session_id,
            goal_text=goal_text,
            goal_created_at=goal_created_at,
            updated_at=self._snapshot_updated_at(snapshot),
        )

    def _merge_fresher_binding(
        self,
        candidate: _MissionBinding | None,
        current: _MissionBinding | None,
    ) -> _MissionBinding | None:
        if candidate is None or current is None or not self._is_current(current):
            return candidate
        if self._mission_key(candidate.mission_id) != self._mission_key(
            current.mission_id
        ):
            return candidate
        comparison = self._revision_compare(
            candidate.updated_at,
            self._latest_revision(
                current.updated_at,
                current.self_patch_updated_at,
            ),
        )
        if comparison is not None and comparison > 0:
            candidate.self_patch_updated_at = current.self_patch_updated_at
            return candidate
        candidate.status = current.status
        candidate.origin = current.origin
        candidate.revision = current.revision
        candidate.updated_at = current.updated_at
        candidate.self_patch_updated_at = current.self_patch_updated_at
        candidate.paused_by_lane = current.paused_by_lane
        candidate.pending_enforcement = current.pending_enforcement
        candidate.pending_abandon = current.pending_abandon
        return candidate

    def _swap_binding(
        self,
        binding: _MissionBinding,
        *,
        replace_assistant: bool,
    ) -> _MissionBinding:
        mission_key = self._mission_key(binding.mission_id)
        existing_key = self._binding_key_by_mission.get(mission_key)
        existing_for_mission = (
            self._bindings.get(existing_key) if existing_key is not None else None
        )
        if existing_for_mission is not None:
            self._drop_binding(
                existing_for_mission,
                cancel_pending=True,
                persist=False,
            )
        if replace_assistant:
            for existing in list(self._bindings.values()):
                if existing.assistant_id == binding.assistant_id:
                    self._drop_binding(
                        existing,
                        cancel_pending=True,
                        persist=False,
                    )
        key = (binding.chat_id, binding.assistant_id)
        self._bindings[key] = binding
        self._binding_key_by_mission[mission_key] = key
        self._persist_bindings()
        return binding

    def _store_binding(
        self,
        snapshot: dict[str, Any] | None,
        *,
        chat_id: int,
        assistant_id: int,
        replace_assistant: bool = False,
        session_id: str | None = None,
        goal_text: str | None = None,
        goal_created_at: float | None = None,
    ) -> _MissionBinding | None:
        binding = self._build_binding(
            snapshot,
            chat_id=chat_id,
            assistant_id=assistant_id,
            session_id=session_id,
            goal_text=goal_text,
            goal_created_at=goal_created_at,
        )
        if binding is None:
            return None
        return self._swap_binding(binding, replace_assistant=replace_assistant)

    def _remove_pending_create(
        self,
        intent: _PendingCreateIntent,
        *,
        persist: bool = True,
    ) -> None:
        try:
            self._pending_creates.remove(intent)
        except ValueError:
            return
        if persist:
            self._persist_bindings()

    def _refresh_pending_create(
        self,
        intent: _PendingCreateIntent,
    ) -> _PendingCreateIntent:
        try:
            index = self._pending_creates.index(intent)
        except ValueError:
            return intent
        refreshed = replace(intent, created_at=time.time())
        self._pending_creates[index] = refreshed
        self._persist_bindings()
        return refreshed

    @staticmethod
    def _create_error_is_ambiguous(exc: BgosApiError) -> bool:
        return exc.status in {408, 429} or exc.status >= 500

    @staticmethod
    def _binding_matches_intent(
        binding: _MissionBinding | None,
        intent: _PendingCreateIntent,
    ) -> bool:
        return bool(
            binding is not None
            and binding.chat_id == intent.chat_id
            and binding.assistant_id == intent.assistant_id
            and binding.session_id == intent.session_id
            and binding.goal_text == intent.goal_text
            and binding.goal_created_at == intent.goal_created_at
        )

    def _apply_snapshot(
        self,
        binding: _MissionBinding,
        response: Any,
        *,
        fallback_status: str,
        persist: bool = True,
    ) -> bool:
        snapshot = self._snapshot(response)
        incoming_updated_at = self._snapshot_updated_at(snapshot)
        comparison = self._revision_compare(
            incoming_updated_at,
            binding.updated_at,
        )
        if comparison is not None and comparison < 0:
            return False
        status = snapshot.get("status") if snapshot is not None else None
        binding.status = str(status or fallback_status)
        if snapshot is not None and snapshot.get("origin") is not None:
            binding.origin = str(snapshot["origin"])
        if incoming_updated_at is not None:
            binding.updated_at = incoming_updated_at
        binding.revision += 1
        if persist:
            self._persist_bindings()
        return True

    def _progress_binding_is_open(self, update: _ProgressUpdate) -> bool:
        mission_key = self._mission_key(update.mission_id)
        binding_key = self._binding_key_by_mission.get(mission_key)
        binding = self._bindings.get(binding_key) if binding_key else None
        return bool(
            binding is not None
            and binding.assistant_id == update.assistant_id
            and binding.status == "active"
        )

    async def _queue_progress(self, update: _ProgressUpdate) -> None:
        mission_key = self._mission_key(update.mission_id)
        lock = self._progress_locks.setdefault(mission_key, asyncio.Lock())
        async with lock:
            if not self._progress_binding_is_open(update):
                return
            loop = asyncio.get_running_loop()
            now = loop.time()
            last = self._last_progress_at.get(mission_key)
            if last is None or now - last >= self._progress_throttle_seconds:
                await self._discard_pending_progress(mission_key)
                await self._send_progress(update)
                if mission_key in self._binding_key_by_mission:
                    self._last_progress_at[mission_key] = loop.time()
                return

            self._pending_progress[mission_key] = update
            task = self._pending_progress_tasks.get(mission_key)
            if task is None or task.done():
                wait_for = self._progress_throttle_seconds - (now - last)
                self._pending_progress_tasks[mission_key] = asyncio.create_task(
                    self._deferred_progress_flush(mission_key, wait_for)
                )

    async def _send_progress(self, update: _ProgressUpdate) -> None:
        try:
            kwargs: dict[str, Any] = {
                "assistant_id": update.assistant_id,
                "mission_id": update.mission_id,
                "feed_entry": update.feed_entry,
            }
            if update.effort is not None:
                kwargs["effort"] = update.effort
            response = await self._mission_patch(
                update.mission_id,
                self._api.patch_mission_progress(**kwargs),
            )
            binding_key = self._binding_key_by_mission.get(
                self._mission_key(update.mission_id)
            )
            binding = self._bindings.get(binding_key) if binding_key else None
            if (
                binding is not None
                and binding.assistant_id == update.assistant_id
                and self._self_patch_response_is_current(binding, response)
            ):
                previous_status = binding.status
                applied = self._apply_snapshot(
                    binding,
                    response,
                    fallback_status=binding.status,
                )
                if (
                    applied
                    and binding.status != previous_status
                    and binding.status
                    in self._OPEN_STATUSES
                    | {"completed", "abandoned", "failed"}
                ):
                    await self._enforce_binding(
                        binding,
                        expected_revision=binding.revision,
                    )
        except Exception as exc:
            if isinstance(exc, BgosApiError) and exc.status == 404:
                self._clear_mission_id(update.mission_id)
            log.warning(
                "mission progress failed assistant=%s mission=%s",
                update.assistant_id,
                update.mission_id,
                exc_info=True,
            )

    async def _deferred_progress_flush(
        self,
        mission_key: str,
        wait_for: float,
    ) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(max(0.0, wait_for))
            lock = self._progress_locks.setdefault(mission_key, asyncio.Lock())
            async with lock:
                update = self._pending_progress.pop(mission_key, None)
                if update is not None and self._progress_binding_is_open(update):
                    await self._send_progress(update)
                    if mission_key in self._binding_key_by_mission:
                        self._last_progress_at[mission_key] = (
                            asyncio.get_running_loop().time()
                        )
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning(
                "deferred mission progress failed mission=%s",
                mission_key,
                exc_info=True,
            )
        finally:
            if self._pending_progress_tasks.get(mission_key) is current:
                self._pending_progress_tasks.pop(mission_key, None)

    async def _discard_pending_progress(self, mission_key: str) -> None:
        self._pending_progress.pop(mission_key, None)
        task = self._pending_progress_tasks.pop(mission_key, None)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_progress(self, mission_id: MissionId) -> None:
        mission_key = self._mission_key(mission_id)
        lock = self._progress_locks.setdefault(mission_key, asyncio.Lock())
        async with lock:
            await self._discard_pending_progress(mission_key)

    async def _flush_progress(self, mission_id: MissionId) -> None:
        mission_key = self._mission_key(mission_id)
        lock = self._progress_locks.setdefault(mission_key, asyncio.Lock())
        async with lock:
            update = self._pending_progress.pop(mission_key, None)
            task = self._pending_progress_tasks.pop(mission_key, None)
            if task is not None and not task.done() and task is not asyncio.current_task():
                if update is not None:
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if update is not None:
                await self._send_progress(update)
                if mission_key in self._binding_key_by_mission:
                    self._last_progress_at[mission_key] = (
                        asyncio.get_running_loop().time()
                    )

    async def _flush_assistant_progress(self, assistant_id: int) -> None:
        for binding in list(self._bindings.values()):
            if binding.assistant_id == assistant_id:
                await self._flush_progress(binding.mission_id)

    def _begin_patch(self, mission_id: MissionId) -> _PatchToken:
        mission_key = self._mission_key(mission_id)
        token = _PatchToken(
            done=asyncio.Event(),
            settled=asyncio.Event(),
            snapshot_acks=set(),
        )
        self._inflight_patches.setdefault(mission_key, set()).add(token)
        binding_key = self._binding_key_by_mission.get(mission_key)
        binding = self._bindings.get(binding_key) if binding_key else None
        if binding is not None:
            binding.revision += 1
        return token

    def _finish_patch(self, mission_id: MissionId, token: _PatchToken) -> None:
        mission_key = self._mission_key(mission_id)
        tokens = self._inflight_patches.get(mission_key)
        if tokens is not None:
            tokens.discard(token)
            if not tokens:
                self._inflight_patches.pop(mission_key, None)
        token.done.set()
        token.settled.set()

    async def _mission_patch(self, mission_id: MissionId, call: Any) -> Any:
        token = self._begin_patch(mission_id)
        try:
            response = await call
            self._record_self_patch(mission_id, response)
            token.done.set()
            if token.snapshot_acks:
                await asyncio.gather(
                    *(ack.wait() for ack in token.snapshot_acks)
                )
            return response
        finally:
            self._finish_patch(mission_id, token)

    def _record_self_patch(self, mission_id: MissionId, response: Any) -> None:
        snapshot = self._snapshot(response)
        updated_at = self._snapshot_updated_at(snapshot)
        if updated_at is None:
            log.warning(
                "mission PATCH response missing updatedAt mission=%s",
                mission_id,
            )
            return
        mission_key = self._mission_key(mission_id)
        binding_key = self._binding_key_by_mission.get(mission_key)
        binding = self._bindings.get(binding_key) if binding_key else None
        if binding is None:
            return
        comparison = self._revision_compare(
            updated_at,
            binding.self_patch_updated_at,
        )
        if (
            binding.self_patch_updated_at is None
            or updated_at == binding.self_patch_updated_at
            or (comparison is not None and comparison >= 0)
        ):
            binding.self_patch_updated_at = updated_at
            binding.revision += 1
            self._persist_bindings()

    def _self_patch_response_is_current(
        self,
        binding: _MissionBinding,
        response: Any,
    ) -> bool:
        if not self._is_current(binding):
            return False
        updated_at = self._snapshot_updated_at(self._snapshot(response))
        if updated_at is None or updated_at != binding.self_patch_updated_at:
            return False
        if binding.updated_at is None or updated_at == binding.updated_at:
            return True
        comparison = self._revision_compare(updated_at, binding.updated_at)
        return comparison is not None and comparison >= 0

    async def _wait_for_inflight_patches(
        self,
        mission_id: MissionId,
    ) -> list[asyncio.Event]:
        mission_key = self._mission_key(mission_id)
        registrations: list[tuple[_PatchToken, asyncio.Event]] = []
        while True:
            registered = {token for token, _ in registrations}
            tokens = tuple(
                token
                for token in self._inflight_patches.get(mission_key, ())
                if token not in registered
            )
            if not tokens:
                return [ack for _, ack in registrations]
            for token in tokens:
                acknowledgement = asyncio.Event()
                token.snapshot_acks.add(acknowledgement)
                registrations.append((token, acknowledgement))
            try:
                await asyncio.gather(*(token.done.wait() for token in tokens))
            except BaseException:
                for token, acknowledgement in registrations:
                    token.snapshot_acks.discard(acknowledgement)
                    acknowledgement.set()
                raise

    async def _wait_for_patch_settlement(self, mission_id: MissionId) -> None:
        mission_key = self._mission_key(mission_id)
        while True:
            tokens = tuple(self._inflight_patches.get(mission_key, ()))
            if not tokens:
                return
            await asyncio.gather(*(token.settled.wait() for token in tokens))

    @staticmethod
    def _acknowledge_patch_snapshots(
        acknowledgements: list[asyncio.Event],
    ) -> None:
        for acknowledgement in acknowledgements:
            acknowledgement.set()

    async def _pause(
        self,
        binding: _MissionBinding,
        reason: str | None,
    ) -> None:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        pause_reason = self._trim(reason, 200)
        try:
            response = await self._mission_patch(
                binding.mission_id,
                self._api.pause_mission(
                    assistant_id=binding.assistant_id,
                    mission_id=binding.mission_id,
                    reason=pause_reason,
                ),
            )
        except Exception as exc:
            self._handle_patch_failure(binding, exc, "pause")
            return
        if self._self_patch_response_is_current(binding, response):
            binding.paused_by_lane = False
            applied = self._apply_snapshot(
                binding,
                response,
                fallback_status="paused",
            )
            if applied:
                await self._enforce_binding(
                    binding,
                    expected_revision=binding.revision,
                )

    async def _resume(self, binding: _MissionBinding) -> bool:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return False
        try:
            response = await self._mission_patch(
                binding.mission_id,
                self._api.resume_mission(
                    assistant_id=binding.assistant_id,
                    mission_id=binding.mission_id,
                ),
            )
        except Exception as exc:
            self._handle_patch_failure(binding, exc, "resume")
            return False
        if self._self_patch_response_is_current(binding, response):
            binding.paused_by_lane = False
            self._apply_snapshot(binding, response, fallback_status="active")
        return self._is_current(binding) and binding.status == "active"

    async def _complete(
        self,
        binding: _MissionBinding,
        summary: str | None,
    ) -> None:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        try:
            response = await self._mission_patch(
                binding.mission_id,
                self._api.complete_mission(
                    assistant_id=binding.assistant_id,
                    mission_id=binding.mission_id,
                    summary=self._trim(summary, 500),
                ),
            )
        except Exception as exc:
            self._handle_patch_failure(binding, exc, "complete")
            return
        if self._self_patch_response_is_current(binding, response):
            self._apply_snapshot(
                binding,
                response,
                fallback_status="completed",
                persist=False,
            )
            if self._is_current(binding):
                self._drop_binding(binding, cancel_pending=True)

    async def _fail(
        self,
        binding: _MissionBinding,
        summary: str | None,
    ) -> None:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        try:
            response = await self._mission_patch(
                binding.mission_id,
                self._api.fail_mission(
                    assistant_id=binding.assistant_id,
                    mission_id=binding.mission_id,
                    summary=self._trim(summary, 500),
                ),
            )
        except Exception as exc:
            self._handle_patch_failure(binding, exc, "fail")
            return
        if self._self_patch_response_is_current(binding, response):
            self._apply_snapshot(
                binding,
                response,
                fallback_status="failed",
                persist=False,
            )
            if self._is_current(binding):
                self._drop_binding(binding, cancel_pending=True)

    async def _abandon(self, binding: _MissionBinding) -> None:
        if not self._is_current(binding):
            return
        binding.pending_abandon = True
        self._persist_bindings()
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        try:
            response = await self._mission_patch(
                binding.mission_id,
                self._api.abandon_mission(
                    assistant_id=binding.assistant_id,
                    mission_id=binding.mission_id,
                ),
            )
        except Exception as exc:
            self._handle_patch_failure(binding, exc, "abandon")
            if isinstance(exc, BgosApiError) and exc.status == 404:
                return
            if self._is_current(binding):
                log.error(
                    "mission abandon remains pending assistant=%s mission=%s",
                    binding.assistant_id,
                    binding.mission_id,
                )
                self.schedule_reconcile()
            return
        if self._self_patch_response_is_current(binding, response):
            self._apply_snapshot(
                binding,
                response,
                fallback_status="abandoned",
                persist=False,
            )
            if self._is_current(binding):
                self._drop_binding(binding, cancel_pending=True)

    def _handle_patch_failure(
        self,
        binding: _MissionBinding,
        exc: Exception,
        transition: str,
    ) -> None:
        if isinstance(exc, BgosApiError) and exc.status == 404:
            self._clear_mission_id(binding.mission_id)
        log.warning(
            "mission %s failed assistant=%s mission=%s",
            transition,
            binding.assistant_id,
            binding.mission_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def _drop_binding(
        self,
        binding: _MissionBinding,
        *,
        cancel_pending: bool,
        persist: bool = True,
    ) -> None:
        key = (binding.chat_id, binding.assistant_id)
        if self._bindings.get(key) is binding:
            self._bindings.pop(key, None)
        mission_key = self._mission_key(binding.mission_id)
        if self._binding_key_by_mission.get(mission_key) == key:
            self._binding_key_by_mission.pop(mission_key, None)
        if cancel_pending:
            task = self._pending_progress_tasks.pop(mission_key, None)
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
            self._pending_progress.pop(mission_key, None)
            self._last_progress_at.pop(mission_key, None)
        if persist:
            self._persist_bindings()

    def _clear_mission_id(self, mission_id: MissionId) -> None:
        mission_key = self._mission_key(mission_id)
        binding_key = self._binding_key_by_mission.get(mission_key)
        binding = self._bindings.get(binding_key) if binding_key else None
        if binding is not None:
            self._drop_binding(binding, cancel_pending=True)

    async def handle_mission_event(self, event: dict[str, Any]) -> None:
        """Apply a mission snapshot and enforce app controls on GoalManager."""
        try:
            snapshot = event.get("mission") if isinstance(event, dict) else None
            mission_id = self._snapshot_id(snapshot)
            if mission_id is None:
                return
            mission_key = self._mission_key(mission_id)
            lock = self._mission_event_locks.setdefault(
                mission_key,
                asyncio.Lock(),
            )
            async with lock:
                acknowledgements = await self._wait_for_inflight_patches(
                    mission_id
                )
                try:
                    await self._handle_mission_event(
                        event,
                        acknowledgements=acknowledgements,
                    )
                finally:
                    self._acknowledge_patch_snapshots(acknowledgements)
        except Exception:
            log.warning("mission event handling failed", exc_info=True)

    async def _handle_mission_event(
        self,
        event: dict[str, Any],
        *,
        acknowledgements: list[asyncio.Event],
    ) -> None:
        if not isinstance(event, dict):
            return
        event_type = event.get("event_type") or event.get("eventType")
        if not isinstance(event_type, str):
            return
        snapshot = event.get("mission")
        if not isinstance(snapshot, dict):
            return
        mission_id = self._snapshot_id(snapshot)
        if mission_id is None:
            return
        mission_key = self._mission_key(mission_id)
        binding_key = self._binding_key_by_mission.get(mission_key)
        binding = self._bindings.get(binding_key) if binding_key else None
        if binding is None and event_type == "mission_created":
            binding = self._bind_pending_create(snapshot)
        if binding is None:
            return
        event_assistant = self._coerce_int(
            snapshot.get("assistantId", snapshot.get("assistant_id"))
        )
        if event_assistant is not None and event_assistant != binding.assistant_id:
            return
        if snapshot.get("origin") not in (None, "derived"):
            return

        previous_status = binding.status
        event_updated_at = self._snapshot_updated_at(snapshot)
        self_comparison = self._revision_compare(
            event_updated_at,
            binding.self_patch_updated_at,
        )
        if self_comparison is not None and self_comparison <= 0:
            return
        current_comparison = self._revision_compare(
            event_updated_at,
            binding.updated_at,
        )
        if current_comparison is not None and current_comparison < 0:
            return
        if not self._apply_snapshot(
            binding,
            snapshot,
            fallback_status=previous_status,
        ):
            return
        expected_revision = binding.revision
        self._acknowledge_patch_snapshots(acknowledgements)
        if binding.status in {"paused", "completed", "abandoned", "failed"}:
            await self._cancel_progress(binding.mission_id)
            if (
                not self._is_current(binding)
                or binding.revision != expected_revision
            ):
                return
        if event_type not in self._STATE_TRANSITION_EVENTS:
            return
        await self._enforce_binding(
            binding,
            expected_revision=expected_revision,
            explicit_resume=event_type == "mission_resumed",
        )

    def _bind_pending_create(
        self,
        snapshot: dict[str, Any],
    ) -> _MissionBinding | None:
        if snapshot.get("origin") != "derived":
            return None
        assistant_id = self._coerce_int(
            snapshot.get("assistantId", snapshot.get("assistant_id"))
        )
        title = snapshot.get("title")
        status = str(snapshot.get("status") or "")
        if (
            assistant_id is None
            or not isinstance(title, str)
            or status not in self._OPEN_STATUSES
        ):
            return None
        now = time.time()
        self._pending_creates = [
            intent
            for intent in self._pending_creates
            if now - intent.created_at <= self._PENDING_CREATE_SECONDS
        ]
        matches = [
            intent
            for intent in self._pending_creates
            if (
                intent.assistant_id == assistant_id
                and intent.title == title
            )
        ]
        if len(matches) != 1:
            if matches:
                log.error(
                    "ambiguous mission create event assistant=%s title=%r",
                    assistant_id,
                    title,
                )
            self._persist_bindings()
            return None
        intent = matches[0]
        candidate = self._build_binding(
            snapshot,
            chat_id=intent.chat_id,
            assistant_id=intent.assistant_id,
            session_id=intent.session_id,
            goal_text=intent.goal_text,
            goal_created_at=intent.goal_created_at,
        )
        if candidate is None:
            return None
        self._remove_pending_create(intent, persist=False)
        return self._swap_binding(candidate, replace_assistant=True)

    async def _resolve_manager_for_binding(
        self,
        binding: _MissionBinding,
        *,
        expected_revision: int,
        terminal: bool,
    ) -> tuple[Any, bool, Any] | None:
        if binding.goal_text is None or (
            not binding.session_id and self._session_id_resolver is None
        ):
            log.error(
                "mission goal identity is incomplete assistant=%s mission=%s",
                binding.assistant_id,
                binding.mission_id,
            )
            self._mark_enforcement_pending(binding, expected_revision)
            return None
        identity = (
            binding.mission_id,
            binding.session_id,
            binding.goal_text,
            binding.goal_created_at,
        )
        session_id: str | None = None
        if self._session_id_resolver is None:
            session_id = binding.session_id
        else:
            for attempt in range(2):
                try:
                    resolved = await self._resolve(
                        self._session_id_resolver(
                            binding.chat_id,
                            binding.assistant_id,
                        )
                    )
                except Exception:
                    resolved = None
                    log.warning(
                        "mission session resolution attempt failed "
                        "assistant=%s mission=%s attempt=%s",
                        binding.assistant_id,
                        binding.mission_id,
                        attempt + 1,
                        exc_info=True,
                    )
                if (
                    not self._is_current(binding)
                    or binding.revision != expected_revision
                    or identity
                    != (
                        binding.mission_id,
                        binding.session_id,
                        binding.goal_text,
                        binding.goal_created_at,
                    )
                ):
                    return None
                if isinstance(resolved, str) and resolved:
                    session_id = resolved
                    break
        if not session_id:
            self._mark_enforcement_pending(binding, expected_revision)
            return None
        if binding.session_id is not None and binding.session_id != session_id:
            self._mark_enforcement_pending(binding, expected_revision)
            log.error(
                "mission session identity changed assistant=%s mission=%s "
                "expected=%s resolved=%s",
                binding.assistant_id,
                binding.mission_id,
                binding.session_id,
                session_id,
            )
            return None
        try:
            manager = self._manager_factory()(session_id)
            state_declared, state = self._manager_state(manager)
        except Exception:
            log.error(
                "mission GoalManager load failed session=%s mission=%s",
                session_id,
                binding.mission_id,
                exc_info=True,
            )
            self._mark_enforcement_pending(binding, expected_revision)
            return None
        if not self._is_current(binding) or binding.revision != expected_revision:
            return None
        if not state_declared:
            log.error(
                "mission GoalManager state unavailable session=%s mission=%s",
                session_id,
                binding.mission_id,
            )
            self._mark_enforcement_pending(binding, expected_revision)
            return None
        if state is None:
            if terminal:
                return manager, state_declared, state
            self._mark_enforcement_pending(binding, expected_revision)
            return None
        state_goal = getattr(state, "goal", None)
        state_created_at = self._optional_float(
            getattr(state, "created_at", None)
        )
        if state_goal != binding.goal_text:
            self._mark_enforcement_pending(binding, expected_revision)
            log.error(
                "mission goal identity changed session=%s mission=%s",
                session_id,
                binding.mission_id,
            )
            return None
        if state_created_at is None:
            self._mark_enforcement_pending(binding, expected_revision)
            log.error(
                "mission goal creation identity unavailable session=%s "
                "mission=%s",
                session_id,
                binding.mission_id,
            )
            return None
        if (
            binding.goal_created_at is not None
            and binding.goal_created_at != state_created_at
        ):
            self._mark_enforcement_pending(binding, expected_revision)
            log.error(
                "mission goal creation identity changed session=%s mission=%s",
                session_id,
                binding.mission_id,
            )
            return None
        if binding.session_id is None:
            binding.session_id = session_id
        if binding.goal_created_at is None:
            binding.goal_created_at = state_created_at
        self._persist_bindings()
        return manager, state_declared, state

    def _mark_enforcement_pending(
        self,
        binding: _MissionBinding,
        expected_revision: int,
    ) -> None:
        if not self._is_current(binding) or binding.revision != expected_revision:
            return
        binding.pending_enforcement = True
        self._persist_bindings()
        log.error(
            "mission control enforcement pending assistant=%s mission=%s "
            "status=%s",
            binding.assistant_id,
            binding.mission_id,
            binding.status,
        )
        if asyncio.current_task() is not self._reconcile_owner_task:
            self.schedule_reconcile()

    async def _enforce_binding(
        self,
        binding: _MissionBinding,
        *,
        expected_revision: int,
        explicit_resume: bool = False,
        status_override: str | None = None,
    ) -> bool:
        status = status_override or binding.status
        terminal = status in {"completed", "abandoned", "failed", "closed"}
        resolved = await self._resolve_manager_for_binding(
            binding,
            expected_revision=expected_revision,
            terminal=terminal,
        )
        if resolved is None:
            return False
        if not self._is_current(binding) or binding.revision != expected_revision:
            return False
        manager, state_declared, state = resolved
        current_status = getattr(state, "status", None) if state is not None else None
        paused_reason = (
            getattr(state, "paused_reason", None) if state is not None else None
        )
        has_wait_barrier = bool(
            state is not None
            and (
                getattr(state, "waiting_on_pid", None) is not None
                or getattr(state, "waiting_on_session", None) is not None
                or getattr(state, "waiting_until", 0.0)
            )
        )
        try:
            if status == "paused":
                if current_status != "paused":
                    manager.pause(reason=self._PAUSE_REASON)
                    binding.paused_by_lane = True
                else:
                    binding.paused_by_lane = (
                        paused_reason == self._PAUSE_REASON
                    )
            elif status == "active":
                should_resume = bool(
                    current_status not in {"active", "paused"}
                    or (
                        current_status == "paused"
                        and (
                            explicit_resume
                            or binding.paused_by_lane
                            or paused_reason == self._PAUSE_REASON
                        )
                    )
                    or (
                        explicit_resume
                        and current_status == "active"
                        and has_wait_barrier
                    )
                )
                if should_resume:
                    manager.resume(reset_budget=False)
                binding.paused_by_lane = False
            elif status == "completed":
                if state is not None and current_status != "done":
                    manager.mark_done("Marked done from the app")
            elif status in {"abandoned", "failed", "closed"}:
                if state is not None:
                    manager.clear()
        except Exception:
            log.error(
                "mission GoalManager enforcement failed session=%s mission=%s "
                "status=%s",
                binding.session_id,
                binding.mission_id,
                status,
                exc_info=True,
            )
            self._mark_enforcement_pending(binding, expected_revision)
            return False
        if not self._is_current(binding) or binding.revision != expected_revision:
            return False
        binding.pending_enforcement = False
        if terminal:
            self._drop_binding(binding, cancel_pending=True)
        else:
            self._persist_bindings()
        return True

    def schedule_reconcile(self) -> None:
        if self._closed:
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            self._reconcile_requested = True
            return
        task = asyncio.create_task(self.reconcile())
        self._reconcile_task = task
        task.add_done_callback(self._reconcile_done)

    def _reconcile_done(self, task: asyncio.Task[None]) -> None:
        if self._reconcile_task is task:
            self._reconcile_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.error("mission reconciliation task failed", exc_info=True)

    async def reconcile(self) -> None:
        """Refresh persisted bindings and enforce the server state locally."""
        async with self._reconcile_lock:
            self._reconcile_owner_task = asyncio.current_task()
            try:
                while True:
                    self._reconcile_requested = False
                    await self._reconcile_pending_creates()
                    for binding in list(self._bindings.values()):
                        await self._reconcile_binding(binding)
                    if not self._reconcile_requested:
                        break
            finally:
                self._reconcile_owner_task = None

    async def _reconcile_pending_creates(self) -> None:
        now = time.time()
        recent = [
            intent
            for intent in self._pending_creates
            if now - intent.created_at <= self._PENDING_CREATE_SECONDS
        ]
        if len(recent) != len(self._pending_creates):
            self._pending_creates = recent
            self._persist_bindings()
        for assistant_id in {intent.assistant_id for intent in recent}:
            try:
                response = await self._api.get_active_mission(
                    assistant_id=assistant_id,
                )
            except Exception:
                log.error(
                    "mission pending create reconciliation GET failed "
                    "assistant=%s",
                    assistant_id,
                    exc_info=True,
                )
                continue
            snapshot = self._snapshot(response)
            mission_id = self._snapshot_id(snapshot)
            if mission_id is None:
                continue
            if self._mission_key(mission_id) in self._binding_key_by_mission:
                continue
            if snapshot is not None:
                self._bind_pending_create(snapshot)

    async def _reconcile_binding(self, binding: _MissionBinding) -> None:
        if not self._is_current(binding):
            return
        await self._wait_for_patch_settlement(binding.mission_id)
        if not self._is_current(binding):
            return
        expected_revision = binding.revision
        try:
            response = await self._api.get_active_mission(
                assistant_id=binding.assistant_id,
            )
        except Exception:
            log.error(
                "mission reconciliation GET failed assistant=%s mission=%s",
                binding.assistant_id,
                binding.mission_id,
                exc_info=True,
            )
            return
        if not self._is_current(binding) or binding.revision != expected_revision:
            return
        snapshot = self._snapshot(response)
        snapshot_id = self._snapshot_id(snapshot)
        if (
            snapshot_id is None
            or self._mission_key(snapshot_id)
            != self._mission_key(binding.mission_id)
        ):
            terminal_status = (
                binding.status
                if binding.status in {"completed", "abandoned", "failed"}
                else "closed"
            )
            await self._enforce_binding(
                binding,
                expected_revision=expected_revision,
                status_override=terminal_status,
            )
            return
        incoming_updated_at = self._snapshot_updated_at(snapshot)
        comparison = self._revision_compare(
            incoming_updated_at,
            self._latest_revision(
                binding.updated_at,
                binding.self_patch_updated_at,
            ),
        )
        if comparison is not None and comparison < 0:
            return
        if not self._apply_snapshot(
            binding,
            snapshot,
            fallback_status=binding.status,
        ):
            return
        expected_revision = binding.revision
        if binding.pending_abandon:
            await self._retry_pending_abandon(binding, expected_revision)
            return
        await self._enforce_binding(
            binding,
            expected_revision=expected_revision,
        )

    async def _retry_pending_abandon(
        self,
        binding: _MissionBinding,
        expected_revision: int,
    ) -> None:
        if not self._is_current(binding) or binding.revision != expected_revision:
            return
        try:
            response = await self._mission_patch(
                binding.mission_id,
                self._api.abandon_mission(
                    assistant_id=binding.assistant_id,
                    mission_id=binding.mission_id,
                ),
            )
        except Exception as exc:
            if isinstance(exc, BgosApiError) and exc.status == 404:
                if self._is_current(binding):
                    self._drop_binding(binding, cancel_pending=True)
                return
            log.error(
                "mission pending abandon retry failed assistant=%s mission=%s",
                binding.assistant_id,
                binding.mission_id,
                exc_info=True,
            )
            return
        if not self._self_patch_response_is_current(binding, response):
            return
        self._apply_snapshot(
            binding,
            response,
            fallback_status="abandoned",
        )
        if self._is_current(binding):
            self._drop_binding(binding, cancel_pending=True)

    async def close(self) -> None:
        """Cancel deferred progress flushes before the API client closes."""
        self._closed = True
        tasks = [
            task
            for task in self._pending_progress_tasks.values()
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        self._pending_progress_tasks.clear()
        self._pending_progress.clear()
        self._progress_locks.clear()
        self._goal_line_locks.clear()
        self._mission_event_locks.clear()
        for tokens in self._inflight_patches.values():
            for token in tokens:
                token.done.set()
                token.settled.set()
                self._acknowledge_patch_snapshots(
                    list(token.snapshot_acks)
                )
        self._inflight_patches.clear()
        reconcile_task = self._reconcile_task
        if reconcile_task is not None and not reconcile_task.done():
            reconcile_task.cancel()
            tasks.append(reconcile_task)
        self._reconcile_task = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
