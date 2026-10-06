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
import sys
import types
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
WALL_EPOCH = 1_790_000_000.0


@dataclass
class FakeApi:
    heartbeats: list[dict[str, Any]] = field(default_factory=list)
    acks: list[str] = field(default_factory=list)
    progresses: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # REST inbound poll: what `inbound?since_message_id=` returns, and the
    # cursors it was asked with.
    inbound: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[int] = field(default_factory=list)

    async def fetch_inbound_since(self, last_id: int) -> dict[str, Any]:
        self.fetches.append(last_id)
        return {"messages": [m for m in self.inbound if m["message_id"] > last_id]}

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
    # Wall clock for lastError `at` and the soak: the monotonic clock plus a
    # fixed epoch, so both move together.
    adapter._wall_clock = lambda: WALL_EPOCH + clock[0]
    return adapter, api


@pytest.fixture
async def sched(monkeypatch: pytest.MonkeyPatch):
    clock = [1_000.0]
    adapter, api = _new_adapter(clock)
    monkeypatch.setattr(bgos_adapter_module, "_LIVE_ADAPTERS", [])
    monkeypatch.setattr(bgos_adapter_module, "_scheduled_update_owner", None)
    monkeypatch.setattr(bgos_adapter_module, "_scheduled_update_outcome", None)
    monkeypatch.setattr(bgos_adapter_module, "_scheduled_update_boot_checked", False)
    monkeypatch.delenv("BGOS_AUTO_UPDATE", raising=False)
    state = SimpleNamespace(
        supervisor=LAUNCHD,
        latest=NEWER,
        pending=None,
        apply_result=AppliedUpdate(__version__, NEWER),
        applied=[],
        apply_kwargs=[],
        restarts=[],
        spawn_ok=True,
    )

    def apply(clone_dir=None, **kwargs):
        state.applied.append(True)
        state.apply_kwargs.append(kwargs)
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
# Soak: the scheduled apply (never update_now) takes a target only once it
# has been on origin/main for 24 hours
# -----------------------------------------------------------------------------


async def test_the_scheduled_apply_asks_for_a_day_of_soak(sched):
    adapter, _api, clock, state = sched
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    [kwargs] = state.apply_kwargs
    assert kwargs["soak_seconds"] == 24 * 60 * 60
    # The injected wall clock, the one git committer times compare with.
    assert kwargs["now"]() == WALL_EPOCH + clock[0]


async def test_a_soaking_target_waits_quietly(sched, caplog):
    adapter, api, clock, state = sched
    caplog.set_level("INFO", logger=bgos_adapter_module.log.name)

    def too_fresh():
        raise SelfUpdateError("soak", retry_after=3600.0)

    state.apply_result = too_fresh
    assert await _idle_through_quiet_window(adapter, clock) == "soak"
    assert state.restarts == []
    # A wait, not a failure: nothing reaches the app as an error, and no
    # attempt is used.
    assert _errors(api) == []
    assert scheduled_update.load_attempts(scheduled_update.attempts_path()) == {}
    assert any("reason=soak" in r.getMessage() for r in caplog.records)
    # No new fetch until the soak is due.
    clock[0] += 3599
    assert await _tick(adapter) == "backoff"
    assert len(state.applied) == 1
    clock[0] += 1
    state.apply_result = AppliedUpdate(__version__, NEWER)
    assert await _tick(adapter) == "restarting"
    assert state.restarts == [LAUNCHD]


async def test_a_pulled_release_is_not_an_error(sched):
    """The daily check saw a version that origin/main no longer has (pulled
    during its soak) and nothing is staged: nothing to do, nothing failed."""
    adapter, api, clock, state = sched

    def gone():
        raise SelfUpdateError("no_update_available")

    state.apply_result = gone
    assert await _idle_through_quiet_window(adapter, clock) == "no_update_available"
    assert _errors(api) == []
    assert state.restarts == []
    clock[0] += 60
    assert await _tick(adapter) == "backoff"


async def test_a_pinned_clone_waits_quietly(sched, caplog):
    """A pin or a rollback holds the clone (apply_update refuses `pinned`,
    finding H1): a wait, not a failure. Nothing restarts, no attempt is
    used, nothing reaches the app as an error."""
    adapter, api, clock, state = sched
    caplog.set_level("INFO", logger=bgos_adapter_module.log.name)

    def pinned():
        raise SelfUpdateError("pinned")

    state.apply_result = pinned
    assert await _idle_through_quiet_window(adapter, clock) == "pinned"
    assert state.restarts == []
    assert _errors(api) == []
    assert scheduled_update.load_attempts(scheduled_update.attempts_path()) == {}
    assert any("reason=pinned" in r.getMessage() for r in caplog.records)
    clock[0] += 60
    assert await _tick(adapter) == "backoff"
    assert len(state.applied) == 1


async def test_a_pin_withdraws_a_failure_reported_before_it(sched):
    """An error the scheduled apply reported before the operator pinned is
    no longer this host's state: the apply is off for a held clone, so the
    report is forgotten and the app's error cleared, once."""
    adapter, api, clock, state = sched
    error = scheduled_update.last_error(
        scheduled_update.FAILED_CODE,
        f"Scheduled update to {NEWER} failed: dirty_tree", at=0.0,
    )
    scheduled_update.save_report(scheduled_update.report_path(), NEWER, error)

    def pinned():
        raise SelfUpdateError("pinned")

    state.apply_result = pinned
    assert await _idle_through_quiet_window(adapter, clock) == "pinned"
    assert _errors(api) == [error, None]
    assert scheduled_update.load_report(scheduled_update.report_path()) is None
    clock[0] += scheduled_update.RETRY_SECONDS
    assert await _tick(adapter) == "pinned"
    assert _errors(api) == [error, None]


async def test_update_now_reports_a_pinned_clone(sched):
    adapter, api, _clock, state = sched

    def pinned():
        raise SelfUpdateError("pinned")

    state.apply_result = pinned
    await adapter._handle_update_rpc({"rpcId": "rpc-pin", "op": "update_now"})
    await asyncio.gather(*adapter._update_tasks, return_exceptions=True)
    assert api.progresses[-1][1] == {
        "stage": "error", "target_version": None, "message": "pinned",
    }
    assert state.restarts == []


async def test_update_now_never_soaks(sched):
    adapter, _api, _clock, state = sched
    await adapter._handle_update_rpc({"rpcId": "rpc-now", "op": "update_now"})
    await asyncio.gather(*adapter._update_tasks, return_exceptions=True)
    assert state.apply_kwargs == [{}]
    assert state.restarts == [LAUNCHD]


# -----------------------------------------------------------------------------
# Visible outcomes: a failed or exhausted run reaches the app as the
# heartbeat lastError, and a landed one clears it (backend HeartbeatErrorDto)
# -----------------------------------------------------------------------------


def _errors(api: FakeApi) -> list[Any]:
    """The lastError each heartbeat carried; heartbeats without the key
    (leave the stored error untouched) are skipped."""
    return [hb["last_error"] for hb in api.heartbeats if "last_error" in hb]


async def test_a_failed_pull_reaches_the_app_as_last_error(sched):
    adapter, api, clock, state = sched

    def dirty():
        raise SelfUpdateError("dirty_tree")

    state.apply_result = dirty
    assert await _idle_through_quiet_window(adapter, clock) == "error"
    [error] = _errors(api)
    assert error["code"] == "scheduled_update_failed"
    assert "dirty_tree" in error["message"] and NEWER in error["message"]
    assert len(error["message"]) <= 300
    assert error["at"].endswith("Z")
    # Persisted, so the next process can re-send or clear it.
    report = scheduled_update.load_report(scheduled_update.report_path())
    assert report == {"target": NEWER, "lastError": error}

    # The same failure again after the backoff is not news.
    clock[0] += scheduled_update.RETRY_SECONDS
    assert await _tick(adapter) == "error"
    assert len(_errors(api)) == 1


async def test_a_restart_spawn_failure_reaches_the_app(sched):
    adapter, api, clock, state = sched
    state.spawn_ok = False
    assert await _idle_through_quiet_window(adapter, clock) == "error"
    [error] = _errors(api)
    assert error["code"] == "scheduled_update_failed"
    assert "restart_spawn_failed" in error["message"]


async def test_exhausted_attempts_reach_the_app_once(sched):
    adapter, api, clock, state = sched
    state.latest = None
    state.pending = NEWER
    path = scheduled_update.attempts_path()
    for _ in range(scheduled_update.MAX_ATTEMPTS_PER_TARGET):
        scheduled_update.record_attempt(path, NEWER)
    assert await _tick(adapter) == "attempts_exhausted"
    clock[0] += QUIET
    assert await _tick(adapter) == "attempts_exhausted"
    [error] = _errors(api)
    assert error["code"] == "scheduled_update_exhausted"
    assert NEWER in error["message"]
    assert state.restarts == []


async def test_a_landed_update_clears_last_error_on_the_next_boot(sched):
    """The restart recorded an attempt onto the version this process now
    runs: the update took, so the first look clears any stored error
    (lastError: null) and forgets the record."""
    adapter, api, _clock, state = sched
    state.latest = __version__
    attempts = scheduled_update.attempts_path()
    report = scheduled_update.report_path()
    scheduled_update.record_attempt(attempts, __version__)
    scheduled_update.save_report(
        report, __version__,
        scheduled_update.last_error(
            scheduled_update.FAILED_CODE, "fetch_failed", at=0.0,
        ),
    )
    assert await _tick(adapter) == "up_to_date"
    assert _errors(api) == [None]
    assert scheduled_update.load_attempts(attempts) == {}
    assert scheduled_update.load_report(report) is None
    # Said once: later beats leave the (now empty) stored error alone.
    assert await _tick(adapter) == "up_to_date"
    await adapter._post_heartbeat_once()
    assert _errors(api) == [None]


async def test_a_landed_scheduled_update_clears_without_a_report(sched):
    adapter, api, _clock, state = sched
    state.latest = __version__
    scheduled_update.record_attempt(scheduled_update.attempts_path(), __version__)
    await _tick(adapter)
    assert _errors(api) == [None]


async def test_an_unresolved_report_is_sent_again_after_a_restart(sched):
    """A restart can cut off the heartbeat that carried the error; the next
    process says it again while the target has not landed."""
    adapter, api, _clock, state = sched
    error = scheduled_update.last_error(
        scheduled_update.FAILED_CODE, "Scheduled update to x failed: merge_failed",
        at=0.0,
    )
    scheduled_update.save_report(scheduled_update.report_path(), NEWER, error)
    await _tick(adapter)
    assert _errors(api) == [error]


async def test_no_record_means_nothing_to_say(sched):
    adapter, api, _clock, state = sched
    state.latest = __version__
    assert await _tick(adapter) == "up_to_date"
    assert _errors(api) == []


async def test_every_bgos_adapter_in_the_process_carries_the_outcome(sched):
    """Multiplexed profiles: one run updates every pairing in the process,
    so each one's heartbeat carries the outcome."""
    adapter, api, clock, state = sched
    other, other_api = _new_adapter(clock)
    try:
        bgos_adapter_module._register_live_adapter(adapter)
        bgos_adapter_module._register_live_adapter(other)
        state.spawn_ok = False
        assert await _idle_through_quiet_window(adapter, clock) == "error"
        assert [e["code"] for e in _errors(api)] == ["scheduled_update_failed"]
        assert [e["code"] for e in _errors(other_api)] == ["scheduled_update_failed"]
    finally:
        bgos_adapter_module._unregister_live_adapter(other)
        await other._real_api.close()  # type: ignore[attr-defined]


async def test_a_failed_heartbeat_post_retries_the_outcome(sched):
    adapter, api, clock, state = sched
    real_post = api.post_heartbeat
    failures = [RuntimeError("backend down")]

    async def flaky(**kwargs):
        if failures and "last_error" in kwargs:
            raise failures.pop()
        await real_post(**kwargs)

    api.post_heartbeat = flaky  # type: ignore[method-assign]
    state.spawn_ok = False
    assert await _idle_through_quiet_window(adapter, clock) == "error"
    assert _errors(api) == []
    await adapter._post_heartbeat_once()
    assert [e["code"] for e in _errors(api)] == ["scheduled_update_failed"]
    await adapter._post_heartbeat_once()
    assert len(_errors(api)) == 1


async def test_a_staged_run_is_not_an_error(sched):
    adapter, api, clock, state = sched

    def apply_then_message_arrives():
        adapter._note_inbound_message()
        return AppliedUpdate(__version__, NEWER)

    state.apply_result = apply_then_message_arrives
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    assert _errors(api) == []


# -----------------------------------------------------------------------------
# Intake hold: once the restart is committed, new inbound work is not taken.
# The persisted cursor stays put, so the next process is handed it again;
# work already in flight is never touched.
# -----------------------------------------------------------------------------


def _message(message_id: int, text: str = "hello") -> dict[str, Any]:
    return {
        "assistant_id": 77, "chat_id": 42, "message_id": message_id,
        "user_id": "u", "text": text, "files": [], "message_type": "standard",
    }


def _capture(adapter: BGOSAdapter) -> list[str]:
    adapter._state.set_route(77, "default")
    adapter._text_batch_window = 0.01
    received: list[str] = []

    async def capture(event) -> None:
        received.append(event.text)

    adapter.handle_message = capture  # type: ignore[method-assign]
    return received


async def test_a_committed_restart_holds_new_inbound_messages(sched):
    adapter, _api, clock, _state = sched
    received = _capture(adapter)
    adapter._save_last_id(500)
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"

    await adapter._handle_inbound(_message(501))
    await asyncio.sleep(0.05)
    assert received == []
    # Not consumed: the cursor the next process polls from is unchanged,
    # and the id is not marked as dispatched.
    assert adapter._load_last_id() == 500
    assert 501 not in adapter._dispatched_inbound_ids
    assert not adapter._pending_text_tasks


async def test_intake_is_held_from_the_last_busy_check_on(sched, monkeypatch):
    """No await between the last busy check and the hold: a message that
    arrives while the attempt is recorded or the restart is spawned (both
    in worker threads) is already held."""
    adapter, _api, clock, state = sched
    seen: list[tuple[str, bool]] = []
    real_record = scheduled_update.record_attempt

    def record(path, target):
        seen.append(("record", adapter._intake_held()))
        return real_record(path, target)

    def spawn(supervisor):
        seen.append(("spawn", adapter._intake_held()))
        return True

    monkeypatch.setattr(scheduled_update, "record_attempt", record)
    monkeypatch.setattr(self_update, "schedule_supervisor_restart", spawn)
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    assert seen == [("record", True), ("spawn", True)]


async def test_a_message_still_on_its_way_in_keeps_the_install_staged(sched):
    """A message that arrived during the restart report and is still on its
    way to a session (stamped, not yet busy) is not cut off: the last look
    before the commit is the full safe moment, not only the busy check."""
    adapter, api, clock, state = sched

    async def heartbeat_while_a_message_arrives(**kwargs):
        api.heartbeats.append(kwargs)
        adapter._note_inbound_message()

    api.post_heartbeat = heartbeat_while_a_message_arrives  # type: ignore[method-assign]
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    assert state.restarts == []
    assert adapter._intake_held() is False


async def test_the_poll_does_not_consume_while_held(sched):
    adapter, api, clock, _state = sched
    adapter._save_last_id(500)
    api.inbound = [_message(501)]
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    await adapter._run_backfill(adapter._load_last_id())
    assert api.fetches == []
    assert adapter._load_last_id() == 500


async def test_a_run_that_stays_staged_holds_nothing(sched):
    adapter, _api, clock, state = sched
    received = _capture(adapter)

    def apply_then_a_turn_starts():
        adapter._active_sessions = {"bgos:1": object()}
        return AppliedUpdate(__version__, NEWER)

    state.apply_result = apply_then_a_turn_starts
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    adapter._active_sessions = {}
    await adapter._handle_inbound(_message(601), batchable=False)
    assert received == ["hello"]


async def test_a_failed_restart_spawn_reopens_intake(sched):
    adapter, _api, clock, state = sched
    received = _capture(adapter)
    state.spawn_ok = False
    assert await _idle_through_quiet_window(adapter, clock) == "error"
    assert adapter._intake_held() is False
    await adapter._handle_inbound(_message(701), batchable=False)
    assert received == ["hello"]


async def test_intake_reopens_when_the_restart_never_comes(sched):
    """Bounded: a kickstart that failed after its spawn leaves the process
    running. Intake reopens, and what was held is fetched again from the
    cursor it was held at, before a newer push can move the cursor past
    it."""
    adapter, api, clock, _state = sched
    received = _capture(adapter)
    adapter._save_last_id(500)
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    await adapter._handle_inbound(_message(501, "first"))
    assert received == []

    clock[0] += bgos_adapter_module._INTAKE_HOLD_SECONDS
    api.inbound = [_message(501, "first"), _message(502, "second")]
    await adapter._handle_inbound(_message(502, "second"))
    await asyncio.sleep(0.05)
    assert api.fetches == [500]
    assert received == ["first", "second"]
    assert adapter._load_last_id() == 502


async def test_held_intake_still_resolves_work_in_flight(sched, monkeypatch):
    """An approval answer resumes a turn that is already running; holding
    it would be cancelling work in flight."""
    adapter, _api, clock, _state = sched
    resolved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bgos_adapter_module, "resolve_gateway_approval",
        lambda session_key, choice: resolved.append((session_key, choice)),
    )
    monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda uid: True)
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    adapter._approval_state[5] = "bgos:42"
    await adapter._handle_callback({"callbackData": "ea:once:5", "userId": "u"})
    assert resolved == [("bgos:42", "once")]


async def test_a_click_starts_no_new_turn_while_held(sched):
    adapter, _api, clock, _state = sched
    received = _capture(adapter)
    assert await _idle_through_quiet_window(adapter, clock) == "restarting"
    await adapter._handle_inbound_click({
        "assistantId": 77, "chatId": 42, "messageId": 9, "userId": "u",
        "buttonText": "Yes", "callbackData": "opt_yes",
    })
    await asyncio.sleep(0.05)
    assert received == []


async def test_every_bgos_adapter_in_the_process_holds(sched):
    """One restart ends every multiplexed profile's adapter."""
    adapter, _api, clock, _state = sched
    other, _other_api = _new_adapter(clock)
    try:
        received = _capture(other)
        assert await _idle_through_quiet_window(adapter, clock) == "restarting"
        await other._handle_inbound(_message(801), batchable=False)
        assert received == []
    finally:
        await other._real_api.close()  # type: ignore[attr-defined]


async def test_update_now_holds_intake_once_its_restart_is_committed(sched):
    adapter, api, _clock, state = sched
    real_progress = api.post_update_rpc_progress
    held_at_restarting: list[bool] = []

    async def progress(rpc_id, **kwargs):
        if kwargs.get("stage") == "restarting":
            held_at_restarting.append(adapter._intake_held())
        await real_progress(rpc_id, **kwargs)

    api.post_update_rpc_progress = progress  # type: ignore[method-assign]
    await adapter._handle_update_rpc({"rpcId": "rpc-hold", "op": "update_now"})
    await asyncio.gather(*adapter._update_tasks, return_exceptions=True)
    assert state.restarts == [LAUNCHD]
    assert held_at_restarting == [True]
    assert adapter._intake_held() is True


async def test_update_now_spawn_failure_reopens_intake(sched):
    adapter, api, _clock, state = sched
    state.spawn_ok = False
    await adapter._handle_update_rpc({"rpcId": "rpc-fail", "op": "update_now"})
    await asyncio.gather(*adapter._update_tasks, return_exceptions=True)
    assert api.progresses[-1][1]["message"] == "restart_spawn_failed"
    assert adapter._intake_held() is False


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


async def test_an_update_now_accepted_during_the_probes_wins(sched, monkeypatch):
    """The tick awaits its probes in worker threads, and an update_now can
    be accepted meanwhile. The scheduled run must then stand down: never a
    second pull of the same clone or a second restart beside it."""
    adapter, _api, clock, state = sched
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET

    def pending_while_update_now_starts(clone_dir=None):
        adapter._update_rpc_in_flight.add("rpc-3")
        return state.pending

    monkeypatch.setattr(
        self_update, "pending_restart_version", pending_while_update_now_starts,
    )
    assert await _tick(adapter) == "update_in_flight"
    assert state.applied == [] and state.restarts == []
    assert adapter._scheduled_update_running is False
    adapter._update_rpc_in_flight.clear()


async def test_an_update_now_on_another_bgos_adapter_blocks(sched):
    """Multiplexed profiles share one clone and one process: an update_now
    on any BGOS adapter in it holds the scheduled apply off."""
    adapter, _api, clock, state = sched
    other, _other_api = _new_adapter(clock)
    try:
        bgos_adapter_module._register_live_adapter(other)
        other._update_rpc_in_flight.add("rpc-4")
        assert await _tick(adapter) == "update_in_flight"
        clock[0] += 10 * QUIET
        assert await _tick(adapter) == "update_in_flight"
        assert state.applied == [] and state.restarts == []
    finally:
        other._update_rpc_in_flight.clear()
        bgos_adapter_module._unregister_live_adapter(other)
        await other._real_api.close()  # type: ignore[attr-defined]


async def test_update_now_on_another_bgos_adapter_is_refused_during_a_scheduled_run(
    sched,
):
    adapter, _api, clock, state = sched
    other, other_api = _new_adapter(clock)
    try:
        # Both connected (connect registers each adapter); this one owns
        # the scheduled run.
        bgos_adapter_module._register_live_adapter(adapter)
        bgos_adapter_module._register_live_adapter(other)
        adapter._scheduled_update_running = True
        await other._handle_update_rpc({"rpcId": "rpc-5", "op": "update_now"})
        assert other_api.acks == ["rpc-5"]
        assert other_api.progresses[-1][1]["message"] == "update_in_flight"
        assert not other._update_tasks
    finally:
        adapter._scheduled_update_running = False
        await asyncio.gather(*other._update_tasks, return_exceptions=True)
        bgos_adapter_module._unregister_live_adapter(other)
        await other._real_api.close()  # type: ignore[attr-defined]
    assert state.applied == [] and state.restarts == []


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


def _inject_process_registry(monkeypatch, registry: Any) -> None:
    """Stand in for Hermes upstream's tools/process_registry.py (the module
    singleton the terminal tool registers background=true processes in)."""
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # a package, so the submodule import resolves
    module = types.ModuleType("tools.process_registry")
    module.process_registry = registry
    tools_pkg.process_registry = module
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.process_registry", module)


class FakeProcessRegistry:
    def __init__(self, running: int) -> None:
        self.running = running

    def count_running(self) -> int:
        return self.running


async def test_a_running_background_process_is_busy(sched, monkeypatch):
    """Finding 9: a process the terminal tool started with background=true
    dies with the gateway, so it holds the scheduled apply off for as long
    as it runs, and the idle window starts over when it ends."""
    adapter, _api, clock, state = sched
    registry = FakeProcessRegistry(running=1)
    _inject_process_registry(monkeypatch, registry)
    assert await _tick(adapter) == "background_job"
    clock[0] += 10 * QUIET
    assert await _tick(adapter) == "background_job"
    assert state.applied == [] and state.restarts == []
    registry.running = 0
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET
    assert await _tick(adapter) == "restarting"


async def test_a_background_process_starting_during_the_pull_keeps_it_staged(
    sched, monkeypatch,
):
    adapter, _api, clock, state = sched
    registry = FakeProcessRegistry(running=0)
    _inject_process_registry(monkeypatch, registry)

    def apply_then_a_job_starts():
        registry.running = 1
        return AppliedUpdate(__version__, NEWER)

    state.apply_result = apply_then_a_job_starts
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    assert state.restarts == []


async def test_a_registry_that_cannot_answer_is_busy(sched, monkeypatch):
    """Present but failing: unknown is unsafe (a guard that fails open would
    kill a live job), unlike an older Hermes that has no registry at all."""
    adapter, _api, _clock, state = sched

    class Broken:
        def count_running(self) -> int:
            raise RuntimeError("registry lock poisoned")

    _inject_process_registry(monkeypatch, Broken())
    assert await _tick(adapter) == "background_job"
    assert state.restarts == []


@pytest.mark.parametrize("missing", ["module", "method"])
async def test_an_older_hermes_without_the_registry_counts_zero_and_logs_once(
    sched, monkeypatch, caplog, missing,
):
    adapter, _api, clock, state = sched
    monkeypatch.setattr(
        bgos_adapter_module, "_background_registry_absent_logged", False,
    )
    if missing == "module":
        # A None entry makes the import raise ImportError, as on a Hermes
        # that predates tools/process_registry.py.
        monkeypatch.setitem(sys.modules, "tools.process_registry", None)
    else:
        _inject_process_registry(monkeypatch, object())
    caplog.set_level("INFO", logger=bgos_adapter_module.log.name)
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET
    assert await _tick(adapter) == "restarting"
    absent = [
        r for r in caplog.records if "process registry" in r.getMessage()
    ]
    assert len(absent) == 1


async def test_update_now_refuses_while_a_background_process_runs(
    sched, monkeypatch,
):
    adapter, api, _clock, state = sched
    monkeypatch.setattr(bgos_adapter_module, "_UPDATE_DRAIN_SECONDS", 0.2)
    _inject_process_registry(monkeypatch, FakeProcessRegistry(running=2))
    await adapter._handle_update_rpc({"rpcId": "rpc-bg", "op": "update_now"})
    await asyncio.gather(*adapter._update_tasks, return_exceptions=True)
    stages = [body["stage"] for _rpc, body in api.progresses]
    assert stages == ["draining", "error"]
    assert api.progresses[-1][1]["message"] == "background_job"
    assert state.applied == [] and state.restarts == []


async def test_another_platform_adapter_with_a_live_session_blocks(sched):
    """Best effort beyond BGOS: the gateway runner's other platform
    adapters (Telegram and friends) share the process the restart ends."""
    adapter, _api, clock, state = sched
    telegram = SimpleNamespace(_active_sessions={"telegram:1": object()})
    adapter.gateway_runner = SimpleNamespace(adapters={"telegram": telegram})
    assert await _tick(adapter) == "busy"
    telegram._active_sessions = {}
    assert await _tick(adapter) == "settling"


# Finding H3: the gateway runs agent work no platform adapter's sessions
# show. Cron jobs run on cron.scheduler's own thread pool in this process
# (tracked only by its get_running_job_ids, upstream #60432), API server runs
# and turns of secondary multiplexed profiles are in the runner's
# _active_work_count() / _running_agents, and a secondary profile's platform
# adapters live in runner._profile_adapters, not runner.adapters. A restart
# ends all of them.


def _inject_cron_scheduler(monkeypatch, get_running_job_ids) -> None:
    """Stand in for Hermes upstream's cron/scheduler.py, as the gateway
    process has it loaded."""
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    module = types.ModuleType("cron.scheduler")
    module.get_running_job_ids = get_running_job_ids
    cron_pkg.scheduler = module
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", module)


async def test_a_running_cron_job_holds_the_scheduled_apply(sched, monkeypatch):
    adapter, _api, clock, state = sched
    running = {"nightly-fixer"}
    _inject_cron_scheduler(monkeypatch, lambda: frozenset(running))
    assert await _tick(adapter) == "background_job"
    clock[0] += 10 * QUIET
    assert await _tick(adapter) == "background_job"
    assert state.applied == [] and state.restarts == []
    running.clear()
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET
    assert await _tick(adapter) == "restarting"


async def test_a_cron_job_starting_during_the_pull_keeps_it_staged(
    sched, monkeypatch,
):
    adapter, _api, clock, state = sched
    running: set[str] = set()
    _inject_cron_scheduler(monkeypatch, lambda: frozenset(running))

    def apply_then_a_cron_job_starts():
        running.add("nightly-fixer")
        return AppliedUpdate(__version__, NEWER)

    state.apply_result = apply_then_a_cron_job_starts
    assert await _idle_through_quiet_window(adapter, clock) == "staged"
    assert state.restarts == []


async def test_a_cron_scheduler_that_cannot_answer_is_busy(sched, monkeypatch):
    adapter, _api, _clock, state = sched

    def broken():
        raise RuntimeError("lock poisoned")

    _inject_cron_scheduler(monkeypatch, broken)
    assert await _tick(adapter) == "background_job"
    assert state.restarts == []


async def test_update_now_refuses_while_a_cron_job_runs(sched, monkeypatch):
    adapter, api, _clock, state = sched
    monkeypatch.setattr(bgos_adapter_module, "_UPDATE_DRAIN_SECONDS", 0.2)
    _inject_cron_scheduler(monkeypatch, lambda: frozenset({"nightly-fixer"}))
    await adapter._handle_update_rpc({"rpcId": "rpc-cron", "op": "update_now"})
    await asyncio.gather(*adapter._update_tasks, return_exceptions=True)
    assert api.progresses[-1][1]["message"] == "background_job"
    assert state.applied == [] and state.restarts == []


async def test_gateway_runner_work_is_busy(sched):
    """Upstream's own drain total: turns on every profile and API server
    runs (cron jobs, counted in it too, are reported apart)."""
    adapter, _api, clock, state = sched
    work = [1]
    adapter.gateway_runner = SimpleNamespace(
        adapters={}, _active_work_count=lambda: work[0],
    )
    assert await _tick(adapter) == "busy"
    clock[0] += 10 * QUIET
    assert await _tick(adapter) == "busy"
    work[0] = 0
    assert await _tick(adapter) == "settling"
    clock[0] += QUIET
    assert await _tick(adapter) == "restarting"
    assert len(state.restarts) == 1


async def test_a_cron_job_inside_the_runner_total_reads_as_background_job(
    sched, monkeypatch,
):
    adapter, _api, _clock, _state = sched
    _inject_cron_scheduler(monkeypatch, lambda: frozenset({"nightly-fixer"}))
    adapter.gateway_runner = SimpleNamespace(
        adapters={}, _active_work_count=lambda: 1,
    )
    assert await _tick(adapter) == "background_job"


async def test_an_older_runner_counts_its_running_agents(sched):
    adapter, _api, _clock, state = sched
    adapter.gateway_runner = SimpleNamespace(
        adapters={}, _running_agents={"telegram:shadow:1": object()},
    )
    assert await _tick(adapter) == "busy"
    assert state.restarts == []


async def test_a_secondary_profile_platform_session_is_busy(sched):
    adapter, _api, _clock, state = sched
    telegram = SimpleNamespace(_active_sessions={"telegram:9": object()})
    adapter.gateway_runner = SimpleNamespace(
        adapters={}, _profile_adapters={"shadow": {"telegram": telegram}},
    )
    assert await _tick(adapter) == "busy"
    telegram._active_sessions = {}
    assert await _tick(adapter) == "settling"


async def test_a_runner_that_cannot_count_its_work_is_busy(sched):
    adapter, _api, _clock, state = sched

    def broken() -> int:
        raise RuntimeError("runner torn down")

    adapter.gateway_runner = SimpleNamespace(adapters={}, _active_work_count=broken)
    assert await _tick(adapter) == "busy"
    assert state.restarts == []


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
