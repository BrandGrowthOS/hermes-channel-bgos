"""A revoked pairing must stop being replayed.

The backend answers a revoked pairing token with a plain 401 "Invalid pairing
token" and no error code. The poll loop used to swallow that every 5 s with a
traceback, forever (about 17,000 refused calls a day). Now N consecutive 401s
pause polling, one slow probe keeps asking, and the first good answer resumes.

The adapter tests run the real `_poll_loop` against an httpx MockTransport with
a fake clock that moves 5 s per request, so 30 refusals look like 150 s.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from hermes_channel_bgos.auth_rejection import (
    AUTH_REJECTION_MIN_COUNT,
    AUTH_REJECTION_MIN_SECONDS,
    PAUSED,
    RESUMED,
    AuthRejectionTracker,
)
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.bgos_ws import BgosWs
from hermes_channel_bgos.config import BgosConfig

INBOUND = "/api/v1/integrations/inbound"
ME = "/api/v1/integrations/me"
HEARTBEAT = "/api/v1/integrations/heartbeat"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


def _tracker():
    clock = _Clock()
    return AuthRejectionTracker(clock=clock), clock


def test_enough_401s_over_enough_time_pause():
    t, clock = _tracker()
    outcomes = []
    for _ in range(AUTH_REJECTION_MIN_COUNT):
        outcomes.append(t.observe(401))
        clock.now += AUTH_REJECTION_MIN_SECONDS / 10
    assert outcomes.count(PAUSED) == 1
    assert t.paused


def test_a_burst_of_401s_without_the_duration_does_not_pause():
    t, _clock = _tracker()
    for _ in range(AUTH_REJECTION_MIN_COUNT * 3):
        assert t.observe(401) is None
    assert not t.paused


def test_a_single_401_then_200_never_pauses():
    t, clock = _tracker()
    assert t.observe(401) is None
    clock.now += 10_000  # even a long gap: the run is over
    assert t.observe(200) is None
    assert t.consecutive == 0
    for _ in range(AUTH_REJECTION_MIN_COUNT - 1):
        clock.now += 10
        assert t.observe(401) is None
    assert not t.paused


def test_a_good_answer_in_the_middle_resets_the_run():
    t, clock = _tracker()
    for _ in range(AUTH_REJECTION_MIN_COUNT - 1):
        clock.now += 10
        t.observe(401)
    t.observe(200)
    for _ in range(AUTH_REJECTION_MIN_COUNT - 1):
        clock.now += 10
        assert t.observe(401) is None
    assert not t.paused


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_only_401_counts_as_a_rejection(status):
    t, clock = _tracker()
    for _ in range(AUTH_REJECTION_MIN_COUNT * 2):
        clock.now += 10
        assert t.observe(status) is None
    assert not t.paused and t.consecutive == 0


def _paused_tracker():
    t, clock = _tracker()
    for _ in range(AUTH_REJECTION_MIN_COUNT):
        clock.now += 10
        t.observe(401)
    assert t.paused
    return t, clock


def test_a_later_good_answer_resumes():
    t, _clock = _paused_tracker()
    assert t.observe(401) is None  # still refused: still paused, no new event
    assert t.observe(200) == RESUMED
    assert not t.paused and t.consecutive == 0


def test_a_304_counts_as_a_good_answer():
    t, _clock = _paused_tracker()
    assert t.observe(304) == RESUMED


def test_a_5xx_while_paused_proves_nothing():
    t, _clock = _paused_tracker()
    assert t.observe(502) is None
    assert t.paused


def test_force_pause_is_idempotent():
    t, _clock = _tracker()
    assert t.force_pause() is True
    assert t.force_pause() is False
    assert t.observe(200) == RESUMED


# ---------------------------------------------------------------------------
# Adapter poll loop
# ---------------------------------------------------------------------------


class _Backend:
    """What the pairing token gets answered with, and what was asked."""

    def __init__(self, clock: _Clock, step: float = 5.0) -> None:
        self.refuse = True
        self.clock = clock
        self.step = step
        self.paths: list[str] = []

    def count(self, path: str) -> int:
        return self.paths.count(path)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.clock.now += self.step
        if self.refuse:
            return httpx.Response(
                401, json={"statusCode": 401, "message": "Invalid pairing token"},
            )
        if request.url.path == ME:
            return httpx.Response(200, json={"pairing_id": 42, "assistants": []})
        if request.url.path == INBOUND:
            return httpx.Response(200, json={"messages": []})
        return httpx.Response(200, json={})


def _adapter(backend: _Backend) -> BGOSAdapter:
    adapter = BGOSAdapter(BgosConfig(base_url="http://backend.test", pairing_token="pair_xyz"))
    adapter._clock = backend.clock
    adapter._auth_probe_seconds = 0.05
    adapter._api._client = httpx.AsyncClient(
        base_url="http://backend.test", transport=httpx.MockTransport(backend),
    )
    return adapter


async def _until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_revoked_pairing_pauses_polling_then_a_200_resumes(caplog, monkeypatch):
    from hermes_channel_bgos import self_update

    # Resuming sends a heartbeat at once; keep it off the network.
    monkeypatch.setattr(self_update, "latest_known_version", lambda: None)
    monkeypatch.setattr(self_update, "update_readiness", lambda: {})
    caplog.set_level(logging.DEBUG)
    backend = _Backend(_Clock())
    adapter = _adapter(backend)
    task = asyncio.create_task(adapter._poll_loop(0.001))
    try:
        await _until(lambda: adapter._auth.paused)
        # Let the loop run past the pause: it must stop polling the inbound
        # endpoint and only probe, slowly.
        inbound_at_pause = backend.count(INBOUND)
        await asyncio.sleep(0.4)
        assert backend.count(INBOUND) == inbound_at_pause
        assert 1 <= backend.count(ME) <= 12  # ~8 probes of 0.05 s, not ~400 polls

        # ONE clear line for the whole episode, no traceback per call.
        loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(loud) == 1, [r.getMessage() for r in loud]
        assert "pairing token" in loud[0].getMessage()
        assert loud[0].exc_info is None

        # The backend accepts the token again: the next probe resumes polling.
        backend.refuse = False
        await _until(lambda: not adapter._auth.paused)
        await _until(lambda: backend.count(INBOUND) > inbound_at_pause)
        assert any("resumed" in r.getMessage() for r in caplog.records)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_single_401_then_200_keeps_polling_at_full_rate():
    backend = _Backend(_Clock())
    adapter = _adapter(backend)
    task = asyncio.create_task(adapter._poll_loop(0.001))
    try:
        await _until(lambda: backend.count(INBOUND) >= 1)
        backend.refuse = False
        first = backend.count(INBOUND)
        await _until(lambda: backend.count(INBOUND) > first + 20)
        assert not adapter._auth.paused
        assert backend.count(ME) == 0  # never fell back to probing
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_other_backfill_callers_do_not_hit_a_paused_backend():
    backend = _Backend(_Clock())
    adapter = _adapter(backend)
    adapter._auth.force_pause()
    await adapter._run_backfill(0)  # e.g. the WebSocket reconnect path
    assert backend.paths == []


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_401s_count_toward_the_pause_and_a_paused_beat_is_skipped(
    monkeypatch,
):
    from hermes_channel_bgos import self_update

    monkeypatch.setattr(self_update, "latest_known_version", lambda: None)
    monkeypatch.setattr(self_update, "update_readiness", lambda: {})
    backend = _Backend(_Clock(), step=10.0)
    adapter = _adapter(backend)
    adapter._wait_heartbeat_write_window = lambda: asyncio.sleep(0)  # type: ignore[assignment]
    for _ in range(AUTH_REJECTION_MIN_COUNT):
        await adapter._post_heartbeat_once()
        if adapter._auth.paused:
            break
    assert adapter._auth.paused
    sent = backend.count(HEARTBEAT)
    await adapter._post_heartbeat_once()
    assert backend.count(HEARTBEAT) == sent


@pytest.mark.asyncio
async def test_a_good_heartbeat_resets_a_refusal_run(monkeypatch):
    from hermes_channel_bgos import self_update

    monkeypatch.setattr(self_update, "latest_known_version", lambda: None)
    monkeypatch.setattr(self_update, "update_readiness", lambda: {})
    backend = _Backend(_Clock())
    adapter = _adapter(backend)
    await adapter._post_heartbeat_once()  # refused once
    assert adapter._auth.consecutive == 1
    backend.refuse = False
    await adapter._post_heartbeat_once()
    assert adapter._auth.consecutive == 0 and not adapter._auth.paused


# ---------------------------------------------------------------------------
# WebSocket pairing_revoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_revoked_event_pauses_at_once(caplog):
    caplog.set_level(logging.WARNING)
    adapter = _adapter(_Backend(_Clock()))
    adapter.pairing_id = 42
    await adapter._handle_pairing_revoked({"pairingId": 42})
    assert adapter._auth.paused
    assert len([r for r in caplog.records if "revoked" in r.getMessage()]) == 1
    await adapter._handle_pairing_revoked({"pairingId": 42})  # a repeat is quiet
    assert len([r for r in caplog.records if "revoked" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_pairing_revoked_for_another_pairing_is_ignored():
    adapter = _adapter(_Backend(_Clock()))
    adapter.pairing_id = 42
    await adapter._handle_pairing_revoked({"pairingId": 99})
    assert not adapter._auth.paused


@pytest.mark.asyncio
async def test_bgos_ws_routes_the_pairing_revoked_event():
    seen = []

    async def on_revoked(data):
        seen.append(data)

    async def noop(data):
        return None

    ws = BgosWs(
        BgosConfig(base_url="http://backend.test", pairing_token="pair_xyz"),
        on_inbound_message=noop, on_callback_result=noop,
        on_pairing_revoked=on_revoked,
    )
    handler = ws._sio.handlers["/"]["pairing_revoked"]
    await handler({"pairingId": 42})
    assert seen == [{"pairingId": 42}]
