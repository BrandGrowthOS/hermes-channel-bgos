"""Bridge Hermes goal-loop notices to BGOS mission cards."""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from .bgos_api import BgosApiError

log = logging.getLogger(__name__)

GoalLineKind = Literal[
    "set",
    "continue",
    "done",
    "cleared",
    "paused",
    "parked",
]
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
_PAUSED_PREFIX = "⏸ Goal paused"
_PARKED_PREFIX = "⏳ Goal parked"
_TURN_COUNT_RE = re.compile(r"(?P<used>\d+)\s*/\s*(?P<budget>\d+)")
_LEADING_SEPARATOR_RE = re.compile(r"^(?:\(judge\)\s*)?(?::|[\u2013\u2014-])?\s*")


@dataclass(frozen=True)
class GoalLine:
    kind: GoalLineKind
    goal_text: str | None = None
    reason: str | None = None
    turns_used: int | None = None
    max_turns: int | None = None


def _number(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _optional_text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _status_tail(line: str, prefix: str) -> str | None:
    tail = line[len(prefix):].strip()
    tail = _LEADING_SEPARATOR_RE.sub("", tail, count=1).strip()
    return tail or None


def classify_goal_line(text: str | None) -> GoalLine | None:
    """Classify the first visible line of an upstream Hermes goal notice."""
    if not isinstance(text, str) or not text.strip():
        return None
    first_line = text.strip().splitlines()[0].strip()

    match = _SET_RE.fullmatch(first_line)
    if match is not None:
        return GoalLine(
            kind="set",
            goal_text=_optional_text(
                match.group("goal") or match.group("goal_plain")
            ),
            max_turns=_number(match.group("budget")),
        )

    match = _CONTINUE_RE.fullmatch(first_line)
    if match is not None:
        return GoalLine(
            kind="continue",
            reason=_optional_text(
                match.group("reason") or match.group("reason_plain")
            ),
            turns_used=_number(match.group("used")),
            max_turns=_number(match.group("budget")),
        )

    match = _CLEARED_RE.fullmatch(first_line)
    if match is not None:
        return GoalLine(kind="cleared")

    match = _DONE_RE.fullmatch(first_line)
    if match is not None:
        return GoalLine(
            kind="done",
            reason=_optional_text(match.group("reason")),
        )

    if first_line == _PAUSED_PREFIX or first_line.startswith(
        f"{_PAUSED_PREFIX} "
    ) or first_line.startswith(f"{_PAUSED_PREFIX}:"):
        counts = _TURN_COUNT_RE.search(first_line)
        return GoalLine(
            kind="paused",
            reason=_status_tail(first_line, _PAUSED_PREFIX),
            turns_used=_number(counts.group("used")) if counts else None,
            max_turns=_number(counts.group("budget")) if counts else None,
        )

    if first_line == _PARKED_PREFIX or first_line.startswith(
        f"{_PARKED_PREFIX} "
    ) or first_line.startswith(f"{_PARKED_PREFIX}:"):
        counts = _TURN_COUNT_RE.search(first_line)
        return GoalLine(
            kind="parked",
            reason=_status_tail(first_line, _PARKED_PREFIX),
            turns_used=_number(counts.group("used")) if counts else None,
            max_turns=_number(counts.group("budget")) if counts else None,
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
    _RECENT_TRANSITION_SECONDS = 30.0

    def __init__(
        self,
        api: Any,
        *,
        assistant_id_resolver: Callable[[int], Any] | None = None,
        session_id_resolver: Callable[[int, int], Any] | None = None,
        goal_manager_factory: Callable[..., Any] | None = None,
        progress_throttle_seconds: float = 3.0,
    ) -> None:
        self._api = api
        self._assistant_id_resolver = assistant_id_resolver
        self._session_id_resolver = session_id_resolver
        self._goal_manager_factory = goal_manager_factory
        self._progress_throttle_seconds = float(progress_throttle_seconds)
        self._bindings: dict[tuple[int, int], _MissionBinding] = {}
        self._binding_key_by_mission: dict[str, tuple[int, int]] = {}
        self._pending_progress: dict[str, _ProgressUpdate] = {}
        self._pending_progress_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_progress_at: dict[str, float] = {}
        self._progress_locks: dict[str, asyncio.Lock] = {}
        self._goal_line_locks: dict[int, asyncio.Lock] = {}
        self._mission_event_locks: dict[str, asyncio.Lock] = {}
        self._recent_transitions: dict[tuple[str, str], float] = {}
        self._inflight_transitions: dict[tuple[str, str], bool] = {}

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
        for existing in list(self._bindings.values()):
            if existing.assistant_id == assistant_id:
                self._drop_binding(existing, cancel_pending=True)
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

        title = self._trim(goal_line.goal_text, 200)
        active = self._snapshot(response)
        mission_id = self._snapshot_id(active)
        can_adopt = bool(
            title is not None
            and mission_id is not None
            and active is not None
            and active.get("origin") == "derived"
            and active.get("status") in self._OPEN_STATUSES
            and active.get("title") == title
        )
        if can_adopt:
            binding = self._store_binding(
                active,
                chat_id=chat_id,
                assistant_id=assistant_id,
                replace_assistant=True,
            )
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
            try:
                response = await self._api.create_mission(**kwargs)
            except Exception:
                log.warning(
                    "mission create failed assistant=%s",
                    assistant_id,
                    exc_info=True,
                )
                return
            binding = self._store_binding(
                self._snapshot(response),
                chat_id=chat_id,
                assistant_id=assistant_id,
                replace_assistant=True,
            )

        if binding is not None and binding.status == "paused":
            await self._resume(binding)

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

    def _store_binding(
        self,
        snapshot: dict[str, Any] | None,
        *,
        chat_id: int,
        assistant_id: int,
        replace_assistant: bool = False,
    ) -> _MissionBinding | None:
        mission_id = self._snapshot_id(snapshot)
        if mission_id is None or snapshot is None:
            return None
        if replace_assistant:
            for key, existing in list(self._bindings.items()):
                if existing.assistant_id == assistant_id:
                    self._drop_binding(existing, cancel_pending=True)
        binding = _MissionBinding(
            mission_id=mission_id,
            assistant_id=assistant_id,
            chat_id=chat_id,
            status=str(snapshot.get("status") or "active"),
            origin=(
                str(snapshot["origin"])
                if snapshot.get("origin") is not None
                else None
            ),
        )
        key = (chat_id, assistant_id)
        self._bindings[key] = binding
        self._binding_key_by_mission[self._mission_key(mission_id)] = key
        return binding

    def _apply_snapshot(
        self,
        binding: _MissionBinding,
        response: Any,
        *,
        fallback_status: str,
    ) -> None:
        snapshot = self._snapshot(response)
        status = snapshot.get("status") if snapshot is not None else None
        binding.status = str(status or fallback_status)
        if snapshot is not None and snapshot.get("origin") is not None:
            binding.origin = str(snapshot["origin"])

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
            await self._api.patch_mission_progress(
                **kwargs,
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

    def _begin_transition(
        self,
        binding: _MissionBinding,
        event_type: str,
    ) -> tuple[str, str]:
        transition = (self._mission_key(binding.mission_id), event_type)
        now = asyncio.get_running_loop().time()
        self._recent_transitions = {
            key: sent_at
            for key, sent_at in self._recent_transitions.items()
            if now - sent_at <= self._RECENT_TRANSITION_SECONDS
        }
        self._inflight_transitions[transition] = False
        return transition

    def _finish_transition(self, transition: tuple[str, str]) -> None:
        echo_seen = self._inflight_transitions.pop(transition, False)
        if not echo_seen:
            self._recent_transitions[transition] = (
                asyncio.get_running_loop().time()
            )

    def _cancel_transition(self, transition: tuple[str, str]) -> None:
        self._inflight_transitions.pop(transition, None)

    async def _pause(
        self,
        binding: _MissionBinding,
        reason: str | None,
    ) -> None:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        revision = binding.revision
        transition = self._begin_transition(binding, "mission_paused")
        try:
            response = await self._api.pause_mission(
                assistant_id=binding.assistant_id,
                mission_id=binding.mission_id,
                reason=self._trim(reason, 200),
            )
        except Exception as exc:
            self._cancel_transition(transition)
            self._handle_patch_failure(binding, exc, "pause")
            return
        if self._is_current(binding) and binding.revision == revision:
            self._apply_snapshot(binding, response, fallback_status="paused")
        self._finish_transition(transition)

    async def _resume(self, binding: _MissionBinding) -> bool:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return False
        revision = binding.revision
        transition = self._begin_transition(binding, "mission_resumed")
        try:
            response = await self._api.resume_mission(
                assistant_id=binding.assistant_id,
                mission_id=binding.mission_id,
            )
        except Exception as exc:
            self._cancel_transition(transition)
            self._handle_patch_failure(binding, exc, "resume")
            return False
        if self._is_current(binding) and binding.revision == revision:
            self._apply_snapshot(binding, response, fallback_status="active")
        self._finish_transition(transition)
        return self._is_current(binding) and binding.status == "active"

    async def _complete(
        self,
        binding: _MissionBinding,
        summary: str | None,
    ) -> None:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        revision = binding.revision
        transition = self._begin_transition(binding, "mission_completed")
        try:
            response = await self._api.complete_mission(
                assistant_id=binding.assistant_id,
                mission_id=binding.mission_id,
                summary=self._trim(summary, 500),
            )
        except Exception as exc:
            self._cancel_transition(transition)
            self._handle_patch_failure(binding, exc, "complete")
            return
        if self._is_current(binding) and binding.revision == revision:
            self._apply_snapshot(binding, response, fallback_status="completed")
        self._finish_transition(transition)

    async def _fail(
        self,
        binding: _MissionBinding,
        summary: str | None,
    ) -> None:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        revision = binding.revision
        transition = self._begin_transition(binding, "mission_failed")
        try:
            response = await self._api.fail_mission(
                assistant_id=binding.assistant_id,
                mission_id=binding.mission_id,
                summary=self._trim(summary, 500),
            )
        except Exception as exc:
            self._cancel_transition(transition)
            self._handle_patch_failure(binding, exc, "fail")
            return
        if self._is_current(binding) and binding.revision == revision:
            self._apply_snapshot(binding, response, fallback_status="failed")
        self._finish_transition(transition)

    async def _abandon(self, binding: _MissionBinding) -> None:
        await self._flush_progress(binding.mission_id)
        if not self._is_current(binding):
            return
        try:
            await self._api.abandon_mission(
                assistant_id=binding.assistant_id,
                mission_id=binding.mission_id,
            )
        except Exception as exc:
            self._handle_patch_failure(binding, exc, "abandon")
        finally:
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
                await self._handle_mission_event(event)
        except Exception:
            log.warning("mission event handling failed", exc_info=True)

    async def _handle_mission_event(self, event: dict[str, Any]) -> None:
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
        transition = (mission_key, event_type)
        if transition in self._inflight_transitions:
            if event_type in self._STATE_TRANSITION_EVENTS:
                binding.revision += 1
            self._inflight_transitions[transition] = True
            self._apply_snapshot(binding, snapshot, fallback_status=binding.status)
            return

        event_status = str(snapshot.get("status") or previous_status)
        sent_at = self._recent_transitions.get(transition)
        if sent_at is not None:
            age = asyncio.get_running_loop().time() - sent_at
            self._recent_transitions.pop(transition, None)
            if (
                age <= self._RECENT_TRANSITION_SECONDS
                and previous_status == event_status
            ):
                self._apply_snapshot(
                    binding,
                    snapshot,
                    fallback_status=previous_status,
                )
                return

        if event_type in self._STATE_TRANSITION_EVENTS:
            binding.revision += 1
        self._apply_snapshot(binding, snapshot, fallback_status=previous_status)
        if binding.status in {"paused", "completed", "abandoned", "failed"}:
            await self._cancel_progress(binding.mission_id)
        if event_type not in self._CONTROL_EVENTS:
            return
        if self._session_id_resolver is None:
            return
        session_id = await self._resolve(
            self._session_id_resolver(binding.chat_id, binding.assistant_id)
        )
        if not isinstance(session_id, str) or not session_id:
            return

        factory = self._goal_manager_factory or _load_goal_manager()
        manager = factory(session_id)
        state_declared = (
            "state" in vars(manager)
            or hasattr(type(manager), "state")
        )
        state = getattr(manager, "state", None) if state_declared else None
        current_status = getattr(state, "status", None) if state is not None else None
        should_act_without_state = not state_declared
        has_wait_barrier = bool(
            state is not None
            and (
                getattr(state, "waiting_on_pid", None) is not None
                or getattr(state, "waiting_on_session", None) is not None
                or getattr(state, "waiting_until", 0.0)
            )
        )
        if event_type == "mission_paused":
            if should_act_without_state or current_status == "active":
                manager.pause()
        elif event_type == "mission_resumed":
            if (
                should_act_without_state
                or current_status == "paused"
                or (current_status == "active" and has_wait_barrier)
            ):
                manager.resume()
        elif event_type == "mission_completed":
            if should_act_without_state or current_status in {"active", "paused"}:
                manager.mark_done("Marked done from the app")

    async def close(self) -> None:
        """Cancel deferred progress flushes before the API client closes."""
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
        self._inflight_transitions.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
