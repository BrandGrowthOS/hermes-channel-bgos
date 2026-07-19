"""Focused tests for persisted mission control reconciliation."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
import hermes_channel_bgos.missions_bridge as missions_bridge_module
from hermes_channel_bgos.bgos_api import BgosApiError
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.missions_bridge import MissionLane


GOAL = "Draft the customer replies"


def _snapshot(
    mission_id: str = "mission-1",
    *,
    status: str = "active",
    assistant_id: int = 7,
    title: str = GOAL,
    updated_at: str = "2026-07-19T00:00:01.000Z",
    created_at: float | None = None,
) -> dict:
    snapshot = {
        "id": mission_id,
        "assistantId": assistant_id,
        "status": status,
        "origin": "derived",
        "title": title,
        "updatedAt": updated_at,
    }
    if created_at is not None:
        snapshot["createdAt"] = created_at
    return snapshot


def _response(snapshot: dict | None) -> dict:
    return {"ok": True, "mission": snapshot}


def _api(*, active: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_active_mission=AsyncMock(return_value=_response(active)),
        create_mission=AsyncMock(
            return_value=_response(_snapshot("mission-created")),
        ),
        patch_mission_progress=AsyncMock(),
        pause_mission=AsyncMock(),
        resume_mission=AsyncMock(),
        complete_mission=AsyncMock(),
        fail_mission=AsyncMock(),
        abandon_mission=AsyncMock(
            return_value=_response(_snapshot(
                status="abandoned",
                updated_at="2026-07-19T00:00:03.000Z",
            )),
        ),
    )


class _GoalState:
    def __init__(
        self,
        status: str,
        *,
        goal: str = GOAL,
        created_at: float = 1.0,
        paused_reason: str | None = None,
    ) -> None:
        self.status = status
        self.goal = goal
        self.created_at = created_at
        self.paused_reason = paused_reason
        self.waiting_on_pid = None
        self.waiting_on_session = None
        self.waiting_until = 0.0


def _store_binding(
    lane: MissionLane,
    *,
    snapshot: dict | None = None,
    chat_id: int = 42,
    session_id: str | None = "session-abc",
    goal: str = GOAL,
    created_at: float | None = 1.0,
):
    binding = lane._store_binding(
        snapshot or _snapshot(),
        chat_id=chat_id,
        assistant_id=7,
        session_id=session_id,
        goal_text=goal,
        goal_created_at=created_at,
    )
    assert binding is not None
    return binding


@pytest.mark.asyncio
async def test_default_binding_store_lives_under_hermes_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first_lane = MissionLane(_api())
    _store_binding(first_lane)
    await first_lane.close()

    store_path = tmp_path / "bgos_mission_bindings.json"
    assert store_path.is_file()
    second_lane = MissionLane(_api())
    assert second_lane._bindings[(42, 7)].mission_id == "mission-1"
    await second_lane.close()


@pytest.mark.asyncio
async def test_adapter_startup_reconciles_after_websocket_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class _FakeWs:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def bind_pairing(self, pairing_id: int) -> None:
            pass

        def bind_assistants(self, assistant_ids: list[int]) -> None:
            pass

        async def start(self) -> None:
            order.append("websocket")

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(bgos_adapter_module, "BgosWs", _FakeWs)
    adapter = BGOSAdapter(BgosConfig(
        base_url="https://bgos.test",
        pairing_token="pair_xyz",
    ))
    adapter._api.whoami = AsyncMock(return_value={
        "pairing_id": 1,
        "assistants": [],
    })
    adapter._api.close = AsyncMock()
    adapter._push_agent_catalog_safe = AsyncMock()
    adapter._run_backfill = AsyncMock()
    adapter._poll_loop = AsyncMock()
    adapter._heartbeat_loop = AsyncMock()
    lane = SimpleNamespace(
        handle_mission_event=AsyncMock(),
        reconcile=AsyncMock(side_effect=lambda: order.append("reconcile")),
        close=AsyncMock(),
    )
    adapter._mission_lane = lane

    try:
        await adapter.connect()

        assert order == ["websocket", "reconcile"]
        lane.reconcile.assert_awaited_once_with()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_adapter_reconnect_schedules_mission_reconciliation() -> None:
    adapter = BGOSAdapter(BgosConfig(
        base_url="https://bgos.test",
        pairing_token="pair_xyz",
    ))
    adapter._run_backfill = AsyncMock()
    schedule_reconcile = Mock()
    adapter._mission_lane = SimpleNamespace(
        schedule_reconcile=schedule_reconcile,
        close=AsyncMock(),
    )

    try:
        adapter._on_reconnect(81)
        await asyncio.sleep(0)

        schedule_reconcile.assert_called_once_with()
        adapter._run_backfill.assert_awaited_once_with(81)
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        ("mission_paused", "paused"),
        ("mission_completed", "completed"),
    ],
)
async def test_control_revalidates_binding_after_session_resolution(
    tmp_path: Path,
    event_type: str,
    status: str,
) -> None:
    resolution_started = asyncio.Event()
    release_resolution = asyncio.Event()

    async def resolve_session(chat_id: int, assistant_id: int) -> str:
        resolution_started.set()
        await release_resolution.wait()
        return "session-old"

    manager_factory = Mock()
    lane = MissionLane(
        _api(),
        session_id_resolver=resolve_session,
        goal_manager_factory=manager_factory,
        binding_store_path=tmp_path / "bindings.json",
    )
    _store_binding(lane, session_id="session-old")

    control = asyncio.create_task(lane.handle_mission_event({
        "event_type": event_type,
        "mission": _snapshot(
            status=status,
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    }))
    await resolution_started.wait()
    _store_binding(
        lane,
        snapshot=_snapshot(
            "mission-new",
            title="A newer goal",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
        session_id="session-new",
        goal="A newer goal",
        created_at=2.0,
    )
    release_resolution.set()
    await control

    manager_factory.assert_not_called()
    assert lane._bindings[(42, 7)].mission_id == "mission-new"
    await lane.close()


@pytest.mark.asyncio
async def test_incomplete_identity_is_hydrated_before_control_enforcement(
    tmp_path: Path,
) -> None:
    resolver = AsyncMock(return_value="session-abc")
    manager = Mock(state=_GoalState("active", created_at=2.0))
    lane = MissionLane(
        _api(),
        session_id_resolver=resolver,
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = lane._store_binding(
        _snapshot(),
        chat_id=42,
        assistant_id=7,
        session_id=None,
        goal_text=GOAL,
        goal_created_at=None,
    )
    assert binding is not None

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    resolver.assert_awaited_once_with(42, 7)
    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert binding.session_id == "session-abc"
    assert binding.goal_created_at == 2.0
    assert binding.pending_enforcement is False
    await lane.close()


@pytest.mark.asyncio
async def test_resolution_retries_then_reconcile_clears_pending_enforcement(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = AsyncMock(side_effect=[None, None])
    manager = Mock(state=_GoalState("active"))
    api = _api(active=_snapshot(
        status="paused",
        updated_at="2026-07-19T00:00:03.000Z",
    ))
    lane = MissionLane(
        api,
        session_id_resolver=resolver,
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)
    schedule_reconcile = Mock()
    lane.schedule_reconcile = schedule_reconcile

    with caplog.at_level(logging.ERROR):
        await lane.handle_mission_event({
            "event_type": "mission_paused",
            "mission": _snapshot(
                status="paused",
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        })

    assert resolver.await_count == 2
    assert binding.pending_enforcement is True
    schedule_reconcile.assert_called_once_with()
    assert "mission control enforcement pending" in caplog.text

    resolver.side_effect = None
    resolver.return_value = "session-abc"
    await lane.reconcile()

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert binding.pending_enforcement is False
    assert resolver.await_count == 3
    await lane.close()


@pytest.mark.asyncio
async def test_resolution_second_attempt_enforces_control(
    tmp_path: Path,
) -> None:
    resolver = AsyncMock(side_effect=[None, "session-abc"])
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        _api(),
        session_id_resolver=resolver,
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    assert resolver.await_count == 2
    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert binding.pending_enforcement is False
    await lane.close()


@pytest.mark.asyncio
async def test_restart_loads_binding_and_reconciles_server_pause(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_lane = MissionLane(_api(), binding_store_path=store_path)
    _store_binding(first_lane)
    await first_lane.close()

    manager = Mock(state=_GoalState("active"))
    second_lane = MissionLane(
        _api(active=_snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        )),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=store_path,
    )

    assert second_lane._bindings[(42, 7)].mission_id == "mission-1"
    await second_lane.reconcile()

    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert second_lane._bindings[(42, 7)].status == "paused"
    await second_lane.close()


@pytest.mark.asyncio
async def test_snapshot_update_and_terminal_drop_persist_automatically(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_manager = Mock(state=_GoalState("active"))
    first_lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=first_manager),
        binding_store_path=store_path,
    )
    _store_binding(first_lane)

    await first_lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })
    await first_lane.close()

    second_manager = Mock(state=_GoalState("active"))
    second_lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=second_manager),
        binding_store_path=store_path,
    )
    assert second_lane._bindings[(42, 7)].status == "paused"

    await second_lane.handle_mission_event({
        "event_type": "mission_completed",
        "mission": _snapshot(
            status="completed",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    })
    await second_lane.close()

    third_lane = MissionLane(_api(), binding_store_path=store_path)
    assert third_lane._bindings == {}
    await third_lane.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        ("mission_failed", "failed"),
        ("mission_abandoned", "abandoned"),
    ],
)
async def test_terminal_websocket_event_clears_goal_and_binding(
    tmp_path: Path,
    event_type: str,
    status: str,
) -> None:
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    _store_binding(lane)

    await lane.handle_mission_event({
        "event_type": event_type,
        "mission": _snapshot(
            status=status,
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    manager.clear.assert_called_once_with()
    assert lane._bindings == {}
    await lane.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paused_by_lane", "paused_reason", "resume_count"),
    [
        (True, "bgos-mission-paused", 1),
        (False, "paused by user", 0),
    ],
)
async def test_reconcile_resumes_only_lane_owned_pause(
    tmp_path: Path,
    paused_by_lane: bool,
    paused_reason: str,
    resume_count: int,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_lane = MissionLane(_api(), binding_store_path=store_path)
    binding = _store_binding(first_lane)
    binding.paused_by_lane = paused_by_lane
    first_lane._persist_bindings()
    await first_lane.close()

    manager = Mock(state=_GoalState(
        "paused",
        paused_reason=paused_reason,
    ))
    second_lane = MissionLane(
        _api(active=_snapshot(
            status="active",
            updated_at="2026-07-19T00:00:02.000Z",
        )),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=store_path,
    )

    await second_lane.reconcile()

    assert manager.resume.call_count == resume_count
    if resume_count:
        manager.resume.assert_called_once_with(reset_budget=False)
    await second_lane.close()


@pytest.mark.asyncio
async def test_reconcile_finalizes_persisted_completed_binding(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_lane = MissionLane(_api(), binding_store_path=store_path)
    _store_binding(first_lane, snapshot=_snapshot(status="completed"))
    await first_lane.close()

    manager = Mock(state=_GoalState("active"))
    second_lane = MissionLane(
        _api(active=None),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=store_path,
    )

    await second_lane.reconcile()

    manager.mark_done.assert_called_once_with("Marked done from the app")
    assert second_lane._bindings == {}
    await second_lane.close()


@pytest.mark.asyncio
async def test_reconcile_clears_persisted_failed_binding(
    tmp_path: Path,
) -> None:
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        _api(active=None),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    _store_binding(lane, snapshot=_snapshot(status="failed"))

    await lane.reconcile()

    manager.clear.assert_called_once_with()
    assert lane._bindings == {}
    await lane.close()


@pytest.mark.asyncio
async def test_reconcile_revives_done_goal_for_open_server_mission(
    tmp_path: Path,
) -> None:
    manager = Mock(state=_GoalState("done"))
    lane = MissionLane(
        _api(active=_snapshot(
            status="active",
            updated_at="2026-07-19T00:00:02.000Z",
        )),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    _store_binding(lane)

    await lane.reconcile()

    manager.resume.assert_called_once_with(reset_budget=False)
    await lane.close()


@pytest.mark.asyncio
async def test_reconcile_rejects_get_older_than_self_patch(
    tmp_path: Path,
) -> None:
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        _api(active=_snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        )),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)
    binding.self_patch_updated_at = "2026-07-19T00:00:03.000Z"
    lane._persist_bindings()

    await lane.reconcile()

    assert binding.status == "active"
    manager.pause.assert_not_called()
    await lane.close()


@pytest.mark.asyncio
async def test_self_patch_revision_survives_restart_and_suppresses_echo(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_api = _api()
    first_api.pause_mission.return_value = _response(_snapshot(
        status="paused",
        updated_at="2026-07-19T00:00:02.000Z",
    ))
    first_lane = MissionLane(first_api, binding_store_path=store_path)
    _store_binding(first_lane)

    await first_lane.handle_goal_line(
        "\u23f8 Goal paused: Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await first_lane.close()

    manager_factory = Mock()
    second_lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=manager_factory,
        binding_store_path=store_path,
    )
    binding = second_lane._bindings[(42, 7)]
    assert binding.self_patch_updated_at == "2026-07-19T00:00:02.000Z"
    revision = binding.revision

    await second_lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    manager_factory.assert_not_called()
    assert binding.revision == revision
    await second_lane.close()


@pytest.mark.asyncio
async def test_stale_reconcile_get_cannot_override_newer_control(
    tmp_path: Path,
) -> None:
    get_started = asyncio.Event()
    release_get = asyncio.Event()

    async def delayed_get(**kwargs) -> dict:
        get_started.set()
        await release_get.wait()
        return _response(_snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ))

    api = _api()
    api.get_active_mission.side_effect = delayed_get
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)

    reconcile = asyncio.create_task(lane.reconcile())
    await get_started.wait()
    await lane.handle_mission_event({
        "event_type": "mission_resumed",
        "mission": _snapshot(
            status="active",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
    })
    release_get.set()
    await reconcile

    assert binding.status == "active"
    assert binding.updated_at == "2026-07-19T00:00:03.000Z"
    manager.pause.assert_not_called()
    await lane.close()


@pytest.mark.asyncio
async def test_adoption_rejects_live_owner_in_different_session(
    tmp_path: Path,
) -> None:
    active = _snapshot(
        "mission-existing",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    api = _api(active=active)
    api.create_mission.side_effect = BgosApiError(
        409,
        "MISSION_ALREADY_ACTIVE",
        {"error": "MISSION_ALREADY_ACTIVE"},
    )

    def manager_for_session(session_id: str) -> Mock:
        created_at = 1.0 if session_id == "session-a" else 2.0
        return Mock(state=_GoalState("active", created_at=created_at))

    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-b",
        goal_manager_factory=manager_for_session,
        binding_store_path=tmp_path / "bindings.json",
    )
    old_binding = _store_binding(
        lane,
        snapshot=active,
        chat_id=41,
        session_id="session-a",
    )

    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    api.create_mission.assert_awaited_once()
    assert lane._bindings[(41, 7)] is old_binding
    assert (42, 7) not in lane._bindings
    await lane.close()


@pytest.mark.asyncio
async def test_adoption_prefers_matching_session_binding(
    tmp_path: Path,
) -> None:
    active = _snapshot(
        "mission-existing",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    api = _api(active=active)
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    _store_binding(lane, snapshot=active)

    await lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    api.create_mission.assert_not_awaited()
    assert lane._bindings[(42, 7)].mission_id == "mission-existing"
    assert lane._bindings[(42, 7)].session_id == "session-abc"
    await lane.close()


@pytest.mark.asyncio
async def test_manager_failure_keeps_enforcement_pending(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = Mock(state=_GoalState("active"))
    manager.pause.side_effect = OSError("goal store unavailable")
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)
    lane.schedule_reconcile = Mock()

    with caplog.at_level(logging.ERROR):
        await lane.handle_mission_event({
            "event_type": "mission_paused",
            "mission": _snapshot(
                status="paused",
                updated_at="2026-07-19T00:00:02.000Z",
            ),
        })

    assert binding.pending_enforcement is True
    lane.schedule_reconcile.assert_called_once_with()
    assert "GoalManager enforcement failed" in caplog.text
    await lane.close()


@pytest.mark.asyncio
async def test_failed_abandon_stays_bound_and_reconcile_retries(
    tmp_path: Path,
) -> None:
    active = _snapshot(
        updated_at="2026-07-19T00:00:02.000Z",
    )
    api = _api(active=active)
    api.abandon_mission.side_effect = [
        RuntimeError("temporary failure"),
        _response(_snapshot(
            status="abandoned",
            updated_at="2026-07-19T00:00:03.000Z",
        )),
    ]
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)
    schedule_reconcile = Mock()
    lane.schedule_reconcile = schedule_reconcile

    await lane.handle_goal_line(
        "✓ Goal cleared.",
        chat_id=42,
        assistant_id=7,
    )

    assert lane._bindings[(42, 7)] is binding
    assert binding.pending_abandon is True
    schedule_reconcile.assert_called_once_with()

    await lane.reconcile()

    assert api.abandon_mission.await_count == 2
    manager.clear.assert_not_called()
    assert lane._bindings == {}
    await lane.close()


@pytest.mark.asyncio
async def test_ambiguous_create_intent_survives_restart_and_binds_event(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_api = _api(active=None)
    first_api.create_mission.side_effect = TimeoutError("response lost")
    first_lane = MissionLane(
        first_api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(
            return_value=Mock(state=_GoalState("active")),
        ),
        binding_store_path=store_path,
    )

    await first_lane.handle_goal_line(
        "⊙ Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    assert len(first_lane._pending_creates) == 1
    await first_lane.close()

    second_lane = MissionLane(
        _api(),
        binding_store_path=store_path,
    )
    assert len(second_lane._pending_creates) == 1

    await second_lane.handle_mission_event({
        "event_type": "mission_created",
        "mission": _snapshot(
            "mission-committed",
            created_at=time.time(),
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    binding = second_lane._bindings[(42, 7)]
    assert binding.mission_id == "mission-committed"
    assert binding.session_id == "session-abc"
    assert binding.goal_text == GOAL
    assert second_lane._pending_creates == []
    await second_lane.close()


@pytest.mark.asyncio
async def test_control_revalidates_binding_after_progress_cancel(
    tmp_path: Path,
) -> None:
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    async def delayed_cancel(mission_id: str) -> None:
        cancel_started.set()
        await release_cancel.wait()

    manager_factory = Mock()
    lane = MissionLane(
        _api(),
        session_id_resolver=lambda chat_id, assistant_id: "session-old",
        goal_manager_factory=manager_factory,
        binding_store_path=tmp_path / "bindings.json",
    )
    _store_binding(lane, session_id="session-old")
    lane._cancel_progress = delayed_cancel

    control = asyncio.create_task(lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    }))
    await cancel_started.wait()
    replacement = _store_binding(
        lane,
        snapshot=_snapshot(
            title="A newer goal",
            updated_at="2026-07-19T00:00:03.000Z",
        ),
        session_id="session-new",
        goal="A newer goal",
        created_at=2.0,
    )
    release_cancel.set()
    await control

    manager_factory.assert_not_called()
    assert lane._bindings[(42, 7)] is replacement
    await lane.close()


@pytest.mark.asyncio
async def test_session_resolution_exception_is_retried(
    tmp_path: Path,
) -> None:
    resolver = AsyncMock(side_effect=[OSError("not ready"), "session-abc"])
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        _api(),
        session_id_resolver=resolver,
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    _store_binding(lane)

    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": _snapshot(
            status="paused",
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    assert resolver.await_count == 2
    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    await lane.close()


@pytest.mark.asyncio
async def test_control_failure_during_reconcile_requests_second_pass(
    tmp_path: Path,
) -> None:
    get_started = asyncio.Event()
    release_get = asyncio.Event()
    paused = _snapshot(
        status="paused",
        updated_at="2026-07-19T00:00:02.000Z",
    )

    async def active_snapshot(**kwargs) -> dict:
        if not get_started.is_set():
            get_started.set()
            await release_get.wait()
        return _response(paused)

    api = _api()
    api.get_active_mission.side_effect = active_snapshot
    resolver = AsyncMock(side_effect=[None, None, "session-abc"])
    manager = Mock(state=_GoalState("active"))
    lane = MissionLane(
        api,
        session_id_resolver=resolver,
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)

    lane.schedule_reconcile()
    reconcile_task = lane._reconcile_task
    assert reconcile_task is not None
    await get_started.wait()
    await lane.handle_mission_event({
        "event_type": "mission_paused",
        "mission": paused,
    })
    assert binding.pending_enforcement is True
    release_get.set()
    await reconcile_task

    assert api.get_active_mission.await_count == 2
    assert resolver.await_count == 3
    manager.pause.assert_called_once_with(reason="bgos-mission-paused")
    assert binding.pending_enforcement is False
    await lane.close()


@pytest.mark.asyncio
async def test_reconcile_clears_persisted_open_binding_when_server_has_none(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_lane = MissionLane(_api(), binding_store_path=store_path)
    _store_binding(first_lane)
    await first_lane.close()

    manager = Mock(state=_GoalState("active"))
    second_lane = MissionLane(
        _api(active=None),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=store_path,
    )

    await second_lane.reconcile()

    manager.clear.assert_called_once_with()
    assert second_lane._bindings == {}
    await second_lane.close()


@pytest.mark.asyncio
async def test_different_session_owner_survives_restart_and_is_not_adopted(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    active = _snapshot(
        "mission-existing",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    first_lane = MissionLane(_api(), binding_store_path=store_path)
    _store_binding(
        first_lane,
        snapshot=active,
        chat_id=41,
        session_id="session-a",
    )
    await first_lane.close()

    api = _api(active=active)
    api.create_mission.side_effect = BgosApiError(
        409,
        "MISSION_ALREADY_ACTIVE",
        {"error": "MISSION_ALREADY_ACTIVE"},
    )

    def manager_for_session(session_id: str) -> Mock:
        created_at = 1.0 if session_id == "session-a" else 2.0
        return Mock(state=_GoalState("active", created_at=created_at))

    second_lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-b",
        goal_manager_factory=manager_for_session,
        binding_store_path=store_path,
    )

    await second_lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    owner = second_lane._bindings[(41, 7)]
    assert owner.session_id == "session-a"
    assert owner.goal_text == GOAL
    assert (42, 7) not in second_lane._bindings
    api.create_mission.assert_awaited_once()
    await second_lane.close()


@pytest.mark.asyncio
async def test_unknown_session_owner_is_not_adopted_by_unknown_session_goal(
    tmp_path: Path,
) -> None:
    active = _snapshot(
        "mission-existing",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    api = _api(active=active)
    api.create_mission.side_effect = BgosApiError(
        409,
        "MISSION_ALREADY_ACTIVE",
        {"error": "MISSION_ALREADY_ACTIVE"},
    )
    lane = MissionLane(api, binding_store_path=tmp_path / "bindings.json")
    owner = _store_binding(
        lane,
        snapshot=active,
        chat_id=41,
        session_id=None,
    )

    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    assert lane._bindings[(41, 7)] is owner
    assert (42, 7) not in lane._bindings
    api.create_mission.assert_awaited_once()
    await lane.close()


@pytest.mark.asyncio
async def test_incomplete_same_session_identity_is_not_adopted(
    tmp_path: Path,
) -> None:
    active = _snapshot(
        "mission-existing",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    api = _api(active=active)
    api.create_mission.side_effect = BgosApiError(
        409,
        "MISSION_ALREADY_ACTIVE",
        {"error": "MISSION_ALREADY_ACTIVE"},
    )
    manager_factory = Mock(side_effect=OSError("goal store unavailable"))
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=manager_factory,
        binding_store_path=tmp_path / "bindings.json",
    )
    owner = lane._store_binding(
        active,
        chat_id=41,
        assistant_id=7,
        session_id="session-abc",
        goal_text=GOAL,
        goal_created_at=None,
    )
    assert owner is not None

    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    assert lane._bindings[(41, 7)] is owner
    assert (42, 7) not in lane._bindings
    api.create_mission.assert_awaited_once()
    await lane.close()


@pytest.mark.asyncio
async def test_incomplete_pending_abandon_survives_restart_and_is_retried(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_api = _api()
    first_api.abandon_mission.side_effect = OSError("offline")
    first_lane = MissionLane(first_api, binding_store_path=store_path)
    binding = _store_binding(
        first_lane,
        session_id=None,
        created_at=None,
    )
    first_lane.schedule_reconcile = Mock()

    await first_lane.handle_goal_line(
        "\u2713 Goal cleared.",
        chat_id=42,
        assistant_id=7,
    )

    assert binding.pending_abandon is True
    await first_lane.close()

    active = _snapshot(updated_at="2026-07-19T00:00:02.000Z")
    second_api = _api(active=active)
    second_lane = MissionLane(
        second_api,
        binding_store_path=store_path,
    )

    assert second_lane._bindings[(42, 7)].pending_abandon is True
    await second_lane.reconcile()

    second_api.abandon_mission.assert_awaited_once()
    assert second_lane._bindings == {}
    await second_lane.close()


@pytest.mark.asyncio
async def test_incomplete_pending_abandon_404_drops_binding(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_api = _api()
    first_api.abandon_mission.side_effect = OSError("offline")
    first_lane = MissionLane(first_api, binding_store_path=store_path)
    binding = _store_binding(
        first_lane,
        session_id=None,
        created_at=None,
    )
    first_lane.schedule_reconcile = Mock()

    await first_lane.handle_goal_line(
        "\u2713 Goal cleared.",
        chat_id=42,
        assistant_id=7,
    )

    assert binding.pending_abandon is True
    await first_lane.close()

    second_api = _api(active=_snapshot(
        updated_at="2026-07-19T00:00:02.000Z",
    ))
    second_api.abandon_mission.side_effect = BgosApiError(
        404,
        "MISSION_NOT_FOUND",
        {"error": "MISSION_NOT_FOUND"},
    )
    second_lane = MissionLane(
        second_api,
        binding_store_path=store_path,
    )

    await second_lane.reconcile()

    second_api.abandon_mission.assert_awaited_once()
    assert second_lane._bindings == {}
    await second_lane.close()


@pytest.mark.asyncio
async def test_cancelled_abandon_persists_for_restart_retry(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    abandon_started = asyncio.Event()

    async def blocked_abandon(**kwargs) -> dict:
        abandon_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    first_api = _api()
    first_api.abandon_mission.side_effect = blocked_abandon
    first_lane = MissionLane(first_api, binding_store_path=store_path)
    binding = _store_binding(first_lane)

    clear_task = asyncio.create_task(first_lane.handle_goal_line(
        "\u2713 Goal cleared.",
        chat_id=42,
        assistant_id=7,
    ))
    await abandon_started.wait()
    clear_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await clear_task

    assert binding.pending_abandon is True
    assert "mission-1" not in first_lane._inflight_patches
    await first_lane.close()

    active = _snapshot(updated_at="2026-07-19T00:00:02.000Z")
    second_api = _api(active=active)
    manager = Mock(state=None)
    second_lane = MissionLane(
        second_api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=store_path,
    )

    assert second_lane._bindings[(42, 7)].pending_abandon is True
    await second_lane.reconcile()

    second_api.abandon_mission.assert_awaited_once()
    manager.clear.assert_not_called()
    assert second_lane._bindings == {}
    await second_lane.close()


@pytest.mark.asyncio
async def test_cancelled_abandon_during_progress_flush_persists_for_retry(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    flush_started = asyncio.Event()

    async def blocked_flush(mission_id: str) -> None:
        flush_started.set()
        await asyncio.Event().wait()

    first_lane = MissionLane(_api(), binding_store_path=store_path)
    binding = _store_binding(first_lane)
    first_lane._flush_progress = blocked_flush

    clear_task = asyncio.create_task(first_lane.handle_goal_line(
        "\u2713 Goal cleared.",
        chat_id=42,
        assistant_id=7,
    ))
    await asyncio.wait_for(flush_started.wait(), 1.0)
    clear_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await clear_task

    assert binding.pending_abandon is True
    await first_lane.close()

    active = _snapshot(updated_at="2026-07-19T00:00:02.000Z")
    second_api = _api(active=active)
    manager = Mock(state=_GoalState("active"))
    second_lane = MissionLane(
        second_api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=store_path,
    )

    assert second_lane._bindings[(42, 7)].pending_abandon is True
    await second_lane.reconcile()

    second_api.abandon_mission.assert_awaited_once()
    manager.clear.assert_not_called()
    assert second_lane._bindings == {}
    await second_lane.close()


@pytest.mark.asyncio
async def test_pending_abandon_binding_is_not_readopted(
    tmp_path: Path,
) -> None:
    active = _snapshot(updated_at="2026-07-19T00:00:02.000Z")
    api = _api(active=active)
    api.abandon_mission.side_effect = OSError("offline")
    api.create_mission.side_effect = BgosApiError(
        409,
        "MISSION_ALREADY_ACTIVE",
        {"error": "MISSION_ALREADY_ACTIVE"},
    )
    lane = MissionLane(api, binding_store_path=tmp_path / "bindings.json")
    binding = _store_binding(
        lane,
        session_id=None,
        created_at=1.0,
    )
    lane.schedule_reconcile = Mock()

    await lane.handle_goal_line(
        "\u2713 Goal cleared.",
        chat_id=42,
        assistant_id=7,
    )
    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    assert lane._bindings[(42, 7)] is binding
    assert binding.pending_abandon is True
    api.create_mission.assert_awaited_once()
    await lane.close()


@pytest.mark.asyncio
async def test_paused_adoption_failure_stays_paused_and_pending(
    tmp_path: Path,
) -> None:
    paused = _snapshot(
        "mission-existing",
        status="paused",
        updated_at="2026-07-19T00:00:02.000Z",
    )
    api = _api(active=paused)
    manager = Mock(state=_GoalState("active"))
    manager.pause.side_effect = OSError("goal store unavailable")
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    lane.schedule_reconcile = Mock()

    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    binding = lane._bindings[(42, 7)]
    assert binding.status == "paused"
    assert binding.pending_enforcement is True
    api.resume_mission.assert_not_awaited()
    lane.schedule_reconcile.assert_called_once_with()
    await lane.close()


@pytest.mark.asyncio
async def test_mission_created_event_wins_before_create_timeout(
    tmp_path: Path,
) -> None:
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    async def timed_out_create(**kwargs) -> dict:
        create_started.set()
        await release_create.wait()
        raise TimeoutError("response lost")

    api = _api(active=None)
    api.create_mission.side_effect = timed_out_create
    lane = MissionLane(api, binding_store_path=tmp_path / "bindings.json")
    set_task = asyncio.create_task(lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    ))
    await create_started.wait()

    await lane.handle_mission_event({
        "event_type": "mission_created",
        "mission": _snapshot(
            "mission-committed",
            created_at=time.time(),
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })
    release_create.set()
    await set_task

    assert lane._bindings[(42, 7)].mission_id == "mission-committed"
    assert lane._pending_creates == []
    assert lane._reconcile_task is None
    await lane.close()


@pytest.mark.asyncio
async def test_reconcile_recovers_pending_create_without_ws_event(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    first_api = _api(active=None)
    first_api.create_mission.side_effect = TimeoutError("response lost")
    first_lane = MissionLane(first_api, binding_store_path=store_path)
    first_lane.schedule_reconcile = Mock()

    await first_lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    await first_lane.close()

    committed = _snapshot(
        "mission-committed",
        created_at=time.time(),
        updated_at="2026-07-19T00:00:02.000Z",
    )
    manager = Mock(state=_GoalState("active"))
    second_lane = MissionLane(
        _api(active=committed),
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=store_path,
    )

    await second_lane.reconcile()

    binding = second_lane._bindings[(42, 7)]
    assert binding.mission_id == "mission-committed"
    assert second_lane._pending_creates == []
    await second_lane.close()


@pytest.mark.asyncio
async def test_expired_create_intent_does_not_bind_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    monkeypatch.setattr(missions_bridge_module.time, "time", lambda: now)
    api = _api(active=None)
    api.create_mission.side_effect = TimeoutError("response lost")
    lane = MissionLane(api, binding_store_path=tmp_path / "bindings.json")
    lane.schedule_reconcile = Mock()

    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    now += lane._PENDING_CREATE_SECONDS + 1.0
    await lane.handle_mission_event({
        "event_type": "mission_created",
        "mission": _snapshot(
            "mission-too-late",
            created_at=now,
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    assert lane._bindings == {}
    assert lane._pending_creates == []
    await lane.close()


@pytest.mark.asyncio
async def test_ambiguous_create_refreshes_intent_and_ignores_server_clock_skew(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        missions_bridge_module.time,
        "time",
        lambda: clock["now"],
    )

    async def delayed_timeout(**kwargs) -> dict:
        clock["now"] += MissionLane._PENDING_CREATE_SECONDS + 1.0
        raise TimeoutError("response lost")

    api = _api(active=None)
    api.create_mission.side_effect = delayed_timeout
    lane = MissionLane(api, binding_store_path=tmp_path / "bindings.json")
    lane.schedule_reconcile = Mock()

    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    assert lane._pending_creates[0].created_at == clock["now"]

    await lane.handle_mission_event({
        "event_type": "mission_created",
        "mission": _snapshot(
            "mission-clock-skewed",
            created_at=-100000.0,
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    assert lane._bindings[(42, 7)].mission_id == "mission-clock-skewed"
    assert lane._pending_creates == []
    await lane.close()


@pytest.mark.asyncio
async def test_same_title_create_intents_in_two_sessions_are_ambiguous(
    tmp_path: Path,
) -> None:
    api = _api(active=None)
    api.create_mission.side_effect = TimeoutError("response lost")

    def manager_for_session(session_id: str) -> Mock:
        created_at = 1.0 if session_id == "session-a" else 2.0
        return Mock(state=_GoalState("active", created_at=created_at))

    lane = MissionLane(
        api,
        session_id_resolver=(
            lambda chat_id, assistant_id: (
                "session-a" if chat_id == 41 else "session-b"
            )
        ),
        goal_manager_factory=manager_for_session,
        binding_store_path=tmp_path / "bindings.json",
    )
    lane.schedule_reconcile = Mock()

    for chat_id in (41, 42):
        await lane.handle_goal_line(
            "\u2299 Goal set (20-turn budget): Draft the customer replies",
            chat_id=chat_id,
            assistant_id=7,
        )
    assert len(lane._pending_creates) == 2

    await lane.handle_mission_event({
        "event_type": "mission_created",
        "mission": _snapshot(
            "mission-ambiguous",
            created_at=time.time(),
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    assert lane._bindings == {}
    assert len(lane._pending_creates) == 2
    await lane.close()


@pytest.mark.asyncio
async def test_definitive_create_error_removes_persisted_intent(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "bindings.json"
    api = _api(active=None)
    api.create_mission.side_effect = BgosApiError(
        400,
        "INVALID_MISSION",
        {"error": "INVALID_MISSION"},
    )
    lane = MissionLane(api, binding_store_path=store_path)

    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    assert lane._pending_creates == []
    await lane.close()
    reloaded = MissionLane(_api(), binding_store_path=store_path)
    assert reloaded._pending_creates == []
    await reloaded.close()


@pytest.mark.asyncio
async def test_server_create_error_keeps_intent_for_committed_mission(
    tmp_path: Path,
) -> None:
    api = _api(active=None)
    api.create_mission.side_effect = BgosApiError(
        503,
        "SERVICE_UNAVAILABLE",
        {"error": "SERVICE_UNAVAILABLE"},
    )
    lane = MissionLane(api, binding_store_path=tmp_path / "bindings.json")
    lane.schedule_reconcile = Mock()

    await lane.handle_goal_line(
        "\u2299 Goal set (20-turn budget): Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )

    assert len(lane._pending_creates) == 1
    lane.schedule_reconcile.assert_called_once_with()

    await lane.handle_mission_event({
        "event_type": "mission_created",
        "mission": _snapshot(
            "mission-committed",
            created_at=1.0,
            updated_at="2026-07-19T00:00:02.000Z",
        ),
    })

    assert lane._bindings[(42, 7)].mission_id == "mission-committed"
    assert lane._pending_creates == []
    await lane.close()


@pytest.mark.asyncio
async def test_local_pause_clears_stale_lane_pause_provenance(
    tmp_path: Path,
) -> None:
    active = _snapshot(updated_at="2026-07-19T00:00:03.000Z")
    api = _api(active=active)
    api.pause_mission.return_value = _response(_snapshot(
        status="paused",
        updated_at="2026-07-19T00:00:02.000Z",
    ))
    manager = Mock(state=_GoalState(
        "paused",
        paused_reason="paused by user",
    ))
    lane = MissionLane(
        api,
        session_id_resolver=lambda chat_id, assistant_id: "session-abc",
        goal_manager_factory=Mock(return_value=manager),
        binding_store_path=tmp_path / "bindings.json",
    )
    binding = _store_binding(lane)
    binding.paused_by_lane = True

    await lane.handle_goal_line(
        "\u23f8 Goal paused: Draft the customer replies",
        chat_id=42,
        assistant_id=7,
    )
    assert binding.paused_by_lane is False

    await lane.reconcile()

    manager.resume.assert_not_called()
    await lane.close()
