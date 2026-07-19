"""Tests for the BGOS mission lane derived from Hermes goal notices."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import hermes_channel_bgos.missions_bridge as missions_bridge
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.bgos_api import BgosApiError
from hermes_channel_bgos.bgos_ws import BgosWs
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.missions_bridge import MissionLane, classify_goal_line


def _snapshot(
    mission_id: str = "mission-1",
    *,
    status: str = "active",
    assistant_id: int = 7,
    origin: str = "derived",
    title: str = "Draft the replies",
) -> dict:
    return {
        "id": mission_id,
        "assistantId": assistant_id,
        "status": status,
        "origin": origin,
        "title": title,
    }


def _api(*, active: dict | None = None) -> SimpleNamespace:
    created = _snapshot()
    return SimpleNamespace(
        get_active_mission=AsyncMock(return_value={"mission": active}),
        create_mission=AsyncMock(
            return_value={"ok": True, "mission": created},
        ),
        patch_mission_progress=AsyncMock(
            return_value={"ok": True, "mission": created},
        ),
        pause_mission=AsyncMock(
            return_value={
                "ok": True,
                "mission": _snapshot(status="paused"),
            },
        ),
        resume_mission=AsyncMock(
            return_value={"ok": True, "mission": created},
        ),
        complete_mission=AsyncMock(
            return_value={
                "ok": True,
                "mission": _snapshot(status="completed"),
            },
        ),
        fail_mission=AsyncMock(
            return_value={
                "ok": True,
                "mission": _snapshot(status="failed"),
            },
        ),
        abandon_mission=AsyncMock(
            return_value={
                "ok": True,
                "mission": _snapshot(status="abandoned"),
            },
        ),
    )


@pytest.mark.parametrize(
    ("text", "kind", "goal_text", "reason", "used", "budget"),
    [
        (
            "⊙ Goal set (20-turn budget): Draft the customer replies",
            "set",
            "Draft the customer replies",
            None,
            None,
            20,
        ),
        (
            "↻ Continuing toward goal (3/20): still drafting replies",
            "continue",
            None,
            "still drafting replies",
            3,
            20,
        ),
        (
            "✓ Goal achieved: all replies are drafted",
            "done",
            None,
            "all replies are drafted",
            None,
            None,
        ),
        (
            "✓ Goal cleared.",
            "cleared",
            None,
            None,
            None,
            None,
        ),
        (
            "⏸ Goal paused \u2014 4/20 turns used. Use /goal resume.",
            "paused",
            None,
            "4/20 turns used. Use /goal resume.",
            4,
            20,
        ),
        (
            "⏳ Goal parked \u2014 waiting on pid 99: tests are running",
            "parked",
            None,
            "waiting on pid 99: tests are running",
            None,
            None,
        ),
    ],
)
def test_classify_goal_line_kinds(
    text: str,
    kind: str,
    goal_text: str | None,
    reason: str | None,
    used: int | None,
    budget: int | None,
) -> None:
    result = classify_goal_line(text)

    assert result is not None
    assert result.kind == kind
    assert result.goal_text == goal_text
    assert result.reason == reason
    assert result.turns_used == used
    assert result.max_turns == budget


def test_classify_set_uses_only_first_line() -> None:
    result = classify_goal_line(
        "⊙ Goal set (8-turn budget): Ship the report\n"
        "Completion contract:\n- Verification: tests pass"
    )

    assert result is not None
    assert result.goal_text == "Ship the report"
    assert result.max_turns == 8


def test_classify_goal_cleared_without_period() -> None:
    result = classify_goal_line("✓ Goal cleared")

    assert result is not None
    assert result.kind == "cleared"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ordinary assistant prose",
        '📖 read_file: "/tmp/input.txt"',
        "✓ Goal cleared: no active goal",
    ],
)
def test_classify_goal_line_negatives(text: str) -> None:
    assert classify_goal_line(text) is None


@pytest.mark.asyncio
async def test_set_creates_derived_mission_with_turn_budget() -> None:
    api = _api()
    lane = MissionLane(api)

    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    api.get_active_mission.assert_awaited_once_with(assistant_id=7)
    api.create_mission.assert_awaited_once_with(
        assistant_id=7,
        title="Draft the customer replies",
        origin="derived",
        effort={"used": 0, "budget": 20, "unit": "turns"},
        first_feed_text="Goal set",
    )
    await lane.close()


@pytest.mark.asyncio
async def test_set_adopts_open_derived_mission() -> None:
    existing = _snapshot(
        "mission-existing",
        title="Draft the customer replies",
    )
    api = _api(active=existing)
    lane = MissionLane(api)

    await lane.handle_goal_line(
        "⊙ Goal set (10-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    api.create_mission.assert_not_awaited()
    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/10): outlining",
        chat_id=42,
        assistant_id=7,
    )
    assert api.patch_mission_progress.await_args.kwargs["mission_id"] == (
        "mission-existing"
    )
    await lane.close()


@pytest.mark.asyncio
async def test_set_after_clear_replaces_stale_different_title() -> None:
    stale = _snapshot("mission-stale", title="Old goal")
    api = _api(active=stale)
    lane = MissionLane(api)

    await lane.handle_goal_line(
        "⊙ Goal set (10-turn budget): New goal",
        chat_id=42,
        assistant_id=7,
    )

    api.create_mission.assert_awaited_once_with(
        assistant_id=7,
        title="New goal",
        origin="derived",
        effort={"used": 0, "budget": 10, "unit": "turns"},
        first_feed_text="Goal set",
    )
    await lane.close()


@pytest.mark.asyncio
async def test_set_without_goal_text_never_adopts() -> None:
    api = _api(active=_snapshot("mission-stale", title="Goal"))
    lane = MissionLane(api)

    await lane.handle_goal_line(
        "⊙ Goal set (10-turn budget)",
        chat_id=42,
        assistant_id=7,
    )

    api.create_mission.assert_awaited_once_with(
        assistant_id=7,
        title="Goal",
        origin="derived",
        effort={"used": 0, "budget": 10, "unit": "turns"},
        first_feed_text="Goal set",
    )
    await lane.close()


@pytest.mark.asyncio
async def test_set_replaces_non_derived_active_mission() -> None:
    api = _api(active=_snapshot("manual-1", origin="manual"))
    lane = MissionLane(api)

    await lane.handle_goal_line(
        "⊙ Goal set (10-turn budget): Derived objective",
        chat_id=42,
        assistant_id=7,
    )

    api.create_mission.assert_awaited_once()
    await lane.close()


@pytest.mark.asyncio
async def test_set_adopting_paused_mission_resumes_it() -> None:
    api = _api(active=_snapshot(
        "mission-paused",
        status="paused",
        title="Resume the objective",
    ))
    lane = MissionLane(api)

    await lane.handle_goal_line(
        "⊙ Goal set (10-turn budget): Resume the objective",
        chat_id=42,
        assistant_id=7,
    )

    api.resume_mission.assert_awaited_once_with(
        assistant_id=7,
        mission_id="mission-paused",
    )
    await lane.close()


@pytest.mark.asyncio
async def test_continue_patches_effort_and_checked_feed() -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_goal_line(
        "↻ Continuing toward goal (3/20): still drafting replies",
        chat_id=42,
        assistant_id=7,
    )

    api.patch_mission_progress.assert_awaited_once_with(
        assistant_id=7,
        mission_id="mission-1",
        effort={"used": 3, "budget": 20, "unit": "turns"},
        feed_entry={
            "kind": "checked",
            "text": "Checking my work: still drafting replies",
        },
    )
    await lane.close()


@pytest.mark.asyncio
async def test_continue_without_counts_omits_effort() -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set: Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_goal_line(
        "↻ Continuing toward goal: still drafting replies",
        chat_id=42,
        assistant_id=7,
    )

    kwargs = api.patch_mission_progress.await_args.kwargs
    assert "effort" not in kwargs
    assert kwargs["feed_entry"] == {
        "kind": "checked",
        "text": "Checking my work: still drafting replies",
    }
    await lane.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "method", "expected_kwargs"),
    [
        (
            "✓ Goal achieved: replies sent successfully",
            "complete_mission",
            {"summary": "replies sent successfully"},
        ),
        (
            "⏸ Goal paused: waiting for approval",
            "pause_mission",
            {"reason": "waiting for approval"},
        ),
        (
            "⏳ Goal parked: dependency is still running",
            "pause_mission",
            {"reason": "Waiting: dependency is still running"},
        ),
    ],
)
async def test_lifecycle_lines_call_mission_api(
    line: str,
    method: str,
    expected_kwargs: dict,
) -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_goal_line(line, chat_id=42, assistant_id=7)

    endpoint = getattr(api, method)
    endpoint.assert_awaited_once_with(
        assistant_id=7,
        mission_id="mission-1",
        **expected_kwargs,
    )
    await lane.close()


@pytest.mark.asyncio
async def test_cleared_flushes_abandons_and_drops_binding() -> None:
    api = _api()
    events: list[str] = []
    api.patch_mission_progress.side_effect = (
        lambda **_: events.append("progress") or {
            "ok": True,
            "mission": _snapshot(),
        }
    )
    api.abandon_mission.side_effect = (
        lambda **_: events.append("abandon") or {
            "ok": True,
            "mission": _snapshot(status="abandoned"),
        }
    )
    lane = MissionLane(api, progress_throttle_seconds=1.0)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): first check",
        chat_id=42,
        assistant_id=7,
    )
    events.clear()
    api.patch_mission_progress.reset_mock()
    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): pending check",
        chat_id=42,
        assistant_id=7,
    )

    handled = await lane.handle_goal_line(
        "✓ Goal cleared.",
        chat_id=42,
        assistant_id=7,
    )

    assert handled is True
    assert events == ["progress", "abandon"]
    api.abandon_mission.assert_awaited_once_with(
        assistant_id=7,
        mission_id="mission-1",
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (3/20): stale check",
        chat_id=42,
        assistant_id=7,
    )
    assert api.patch_mission_progress.await_count == 1
    await lane.close()


@pytest.mark.asyncio
async def test_cleared_without_binding_is_noop() -> None:
    api = _api()
    lane = MissionLane(api)

    handled = await lane.handle_goal_line(
        "✓ Goal cleared.",
        chat_id=42,
        assistant_id=7,
    )

    assert handled is True
    api.abandon_mission.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_continue_after_pause_resumes_before_progress() -> None:
    api = _api()
    events: list[str] = []
    api.resume_mission.side_effect = lambda **_: events.append("resume") or {
        "ok": True,
        "mission": _snapshot(),
    }
    api.patch_mission_progress.side_effect = (
        lambda **_: events.append("progress") or {
            "ok": True,
            "mission": _snapshot(),
        }
    )
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "⏸ Goal paused: waiting for approval",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_goal_line(
        "↻ Continuing toward goal (4/20): approval arrived",
        chat_id=42,
        assistant_id=7,
    )

    assert events == ["resume", "progress"]
    await lane.close()


@pytest.mark.asyncio
async def test_progress_throttle_latest_wins_with_one_deferred_flush() -> None:
    api = _api()
    lane = MissionLane(api, progress_throttle_seconds=0.04)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): first check",
        chat_id=42,
        assistant_id=7,
    )
    api.patch_mission_progress.reset_mock()

    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): older pending check",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (3/20): newest pending check",
        chat_id=42,
        assistant_id=7,
    )

    api.patch_mission_progress.assert_not_awaited()
    assert len(lane._pending_progress_tasks) == 1
    await asyncio.sleep(0.06)
    api.patch_mission_progress.assert_awaited_once()
    kwargs = api.patch_mission_progress.await_args.kwargs
    assert kwargs["effort"]["used"] == 3
    assert kwargs["feed_entry"]["text"].endswith("newest pending check")
    await lane.close()


@pytest.mark.asyncio
async def test_lifecycle_transition_flushes_pending_progress_first() -> None:
    api = _api()
    events: list[str] = []
    api.patch_mission_progress.side_effect = (
        lambda **_: events.append("progress") or {
            "ok": True,
            "mission": _snapshot(),
        }
    )
    api.complete_mission.side_effect = lambda **_: events.append("complete") or {
        "ok": True,
        "mission": _snapshot(status="completed"),
    }
    lane = MissionLane(api, progress_throttle_seconds=1.0)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): initial check",
        chat_id=42,
        assistant_id=7,
    )
    events.clear()
    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): pending check",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_goal_line(
        "✓ Goal achieved: finished after the pending check",
        chat_id=42,
        assistant_id=7,
    )

    assert events == ["progress", "complete"]
    assert api.patch_mission_progress.await_args.kwargs["effort"]["used"] == 2
    await lane.close()


@pytest.mark.asyncio
async def test_rest_failures_are_swallowed() -> None:
    api = _api()
    api.get_active_mission.side_effect = RuntimeError("backend unavailable")
    lane = MissionLane(api)

    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    api.create_mission.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_failed_new_set_drops_previous_goal_binding() -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    api.get_active_mission.side_effect = RuntimeError("backend unavailable")

    await lane.handle_goal_line(
        "⊙ Goal set (10-turn budget): Prepare the launch notes",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/10): checking launch notes",
        chat_id=42,
        assistant_id=7,
    )

    api.patch_mission_progress.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_overlapping_sets_keep_the_newest_mission_binding() -> None:
    api = _api()
    first_create_started = asyncio.Event()
    release_first_create = asyncio.Event()

    async def create_mission(**kwargs) -> dict:
        if kwargs["title"] == "First goal":
            first_create_started.set()
            await release_first_create.wait()
            mission = _snapshot("mission-first")
        else:
            mission = _snapshot("mission-second")
        return {"ok": True, "mission": mission}

    api.create_mission.side_effect = create_mission
    lane = MissionLane(api)
    first_task = asyncio.create_task(lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): First goal",
        chat_id=42,
        assistant_id=7,
    ))
    await first_create_started.wait()
    second_task = asyncio.create_task(lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Second goal",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.sleep(0)
    release_first_create.set()
    await asyncio.gather(first_task, second_task)

    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): checking newest goal",
        chat_id=42,
        assistant_id=7,
    )

    assert api.patch_mission_progress.await_args.kwargs["mission_id"] == (
        "mission-second"
    )
    await lane.close()


@pytest.mark.asyncio
async def test_patch_404_clears_stale_id_and_next_set_recreates() -> None:
    api = _api()
    api.create_mission.side_effect = [
        {"ok": True, "mission": _snapshot("mission-stale")},
        {"ok": True, "mission": _snapshot("mission-new")},
    ]
    api.patch_mission_progress.side_effect = BgosApiError(
        404,
        "MISSION_NOT_FOUND",
        {"error": "MISSION_NOT_FOUND"},
    )
    lane = MissionLane(api)
    set_line = "⊙ Goal set (20-turn budget): Draft the customer replies"
    await lane.handle_goal_line(set_line, chat_id=42, assistant_id=7)

    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): checking",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(set_line, chat_id=42, assistant_id=7)

    assert api.create_mission.await_count == 2
    assert api.create_mission.await_args_list == [
        call(
            assistant_id=7,
            title="Draft the customer replies",
            origin="derived",
            effort={"used": 0, "budget": 20, "unit": "turns"},
            first_feed_text="Goal set",
        ),
        call(
            assistant_id=7,
            title="Draft the customer replies",
            origin="derived",
            effort={"used": 0, "budget": 20, "unit": "turns"},
            first_feed_text="Goal set",
        ),
    ]
    await lane.close()


class _GoalState:
    def __init__(
        self,
        status: str,
        *,
        waiting_on_pid: int | None = None,
        waiting_on_session: str | None = None,
        waiting_until: float = 0.0,
    ) -> None:
        self.status = status
        self.waiting_on_pid = waiting_on_pid
        self.waiting_on_session = waiting_on_session
        self.waiting_until = waiting_until


@pytest.mark.asyncio
async def test_control_events_enforce_real_goal_state(monkeypatch) -> None:
    api = _api()
    states = iter(["active", "paused", "active"])
    managers: list[Mock] = []

    def goal_manager_factory(session_id: str) -> Mock:
        manager = Mock(state=_GoalState(next(states)))
        managers.append(manager)
        assert session_id == "session-abc"
        return manager

    monkeypatch.setattr(
        missions_bridge,
        "_load_goal_manager",
        lambda: goal_manager_factory,
    )
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })
    await lane.handle_mission_event({
        "event_type": "mission_resumed",
        "mission": _snapshot(status="active"),
    })
    await lane.handle_mission_event({
        "event_type": "mission_completed",
        "mission": _snapshot(status="completed"),
    })

    managers[0].pause.assert_called_once_with()
    managers[1].resume.assert_called_once_with()
    managers[2].mark_done.assert_called_once_with("Marked done from the app")
    await lane.close()


@pytest.mark.asyncio
async def test_resume_control_clears_parked_barrier_while_goal_is_active() -> None:
    manager = Mock(state=_GoalState("active", waiting_on_pid=99))
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_resumed",
        "mission": _snapshot(status="active"),
    })

    manager.resume.assert_called_once_with()
    await lane.close()


@pytest.mark.asyncio
async def test_duplicate_resume_control_does_not_reset_active_goal() -> None:
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_resumed",
        "mission": _snapshot(status="active"),
    })

    manager.resume.assert_not_called()
    await lane.close()


@pytest.mark.asyncio
async def test_control_event_for_unknown_mission_is_ignored(monkeypatch) -> None:
    factory = Mock()
    monkeypatch.setattr(missions_bridge, "_load_goal_manager", factory)
    resolver = Mock(return_value=None)
    lane = MissionLane(_api(), session_id_resolver=resolver)

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot("unknown-mission", status="paused"),
    })

    resolver.assert_not_called()
    factory.assert_not_called()
    await lane.close()


@pytest.mark.asyncio
async def test_control_echo_is_ignored_when_goal_already_matches(monkeypatch) -> None:
    manager = Mock(state=_GoalState("paused"))
    monkeypatch.setattr(
        missions_bridge,
        "_load_goal_manager",
        lambda: Mock(return_value=manager),
    )
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })

    manager.pause.assert_not_called()
    await lane.close()


@pytest.mark.asyncio
async def test_recent_outbound_transition_echo_skips_goal_manager(
    monkeypatch,
) -> None:
    factory = Mock()
    monkeypatch.setattr(missions_bridge, "_load_goal_manager", factory)
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "⏳ Goal parked: waiting for tests",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })

    factory.assert_not_called()
    await lane.close()


@pytest.mark.asyncio
async def test_missing_echo_does_not_swallow_later_user_pause() -> None:
    resumed_manager = Mock(state=_GoalState("paused"))
    paused_manager = Mock(state=_GoalState("active"))
    factory = Mock(side_effect=[resumed_manager, paused_manager])
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=factory,
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "⏸ Goal paused: waiting for input",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_resumed",
        "mission": _snapshot(status="active"),
    })
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })

    resumed_manager.resume.assert_called_once_with()
    paused_manager.pause.assert_called_once_with()
    await lane.close()


@pytest.mark.asyncio
async def test_known_control_updates_lane_when_session_is_unavailable() -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })
    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): input arrived",
        chat_id=42,
        assistant_id=7,
    )

    api.resume_mission.assert_awaited_once_with(
        assistant_id=7,
        mission_id="mission-1",
    )
    await lane.close()


@pytest.mark.asyncio
async def test_resume_404_clears_binding_without_stale_progress() -> None:
    api = _api()
    api.resume_mission.side_effect = BgosApiError(
        404,
        "MISSION_NOT_FOUND",
        {"error": "MISSION_NOT_FOUND"},
    )
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "⏸ Goal paused: waiting for input",
        chat_id=42,
        assistant_id=7,
    )
    api.patch_mission_progress.reset_mock()

    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): input arrived",
        chat_id=42,
        assistant_id=7,
    )

    api.patch_mission_progress.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_progress_404_during_flush_skips_stale_complete() -> None:
    api = _api()
    lane = MissionLane(api, progress_throttle_seconds=1.0)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): initial check",
        chat_id=42,
        assistant_id=7,
    )
    api.patch_mission_progress.side_effect = BgosApiError(
        404,
        "MISSION_NOT_FOUND",
        {"error": "MISSION_NOT_FOUND"},
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): pending check",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_goal_line(
        "✓ Goal achieved: complete",
        chat_id=42,
        assistant_id=7,
    )

    api.complete_mission.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_terminal_ws_event_cancels_deferred_progress() -> None:
    api = _api()
    lane = MissionLane(api, progress_throttle_seconds=0.04)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): initial check",
        chat_id=42,
        assistant_id=7,
    )
    api.patch_mission_progress.reset_mock()
    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): pending check",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_completed",
        "mission": _snapshot(status="completed"),
    })
    await asyncio.sleep(0.06)

    api.patch_mission_progress.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_continue_is_ignored_for_terminal_mission() -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_mission_event({
        "event_type": "mission_failed",
        "mission": _snapshot(status="failed"),
    })

    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): stale continuation",
        chat_id=42,
        assistant_id=7,
    )

    api.patch_mission_progress.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_waiting_continue_rechecks_status_after_terminal_event() -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    mission_lock = lane._progress_locks.setdefault(
        "mission-1",
        asyncio.Lock(),
    )
    await mission_lock.acquire()
    continue_task = asyncio.create_task(lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): stale continuation",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.sleep(0)
    terminal_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_failed",
        "mission": _snapshot(status="failed"),
    }))
    await asyncio.sleep(0)

    mission_lock.release()
    await asyncio.gather(continue_task, terminal_task)

    api.patch_mission_progress.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_waiting_continue_rechecks_status_after_pause_event() -> None:
    api = _api()
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    mission_lock = lane._progress_locks.setdefault(
        "mission-1",
        asyncio.Lock(),
    )
    await mission_lock.acquire()
    continue_task = asyncio.create_task(lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): stale continuation",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.sleep(0)
    pause_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    }))
    await asyncio.sleep(0)

    mission_lock.release()
    await asyncio.gather(continue_task, pause_task)

    api.patch_mission_progress.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_late_resume_response_does_not_override_newer_app_pause() -> None:
    api = _api()
    resume_started = asyncio.Event()
    release_resume = asyncio.Event()

    async def delayed_resume(**kwargs) -> dict:
        resume_started.set()
        await release_resume.wait()
        return {"ok": True, "mission": _snapshot(status="active")}

    api.resume_mission.side_effect = delayed_resume
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "⏸ Goal paused: waiting for input",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })
    api.patch_mission_progress.reset_mock()

    continue_task = asyncio.create_task(lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): input arrived",
        chat_id=42,
        assistant_id=7,
    ))
    await resume_started.wait()
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })
    release_resume.set()
    await continue_task

    manager.pause.assert_called_once_with()
    api.patch_mission_progress.assert_not_awaited()
    await lane.close()


@pytest.mark.asyncio
async def test_old_pause_echo_does_not_invalidate_inflight_resume() -> None:
    api = _api()
    resume_started = asyncio.Event()
    release_resume = asyncio.Event()

    async def delayed_resume(**kwargs) -> dict:
        resume_started.set()
        await release_resume.wait()
        return {"ok": True, "mission": _snapshot(status="active")}

    api.resume_mission.side_effect = delayed_resume
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "⏸ Goal paused: waiting for input",
        chat_id=42,
        assistant_id=7,
    )

    continue_task = asyncio.create_task(lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): input arrived",
        chat_id=42,
        assistant_id=7,
    ))
    await resume_started.wait()
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })
    release_resume.set()
    await continue_task

    api.patch_mission_progress.assert_awaited_once()
    assert api.patch_mission_progress.await_args.kwargs["effort"]["used"] == 2
    await lane.close()


@pytest.mark.asyncio
async def test_control_events_enforce_goal_state_in_websocket_order() -> None:
    api = _api()
    first_resolution_started = asyncio.Event()
    release_first_resolution = asyncio.Event()
    resolution_count = 0

    async def resolve_session(chat_id: int, assistant_id: int) -> str:
        nonlocal resolution_count
        resolution_count += 1
        if resolution_count == 1:
            first_resolution_started.set()
            await release_first_resolution.wait()
        return "session-abc"

    actions: list[str] = []
    state = _GoalState("active")
    manager = Mock(state=state)

    def pause() -> None:
        actions.append("pause")
        state.status = "paused"

    def resume() -> None:
        actions.append("resume")
        state.status = "active"

    manager.pause.side_effect = pause
    manager.resume.side_effect = resume
    lane = MissionLane(
        api,
        session_id_resolver=resolve_session,
        goal_manager_factory=Mock(return_value=manager),
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    pause_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    }))
    await first_resolution_started.wait()
    resume_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_resumed",
        "mission": _snapshot(status="active"),
    }))
    await asyncio.sleep(0)
    release_first_resolution.set()
    await asyncio.gather(pause_task, resume_task)

    assert actions == ["pause", "resume"]
    await lane.close()


@pytest.mark.asyncio
async def test_routine_tick_does_not_override_inflight_pause_response() -> None:
    api = _api()
    pause_started = asyncio.Event()
    release_pause = asyncio.Event()

    async def delayed_pause(**kwargs) -> dict:
        pause_started.set()
        await release_pause.wait()
        return {"ok": True, "mission": _snapshot(status="paused")}

    api.pause_mission.side_effect = delayed_pause
    lane = MissionLane(api)
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    pause_task = asyncio.create_task(lane.handle_goal_line(
        "⏸ Goal paused: waiting for input",
        chat_id=42,
        assistant_id=7,
    ))
    await pause_started.wait()
    await lane.handle_mission_event({
        "event_type": "mission_ticked",
        "mission": _snapshot(status="active"),
    })
    release_pause.set()
    await pause_task

    await lane.handle_goal_line(
        "↻ Continuing toward goal (2/20): input arrived",
        chat_id=42,
        assistant_id=7,
    )

    api.resume_mission.assert_awaited_once_with(
        assistant_id=7,
        mission_id="mission-1",
    )
    await lane.close()


@pytest.mark.asyncio
async def test_control_supports_plain_mock_goal_manager() -> None:
    manager = Mock()
    factory = Mock(return_value=manager)
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=factory,
    )
    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    })

    factory.assert_called_once_with("session-abc")
    manager.pause.assert_called_once_with()
    await lane.close()


@pytest.mark.asyncio
async def test_session_resolution_survives_newer_addressed_assistant() -> None:
    adapter = BGOSAdapter(BgosConfig(
        base_url="https://bgos.test",
        pairing_token="pair_xyz",
    ))
    adapter._state.addressed_assistant_id_by_chat[42] = 99
    source = object()
    adapter.build_source = Mock(return_value=source)
    session_store = SimpleNamespace(
        peek_session_id=AsyncMock(return_value="session-abc"),
    )
    adapter.gateway_runner = SimpleNamespace(
        _session_key_for_source=Mock(return_value="bgos:42"),
        async_session_store=session_store,
    )

    try:
        session_id = await adapter._mission_session_id_for_chat(42, 7)

        assert session_id == "session-abc"
        session_store.peek_session_id.assert_awaited_once_with("bgos:42")
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_send_goal_line_remains_visible_and_triggers_lane(
) -> None:
    adapter = BGOSAdapter(BgosConfig(
        base_url="https://bgos.test",
        pairing_token="pair_xyz",
    ))
    adapter._state.record_inbound_chat(42)
    adapter._state.addressed_assistant_id_by_chat[42] = 7
    lane = SimpleNamespace(
        handle_goal_line=AsyncMock(),
        handle_mission_event=AsyncMock(),
        close=AsyncMock(),
    )
    adapter._mission_lane = lane
    adapter._assistant_id_for_chat = AsyncMock(return_value=7)
    adapter._api.post_send_message = AsyncMock(
        return_value={"message": {"id": 801}},
    )
    line = "✓ Goal achieved: all replies are drafted"

    try:
        await adapter.send(42, line)

        lane.handle_goal_line.assert_awaited_once_with(
            line,
            chat_id=42,
            assistant_id=7,
        )
        request = adapter._api.post_send_message.await_args.kwargs
        assert request["text"] == line
        assert request["message_type"] == "standard"
        assert adapter._api.post_send_message.await_count == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_send_non_goal_text_does_not_touch_lane() -> None:
    adapter = BGOSAdapter(BgosConfig(
        base_url="https://bgos.test",
        pairing_token="pair_xyz",
    ))
    adapter._state.record_inbound_chat(42)
    adapter._state.addressed_assistant_id_by_chat[42] = 7
    lane = SimpleNamespace(
        handle_goal_line=AsyncMock(),
        handle_mission_event=AsyncMock(),
        close=AsyncMock(),
    )
    adapter._mission_lane = lane
    adapter._assistant_id_for_chat = AsyncMock(return_value=7)
    adapter._api.post_send_message = AsyncMock(
        return_value={"message": {"id": 802}},
    )

    try:
        await adapter.send(42, "A regular answer")

        lane.handle_goal_line.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_websocket_dispatches_mission_events() -> None:
    received: list[dict] = []
    ws = BgosWs(
        BgosConfig(
            base_url="https://bgos.test",
            pairing_token="pair_xyz",
        ),
        on_inbound_message=lambda data: None,
        on_callback_result=lambda data: None,
        on_mission_event=lambda data: received.append(data),
    )
    event = {
        "event_type": "mission_paused",
        "mission": _snapshot(status="paused"),
    }

    handler = ws._sio.handlers["/"]["mission_paused"]
    await handler(event)

    assert received == [event]
