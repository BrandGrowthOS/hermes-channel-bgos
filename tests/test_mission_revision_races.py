"""Race regressions for mission revisions and atomic adoption."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_channel_bgos.missions_bridge import MissionLane


TITLE = "Draft the customer replies"


def _snapshot(
    mission_id: str = "mission-1",
    *,
    status: str = "active",
    updated_at: str = "2026-07-19T00:00:01.000Z",
    created_at: float | None = None,
) -> dict:
    snapshot = {
        "id": mission_id,
        "assistantId": 7,
        "status": status,
        "origin": "derived",
        "title": TITLE,
        "updatedAt": updated_at,
    }
    if created_at is not None:
        snapshot["createdAt"] = created_at
    return snapshot


def _api() -> SimpleNamespace:
    return SimpleNamespace(
        get_active_mission=AsyncMock(return_value={"mission": None}),
        create_mission=AsyncMock(
            return_value={"ok": True, "mission": _snapshot()},
        ),
        patch_mission_progress=AsyncMock(
            return_value={
                "ok": True,
                "mission": _snapshot(
                    updated_at="2026-07-19T00:00:02.000Z",
                ),
            },
        ),
        pause_mission=AsyncMock(
            return_value={
                "ok": True,
                "mission": _snapshot(
                    status="paused",
                    updated_at="2026-07-19T00:00:02.000Z",
                ),
            },
        ),
        resume_mission=AsyncMock(),
        complete_mission=AsyncMock(),
        fail_mission=AsyncMock(),
        abandon_mission=AsyncMock(),
    )


def _manager() -> tuple[Mock, SimpleNamespace]:
    state = SimpleNamespace(
        status="active",
        goal=TITLE,
        created_at=1.0,
        paused_reason=None,
        waiting_on_pid=None,
        waiting_on_session=None,
        waiting_until=0.0,
    )
    manager = Mock(state=state)

    def pause(*, reason: str) -> None:
        state.status = "paused"
        state.paused_reason = reason

    manager.pause.side_effect = pause
    return manager, state


def _lane(
    api: SimpleNamespace,
    tmp_path: Path,
    manager: Mock,
) -> MissionLane:
    return MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )


async def _set_goal(lane: MissionLane) -> None:
    await lane.handle_goal_line(
        f"⊙ Goal set (20-turn budget): {TITLE}",
        chat_id=42,
        assistant_id=7,
    )


@pytest.mark.asyncio
async def test_equal_updated_at_self_echo_is_suppressed(tmp_path: Path) -> None:
    api = _api()
    manager, state = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    state.status = "paused"
    state.paused_reason = "paused by user"
    factory = lane._goal_manager_factory
    assert isinstance(factory, Mock)
    factory.reset_mock()

    await lane.handle_goal_line(
        "⏸ Goal paused: waiting for tests",
        chat_id=42,
        assistant_id=7,
    )
    factory.assert_called_once_with("session-abc")
    factory.reset_mock()
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    factory.assert_not_called()
    manager.pause.assert_not_called()
    await lane.close()


@pytest.mark.asyncio
async def test_newer_pause_racing_daemon_pause_is_enforced(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()

    async def delayed_pause(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {
            "ok": True,
            "mission": _snapshot(
                status="paused",
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        }

    api.pause_mission.side_effect = delayed_pause
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    manager.pause.reset_mock()

    patch_task = asyncio.create_task(lane.handle_goal_line(
        "⏸ Goal paused: waiting for tests",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    event_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    }))
    await asyncio.sleep(0)
    assert not event_task.done()

    release_patch.set()
    await asyncio.wait_for(
        asyncio.gather(patch_task, event_task),
        1.0,
    )

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    binding = lane._bindings[(42, 7)]
    assert binding.updated_at == "2026-07-19T00:00:03.000Z"
    await lane.close()


@pytest.mark.asyncio
async def test_equal_revision_app_pause_beating_daemon_pause_is_enforced(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()
    app_pause = _snapshot(
        status="paused",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    app_pause["pausedReason"] = None

    async def idempotent_pause(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {"ok": True, "mission": app_pause}

    api.pause_mission.side_effect = idempotent_pause
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    manager.pause.reset_mock()

    patch_task = asyncio.create_task(lane.handle_goal_line(
        "\u23f8 Goal paused: waiting for tests",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    event_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": app_pause,
    }))
    await asyncio.sleep(0)
    assert not event_task.done()

    release_patch.set()
    await asyncio.wait_for(
        asyncio.gather(patch_task, event_task),
        1.0,
    )

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    binding = lane._bindings[(42, 7)]
    assert binding.status == "paused"
    assert binding.updated_at == "2026-07-19T00:00:02.000Z"
    await lane.close()


@pytest.mark.asyncio
async def test_equal_revision_daemon_pause_response_is_enforced(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()
    daemon_pause = _snapshot(
        status="paused",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    daemon_pause["pausedReason"] = "Waiting: child process"

    async def idempotent_pause(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {"ok": True, "mission": daemon_pause}

    api.pause_mission.side_effect = idempotent_pause
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    manager.pause.reset_mock()

    patch_task = asyncio.create_task(lane.handle_goal_line(
        "\u23f3 Goal parked: child process",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    event_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": daemon_pause,
    }))
    await asyncio.sleep(0)
    assert not event_task.done()

    release_patch.set()
    await asyncio.wait_for(
        asyncio.gather(patch_task, event_task),
        1.0,
    )

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    binding = lane._bindings[(42, 7)]
    assert binding.status == "paused"
    assert binding.updated_at == "2026-07-19T00:00:02.000Z"
    await lane.close()


@pytest.mark.asyncio
async def test_patch_owner_cancellation_cleans_token_and_propagates(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()

    async def blocked_pause(**kwargs) -> dict:
        patch_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    api.pause_mission.side_effect = blocked_pause
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    manager.pause.reset_mock()

    patch_task = asyncio.create_task(lane.handle_goal_line(
        "⏸ Goal paused: waiting for tests",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    patch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await patch_task

    assert "mission-1" not in lane._inflight_patches
    assert lane._bindings[(42, 7)].self_patch_updated_at is None

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    })
    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    await lane.close()


@pytest.mark.asyncio
async def test_ws_waiter_cancellation_cannot_hang_patch(tmp_path: Path) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()

    async def delayed_pause(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {
            "ok": True,
            "mission": _snapshot(
                status="paused",
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        }

    api.pause_mission.side_effect = delayed_pause
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)

    patch_task = asyncio.create_task(lane.handle_goal_line(
        "⏸ Goal paused: waiting for tests",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    waiter_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    }))
    await asyncio.sleep(0)
    assert not waiter_task.done()

    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    release_patch.set()
    await asyncio.wait_for(patch_task, 1.0)

    assert "mission-1" not in lane._inflight_patches
    await lane.close()


@pytest.mark.asyncio
async def test_progress_patch_and_newer_pause_do_not_deadlock(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()

    async def delayed_progress(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {
            "ok": True,
            "mission": _snapshot(
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        }

    api.patch_mission_progress.side_effect = delayed_progress
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    manager.pause.reset_mock()

    progress_task = asyncio.create_task(lane.handle_goal_line(
        "↻ Continuing toward goal (1/20): checking",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    pause_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    }))
    await asyncio.sleep(0)
    assert not pause_task.done()

    release_patch.set()
    await asyncio.wait_for(
        asyncio.gather(progress_task, pause_task),
        1.0,
    )

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert lane._bindings[(42, 7)].status == "paused"
    await lane.close()


@pytest.mark.asyncio
async def test_progress_snapshot_enforces_pause_when_older_event_is_suppressed(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()

    async def delayed_progress(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {
            "ok": True,
            "mission": _snapshot(
                status="paused",
                updated_at="2026-07-19T00:00:03.000Z",
            ),
        }

    api.patch_mission_progress.side_effect = delayed_progress
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    manager.pause.reset_mock()

    progress_task = asyncio.create_task(lane.handle_goal_line(
        "\u21bb Continuing toward goal (1/20): checking",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    pause_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    }))
    await asyncio.sleep(0)
    assert not pause_task.done()

    release_patch.set()
    await asyncio.wait_for(
        asyncio.gather(progress_task, pause_task),
        1.0,
    )

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    binding = lane._bindings[(42, 7)]
    assert binding.status == "paused"
    assert binding.updated_at == "2026-07-19T00:00:03.000Z"
    assert binding.self_patch_updated_at == "2026-07-19T00:00:03.000Z"
    await lane.close()


@pytest.mark.asyncio
async def test_reconcile_waits_for_pause_patch_before_using_snapshot(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()

    async def delayed_pause(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {
            "ok": True,
            "mission": _snapshot(
                status="paused",
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        }

    api.pause_mission.side_effect = delayed_pause
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    api.get_active_mission.return_value = {
        "mission": _snapshot(
            status="active",
            updated_at="2026-07-19T00:00:01.000Z",
        ),
    }
    api.get_active_mission.reset_mock()
    manager.pause.reset_mock()

    pause_task = asyncio.create_task(lane.handle_goal_line(
        "\u23f8 Goal paused: waiting for tests",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    reconcile_task = asyncio.create_task(lane.reconcile())
    await asyncio.sleep(0)
    api.get_active_mission.assert_not_awaited()

    release_patch.set()
    await asyncio.wait_for(
        asyncio.gather(pause_task, reconcile_task),
        1.0,
    )

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    manager.resume.assert_not_called()
    binding = lane._bindings[(42, 7)]
    assert binding.status == "paused"
    assert binding.updated_at == "2026-07-19T00:00:02.000Z"
    await lane.close()


@pytest.mark.asyncio
async def test_reconcile_cannot_resume_goal_during_complete_patch(
    tmp_path: Path,
) -> None:
    api = _api()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()

    async def delayed_complete(**kwargs) -> dict:
        patch_started.set()
        await release_patch.wait()
        return {
            "ok": True,
            "mission": _snapshot(
                status="completed",
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        }

    api.complete_mission.side_effect = delayed_complete
    manager, state = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    state.status = "done"
    api.get_active_mission.return_value = {
        "mission": _snapshot(
            status="active",
            updated_at="2026-07-19T00:00:01.000Z",
        ),
    }
    api.get_active_mission.reset_mock()

    complete_task = asyncio.create_task(lane.handle_goal_line(
        "\u2713 Goal achieved: all checks pass",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(patch_started.wait(), 1.0)
    reconcile_task = asyncio.create_task(lane.reconcile())
    await asyncio.sleep(0)
    api.get_active_mission.assert_not_awaited()

    release_patch.set()
    await asyncio.wait_for(
        asyncio.gather(complete_task, reconcile_task),
        1.0,
    )

    manager.resume.assert_not_called()
    assert lane._bindings == {}
    await lane.close()


@pytest.mark.asyncio
async def test_resume_patch_invalidates_pause_waiting_on_progress_cancel(
    tmp_path: Path,
) -> None:
    api = _api()
    api.resume_mission.return_value = {
        "ok": True,
        "mission": _snapshot(
            status="active",
            updated_at="2026-07-19T00:00:04.000Z",
        ),
    }
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    async def delayed_cancel(mission_id: str) -> None:
        cancel_started.set()
        await release_cancel.wait()

    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    await _set_goal(lane)
    lane._cancel_progress = delayed_cancel
    manager.pause.reset_mock()

    pause_task = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    }))
    await asyncio.wait_for(cancel_started.wait(), 1.0)

    await lane.handle_goal_line(
        f"\u25b6 Goal resumed: {TITLE}",
        chat_id=42,
        assistant_id=7,
    )
    release_cancel.set()
    await asyncio.wait_for(pause_task, 1.0)

    manager.pause.assert_not_called()
    assert lane._bindings[(42, 7)].status == "active"
    await lane.close()


@pytest.mark.asyncio
async def test_old_mapping_is_enforceable_during_delayed_adoption_get(
    tmp_path: Path,
) -> None:
    api = _api()
    get_started = asyncio.Event()
    release_get = asyncio.Event()

    async def delayed_get(**kwargs) -> dict:
        get_started.set()
        await release_get.wait()
        return {
            "mission": _snapshot(
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        }

    api.get_active_mission.side_effect = delayed_get
    manager, state = _manager()
    lane = _lane(api, tmp_path, manager)
    lane._store_binding(
        _snapshot(updated_at="2026-07-19T00:00:01.000Z"),
        chat_id=42,
        assistant_id=7,
        session_id="session-abc",
        goal_text=TITLE,
        goal_created_at=1.0,
    )

    adoption_task = asyncio.create_task(_set_goal(lane))
    await asyncio.wait_for(get_started.wait(), 1.0)
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    })

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert state.status == "paused"
    assert not adoption_task.done()

    release_get.set()
    await asyncio.wait_for(adoption_task, 1.0)

    api.create_mission.assert_not_awaited()
    assert lane._bindings[(42, 7)].status == "paused"
    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    await lane.close()


@pytest.mark.asyncio
async def test_terminal_event_during_adoption_get_cannot_resurrect_mission(
    tmp_path: Path,
) -> None:
    api = _api()
    get_started = asyncio.Event()
    release_get = asyncio.Event()
    stale_active = _snapshot(
        updated_at="2026-07-19T00:00:01.000Z",
    )

    async def delayed_get(**kwargs) -> dict:
        get_started.set()
        await release_get.wait()
        return {"mission": stale_active}

    api.get_active_mission.side_effect = delayed_get
    api.create_mission.return_value = {
        "ok": True,
        "mission": _snapshot(
            "mission-new",
            updated_at="2026-07-19T00:00:04.000Z",
        ),
    }
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)
    lane._store_binding(
        stale_active,
        chat_id=42,
        assistant_id=7,
        session_id="session-abc",
        goal_text=TITLE,
        goal_created_at=1.0,
    )

    adoption_task = asyncio.create_task(_set_goal(lane))
    await asyncio.wait_for(get_started.wait(), 1.0)
    await lane.handle_mission_event({
        "event_type": "mission_completed",
        "mission": _snapshot(
            status="completed",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    })
    assert lane._bindings == {}

    release_get.set()
    await asyncio.wait_for(adoption_task, 1.0)

    api.create_mission.assert_awaited_once()
    assert lane._bindings[(42, 7)].mission_id == "mission-new"
    manager.mark_done.assert_called_once_with("Marked done from the app")
    await lane.close()


@pytest.mark.asyncio
async def test_created_then_pause_beats_stale_create_response(
    tmp_path: Path,
) -> None:
    api = _api()
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    async def delayed_create(**kwargs) -> dict:
        create_started.set()
        await release_create.wait()
        return {
            "ok": True,
            "mission": _snapshot(
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        }

    api.create_mission.side_effect = delayed_create
    manager, _ = _manager()
    lane = _lane(api, tmp_path, manager)

    create_task = asyncio.create_task(_set_goal(lane))
    await asyncio.wait_for(create_started.wait(), 1.0)
    await lane.handle_mission_event({
        "event_type": "mission_created",
        "mission": _snapshot(
            updated_at="2026-07-19T00:00:02.000Z",
            created_at=time.time(),
        ),
    })
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    })
    assert lane._bindings[(42, 7)].status == "paused"

    release_create.set()
    await asyncio.wait_for(create_task, 1.0)

    binding = lane._bindings[(42, 7)]
    assert binding.status == "paused"
    assert binding.updated_at == "2026-07-19T00:00:03.000Z"
    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert not lane._pending_creates
    await lane.close()
