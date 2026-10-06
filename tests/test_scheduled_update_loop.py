"""The adapter's scheduled-update loop (design 2.3, decision D8), driven
tick by tick with fakes for every self_update effect and an injected clock.

What must hold: it pulls and restarts ONLY under a verified supervisor and
only at a safe moment (idle held for 10 minutes, no message in or out for
10 minutes); work that arrives during the pull keeps the install staged; it
never runs beside an update_now; failures back off; one loop per process.
No subprocess, no network: the restart is a recording fake.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
from hermes_channel_bgos import __version__, scheduled_update, self_update
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.self_update import AppliedUpdate, SelfUpdateError


pytestmark = pytest.mark.asyncio

_MAJOR, _MINOR, _PATCH = (int(p) for p in __version__.split(".")[:3])
NEWER = f"{_MAJOR}.{_MINOR + 1}.0"
QUIET = scheduled_update.QUIET_SECONDS
LAUNCHD = self_update.Supervisor("launchd", "gui/501/ai.hermes.gateway")


@dataclass
class FakeApi:
    heartbeats: list[dict[str, Any]] = field(default_factory=list)
    acks: list[str] = field(default_factory=list)
    progresses: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def post_heartbeat(self, **kwargs: Any) -> None:
        self.heartbeats.append(kwargs)

    async def post_update_rpc_ack(self, rpc_id: str) -> None:
        self.acks.append(rpc_id)

    async def post_update_rpc_progress(self, rpc_id: str, **kwargs: Any) -> None:
        self.progresses.append((rpc_id, kwargs))

    async def close(self) -> None:
        pass


def _new_adapter(clock: list[float]) -> tuple[BGOSAdapter, FakeApi]:
    adapter = BGOSAdapter(
        BgosConfig(base_url="https://bgos.test", pairing_token="pair_xyz"),
    )
    api = FakeApi()
    adapter._real_api = adapter._api  # type: ignore[attr-defined]
    adapter._api = api  # type: ignore[assignment]
    adapter._clock = lambda: clock[0]
    return adapter, api


@pytest.fixture
async def sched(monkeypatch: pytest.MonkeyPatch):
    clock = [1_000.0]
    adapter, api = _new_adapter(clock)
    monkeypatch.setattr(bgos_adapter_module, "_LIVE_ADAPTERS", [])
    monkeypatch.setattr(bgos_adapter_module, "_scheduled_update_owner", None)
    monkeypatch.delenv("BGOS_AUTO_UPDATE", raising=False)
    state = SimpleNamespace(
        supervisor=LAUNCHD,
        latest=NEWER,
        pending=None,
        apply_result=AppliedUpdate(__version__, NEWER),
        applied=[],
        restarts=[],
        spawn_ok=True,
    )

    def apply(clone_dir=None):
        state.applied.append(True)
        if callable(state.apply_result):
            return state.apply_result()
        return state.apply_result

    monkeypatch.setattr(self_update, "verified_supervisor", lambda: state.supervisor)
    monkeypatch.setattr(self_update, "latest_known_version", lambda: state.latest)
    monkeypatch.setattr(
        self_update, "pending_restart_version", lambda clone_dir=None: state.pending,
    )
    monkeypatch.setattr(self_update, "apply_update", apply)
    monkeypatch.setattr(
        self_update, "schedule_supervisor_restart",
        lambda supervisor: state.restarts.append(supervisor) or state.spawn_ok,
    )
    try:
        yield adapter, api, clock, state
    finally:
        await adapter._real_api.close()  # type: ignore[attr-defined]
        await adapter.disconnect()


async def _tick(adapter: BGOSAdapter) -> str:
    return await adapter._scheduled_update_tick()


async def _idle_through_quiet_window(adapter, clock) -> str:
    """First tick starts the idle stretch; the next one a full quiet window
    later is the earliest safe moment."""
    first = await _tick(adapter)
    assert first == "settling"
    clock[0] += QUIET
    return await _tick(adapter)


# -----------------------------------------------------------------------------
# The happy path, and the waiting before it
# -----------------------------------------------------------------------------


async def test_newer_version_is_pulled_and_restarted_after_the_quiet_window(sched):
    adapter, api, clock, state = sched
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET - 1
    assert await _tick(adapter) == "settling"
    assert state.applied == [] and state.restarts == []

    clock[0] += 1
    assert await _tick(adapter) == "restarting"
    assert state.applied == [True]
    assert state.restarts == [LAUNCHD]
    # Reported where it can be: a heartbeat with the fresh readiness rides
    # out before the restart (there is no rpcId to post progress to).
    assert api.heartbeats, "the restart must be announced by a heartbeat"
    assert api.heartbeats[-1]["daemon_version"] == __version__


async def test_an_already_staged_install_restarts_without_pulling(sched):
    adapter, _api, clock, state = sched
    state.latest = None
    state.pending = NEWER
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    assert state.applied == []
    assert state.restarts == [LAUNCHD]


async def test_up_to_date_does_nothing(sched):
    adapter, _api, clock, state = sched
    state.latest = __version__
    assert await _tick(adapter) == "up_to_date"
    clock[0] += 10 * QUIET
    assert await _tick(adapter) == "up_to_date"
    assert state.applied == [] and state.restarts == []


# -----------------------------------------------------------------------------
# D8: never without a verified supervisor; the kill switch
# -----------------------------------------------------------------------------


async def test_unsupervised_host_never_pulls_or_exits(sched):
    adapter, _api, clock, state = sched
    state.supervisor = None
    state.pending = NEWER
    for _ in range(5):
        assert await _tick(adapter) == "unsupervised"
        clock[0] += QUIET
    assert state.applied == [] and state.restarts == []


async def test_kill_switch_stops_the_scheduled_apply(sched, monkeypatch):
    adapter, _api, clock, state = sched
    monkeypatch.setenv("BGOS_AUTO_UPDATE", "0")
    assert await _tick(adapter) == "updates_disabled"
    clock[0] += 10 * QUIET
    assert await _tick(adapter) == "updates_disabled"
    assert state.applied == [] and state.restarts == []


# -----------------------------------------------------------------------------
# The safe moment: busy, recent messages, work arriving mid-pull
# -----------------------------------------------------------------------------


async def test_an_active_session_restarts_the_idle_window(sched):
    adapter, _api, clock, state = sched
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET - 60
    adapter._active_sessions = {"bgos:1": object()}
    assert await _tick(adapter) == "busy"
    clock[0] += 60
    adapter._active_sessions = {}
    # Idle again, but only just: the window starts over.
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET - 1
    assert await _tick(adapter) == "settling"
    clock[0] += 1
    assert await _tick(adapter) == "restarting"
    assert len(state.restarts) == 1


async def test_a_pending_plugin_task_is_busy(sched):
    adapter, _api, clock, state = sched
    hung = asyncio.create_task(asyncio.sleep(30))
    adapter._voice_tasks.add(hung)
    try:
        assert await _tick(adapter) == "busy"
        clock[0] += 10 * QUIET
        assert await _tick(adapter) == "busy"
        assert state.restarts == []
        assert not hung.cancelled()
    finally:
        adapter._voice_tasks.discard(hung)
        hung.cancel()
        await asyncio.gather(hung, return_exceptions=True)


async def test_a_recent_inbound_message_defers_the_restart(sched):
    adapter, _api, clock, state = sched
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET - 100
    adapter._note_inbound_message()
    clock[0] += 100
    assert await _tick(adapter) == "recent_message"
    clock[0] += QUIET - 100
    assert await _tick(adapter) == "restarting"
    assert len(state.restarts) == 1


async def test_a_recent_outbound_message_defers_the_restart(sched):
    adapter, _api, clock, state = sched
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET - 1
    adapter._note_outbound_message()
    clock[0] += 1
    assert await _tick(adapter) == "recent_message"
    assert state.restarts == []


async def test_work_arriving_during_the_pull_keeps_the_install_staged(sched):
    adapter, api, clock, state = sched

    def apply_then_message_arrives():
        adapter._active_sessions = {"bgos:1": object()}
        return AppliedUpdate(__version__, NEWER)

    state.apply_result = apply_then_message_arrives
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    assert state.restarts == []
    # Stopped right after the pull: announced once as staged (its readiness
    # carries pendingRestartVersion), never as restarting, no attempt used.
    assert len(api.heartbeats) == 1
    assert scheduled_update.load_attempts(scheduled_update.attempts_path()) == {}
    adapter._active_sessions = {}


async def test_a_message_during_the_pull_keeps_the_install_staged(sched):
    adapter, _api, clock, state = sched

    def apply_then_message_arrives():
        adapter._note_inbound_message()
        return AppliedUpdate(__version__, NEWER)

    state.apply_result = apply_then_message_arrives
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    assert state.restarts == []


async def test_a_turn_starting_during_the_restart_report_cancels_nothing(sched):
    """The heartbeat POST before the restart takes real time; a session
    that starts during it keeps the install staged."""
    adapter, api, clock, state = sched

    async def heartbeat_while_a_turn_starts(**kwargs):
        api.heartbeats.append(kwargs)
        adapter._active_sessions = {"bgos:1": object()}

    api.post_heartbeat = heartbeat_while_a_turn_starts  # type: ignore[method-assign]
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    assert state.restarts == []
    # A run that never restarted used none of the 3 attempts, and the next
    # safe moment is not pushed back by a backoff.
    assert scheduled_update.load_attempts(scheduled_update.attempts_path()) == {}
    assert adapter._scheduled_update_not_before is None
    adapter._active_sessions = {}


# -----------------------------------------------------------------------------
# Failures: report, back off, never loop
# -----------------------------------------------------------------------------


async def test_a_pull_failure_backs_off_and_never_restarts(sched):
    adapter, _api, clock, state = sched

    def dirty():
        raise SelfUpdateError("dirty_tree")

    state.apply_result = dirty
    assert await _idle_through_quiet_window(adapter, clock) == "error"
    clock[0] += 60
    assert await _tick(adapter) == "backoff"
    clock[0] += scheduled_update.RETRY_SECONDS
    assert await _tick(adapter) == "error"
    assert len(state.applied) == 2
    assert state.restarts == []


async def test_no_update_available_falls_back_to_the_staged_install(sched):
    adapter, _api, clock, state = sched

    def nothing_new():
        state.pending = NEWER
        raise SelfUpdateError("no_update_available")

    state.apply_result = nothing_new
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    assert state.restarts == [LAUNCHD]


async def test_restart_spawn_failure_is_an_error_and_backs_off(sched):
    adapter, _api, clock, state = sched
    state.spawn_ok = False
    assert await _idle_through_quiet_window(adapter, clock) == "error"
    clock[0] += 60
    assert await _tick(adapter) == "backoff"


async def test_every_restart_is_counted_and_the_cap_stops_a_loop(sched):
    """A restart that keeps landing on the old code (pending never clears)
    must not restart forever: 3 attempts per target, across processes."""
    adapter, _api, clock, state = sched
    state.latest = None
    state.pending = NEWER
    path = scheduled_update.attempts_path()
    for attempt in range(1, scheduled_update.MAX_ATTEMPTS_PER_TARGET + 1):
        assert await _idle_through_quiet_window(adapter, clock) == "restarting"
        assert scheduled_update.load_attempts(path) == {NEWER: attempt}
        # The "restarted" process comes back on the old code.
        adapter._scheduled_update_idle_since = None
        adapter._scheduled_update_not_before = None
    assert await _tick(adapter) == "attempts_exhausted"
    assert len(state.restarts) == scheduled_update.MAX_ATTEMPTS_PER_TARGET


async def test_restarts_are_spaced_by_the_retry_window(sched):
    adapter, _api, clock, state = sched
    state.latest = None
    state.pending = NEWER
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    clock[0] += QUIET
    assert await _tick(adapter) == "backoff"
    assert len(state.restarts) == 1


# -----------------------------------------------------------------------------
# Never beside an update_now; one loop per process
# -----------------------------------------------------------------------------


async def test_skips_while_an_update_now_is_in_flight(sched):
    adapter, _api, clock, state = sched
    adapter._update_rpc_in_flight.add("rpc-1")
    assert await _tick(adapter) == "update_in_flight"
    clock[0] += 10 * QUIET
    assert await _tick(adapter) == "update_in_flight"
    assert state.applied == [] and state.restarts == []
    adapter._update_rpc_in_flight.clear()


async def test_update_now_is_refused_while_a_scheduled_run_is_in_progress(sched):
    adapter, api, _clock, state = sched
    adapter._scheduled_update_running = True
    await adapter._handle_update_rpc({"rpcId": "rpc-2", "op": "update_now"})
    assert api.acks == ["rpc-2"]
    assert api.progresses[-1][1]["message"] == "update_in_flight"
    assert state.applied == []
    adapter._scheduled_update_running = False


async def test_another_bgos_adapter_in_the_process_being_busy_blocks(sched):
    """Multiplexed profiles each run their own BGOS adapter in the same
    gateway process: a turn on any of them is a turn the restart would
    kill."""
    adapter, _api, clock, state = sched
    other, _other_api = _new_adapter(clock)
    try:
        other._active_sessions = {"bgos:9": object()}
        bgos_adapter_module._register_live_adapter(other)
        assert await _tick(adapter) == "busy"
        clock[0] += 10 * QUIET
        assert await _tick(adapter) == "busy"
        assert state.restarts == []
    finally:
        bgos_adapter_module._unregister_live_adapter(other)
        await other._real_api.close()  # type: ignore[attr-defined]


async def test_a_message_on_another_bgos_adapter_defers(sched):
    adapter, _api, clock, state = sched
    other, _other_api = _new_adapter(clock)
    try:
        bgos_adapter_module._register_live_adapter(other)
        assert await _tick(adapter) == "settling"
        clock[0] += QUIET - 1
        other._note_outbound_message()
        clock[0] += 1
        assert await _tick(adapter) == "recent_message"
    finally:
        bgos_adapter_module._unregister_live_adapter(other)
        await other._real_api.close()  # type: ignore[attr-defined]


async def test_another_platform_adapter_with_a_live_session_blocks(sched):
    """Best effort beyond BGOS: the gateway runner's other platform
    adapters (Telegram and friends) share the process the restart ends."""
    adapter, _api, clock, state = sched
    telegram = SimpleNamespace(_active_sessions={"telegram:1": object()})
    adapter.gateway_runner = SimpleNamespace(adapters={"telegram": telegram})
    assert await _tick(adapter) == "busy"
    telegram._active_sessions = {}
    assert await _tick(adapter) == "settling"


async def test_only_one_scheduled_loop_acts_per_process(sched):
    adapter, _api, clock, state = sched
    other, _other_api = _new_adapter(clock)
    try:
        assert await _tick(adapter) == "settling"
        assert await other._scheduled_update_tick() == "not_owner"
    finally:
        await other._real_api.close()  # type: ignore[attr-defined]


# -----------------------------------------------------------------------------
# Activity stamps and the loop itself
# -----------------------------------------------------------------------------


async def test_inbound_events_stamp_the_last_message_time(sched, monkeypatch):
    adapter, _api, clock, _state = sched

    async def no_refresh() -> bool:
        return False

    monkeypatch.setattr(adapter, "_refresh_pairing_scope", no_refresh)
    clock[0] = 5_000.0
    await adapter._handle_inbound({"assistant_id": 77, "chat_id": 1, "text": "hi"})
    assert adapter._last_inbound_message_at == 5_000.0
    clock[0] = 5_001.0
    await adapter._handle_inbound_click({"assistantId": 77})
    assert adapter._last_inbound_message_at == 5_001.0
    clock[0] = 5_002.0
    await adapter._handle_callback({"callbackData": ""})
    assert adapter._last_inbound_message_at == 5_002.0


async def test_outbound_api_calls_stamp_the_last_message_time(mock_bgos_server):
    clock = [7_000.0]
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    adapter._clock = lambda: clock[0]
    mock_bgos_server.on("POST", "/api/v1/messages").respond(200, {"id": 1})
    mock_bgos_server.on("PATCH", "/api/v1/messages/1").respond(200, {"id": 1})
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(200, {"id": 2})
    mock_bgos_server.on("POST", "/api/v1/peers/6/send").respond(
        200, {"status": "sent", "messageId": 3},
    )
    try:
        await adapter._api.post_message(chat_id=1, text="hello")
        assert adapter._last_outbound_message_at == 7_000.0
        clock[0] = 7_005.0
        await adapter._api.patch_message(1, text="hello again", user_id="u")
        assert adapter._last_outbound_message_at == 7_005.0
        clock[0] = 7_010.0
        await adapter._api.post_send_message(chat_id=1, assistant_id=5, text="hi")
        assert adapter._last_outbound_message_at == 7_010.0
        clock[0] = 7_015.0
        await adapter._api.send_peer(
            caller_assistant_id=5, target_assistant_id=6, text="ping",
            parent_message_id=1,
        )
        assert adapter._last_outbound_message_at == 7_015.0
    finally:
        await adapter.disconnect()


async def test_the_loop_ticks_and_survives_a_failing_tick(sched, monkeypatch):
    adapter, _api, _clock, _state = sched
    monkeypatch.setattr(bgos_adapter_module, "_SCHEDULED_UPDATE_TICK_SECONDS", 0.001)
    calls: list[int] = []
    done = asyncio.Event()

    async def tick() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("probe blew up")
        if len(calls) >= 3:
            done.set()
        return "settling"

    monkeypatch.setattr(adapter, "_scheduled_update_tick", tick)
    task = asyncio.create_task(adapter._scheduled_update_loop())
    try:
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 3


async def test_connect_starts_the_loop_and_disconnect_stops_it(
    mock_bgos_server, monkeypatch,
) -> None:
    monkeypatch.setattr(bgos_adapter_module, "_LIVE_ADAPTERS", [])
    monkeypatch.setattr(bgos_adapter_module, "_scheduled_update_owner", None)
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": []},
    )
    mock_bgos_server.on("POST", "/api/v1/integrations/heartbeat").respond(204)
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    try:
        task = adapter._scheduled_update_task
        assert task is not None and not task.done()
        assert bgos_adapter_module._live_adapters() == [adapter]

        async def _claimed() -> None:
            while bgos_adapter_module._scheduled_update_owner is None:
                await asyncio.sleep(0.01)

        # The first tick runs at once and claims the process's loop.
        await asyncio.wait_for(_claimed(), timeout=2.0)
        assert bgos_adapter_module._scheduled_update_owner() is adapter
    finally:
        # Bounded: a disconnect that leaves the loop running never returns.
        await asyncio.wait_for(adapter.disconnect(), timeout=5.0)
    assert task.cancelled()
    assert adapter._scheduled_update_task is None
    assert bgos_adapter_module._live_adapters() == []
    # The claim passes on: the next live adapter's loop may act.
    assert bgos_adapter_module._scheduled_update_owner is None
