"""The adapter's REST poll with per-agent cursors: it starts from the lowest
known cursor, keeps each row only past ITS agent's cursor, and carries an idle
agent up to what the poll covered so it cannot pin the window."""
from __future__ import annotations

from typing import Any

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig


def _row(message_id: int, assistant_id: int) -> dict[str, Any]:
    return {
        "id": message_id,
        "message_id": message_id,
        "chat_id": 900,
        "text": "hello",
        "user_id": "user_1",
        "assistant_id": assistant_id,
        "message_type": "standard",
    }


class _Backend:
    """Serves one staged response per fetch and records the cursor asked for."""

    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.since_seen: list[int] = []

    async def fetch_inbound_since(self, since_message_id: int) -> dict[str, Any]:
        self.since_seen.append(since_message_id)
        return {"messages": self.responses.pop(0) if self.responses else []}


def _adapter(backend: _Backend, agents: tuple[int, ...] = (1, 2)) -> tuple[BGOSAdapter, list[tuple[int, int]]]:
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    for aid in agents:
        adapter._state.set_route(aid, f"route_{aid}")
    adapter._api = backend  # type: ignore[assignment]
    got: list[tuple[int, int]] = []

    async def capture(event: Any) -> None:
        got.append((int(event.agent_route.split("_")[1]), event.message_id))

    adapter.handle_message = capture  # type: ignore[method-assign]
    return adapter, got


async def test_polls_from_the_lowest_known_cursor_and_keeps_each_row_past_its_own_cursor() -> None:
    backend = _Backend([_row(490, 1), _row(490, 2), _row(510, 1)])
    adapter, got = _adapter(backend)
    adapter._poll_since()
    adapter._cursors.save(1, 500)  # agent 1 is ahead
    adapter._cursors.save(2, 480)

    await adapter._run_backfill(adapter._poll_since())

    assert backend.since_seen == [480]
    # (490, agent 1) is behind agent 1's cursor: already consumed, not replayed.
    assert sorted(got) == [(1, 510), (2, 490)]


async def test_an_idle_agent_is_carried_up_to_what_the_poll_covered() -> None:
    backend = _Backend([_row(600, 1)])
    adapter, _got = _adapter(backend)
    adapter._poll_since()
    adapter._cursors.save(1, 100)
    adapter._cursors.save(2, 100)

    await adapter._run_backfill(adapter._poll_since())

    # Agent 2 had no row up to 600: nothing for it is pending below that.
    assert adapter._cursors.get(2) == 600
    # Agent 1 consumed its own row.
    assert adapter._cursors.get(1) == 600


async def test_a_full_page_stops_the_idle_carry_one_below_its_last_id(monkeypatch) -> None:
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "0")
    backend = _Backend([_row(101 + i, 1) for i in range(200)])
    adapter, _got = _adapter(backend)
    adapter._poll_since()
    adapter._cursors.save(1, 100)
    adapter._cursors.save(2, 100)

    await adapter._run_backfill(adapter._poll_since())

    assert adapter._cursors.get(2) == 299  # last id 300, minus one: its other rows may be cut


async def test_the_storm_guard_fast_forwards_every_known_agent_and_skips_dispatch(monkeypatch) -> None:
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "2")
    backend = _Backend([_row(11, 1), _row(12, 2), _row(13, 1)])
    adapter, got = _adapter(backend)
    adapter._poll_since()
    adapter._cursors.save(1, 10)
    adapter._cursors.save(2, 10)

    await adapter._run_backfill(adapter._poll_since())

    assert got == []
    assert adapter._cursors.get(1) == 13
    assert adapter._cursors.get(2) == 13


async def test_an_agent_exposed_long_after_the_migration_does_not_replay_history() -> None:
    # Legacy cursor 3000; agent 1 has since consumed up to 90000.
    backend = _Backend([_row(3100 + i, 7) for i in range(30)] + [_row(90100, 1)])
    adapter, got = _adapter(backend, agents=(1,))
    adapter._cursors._legacy_path.write_text("3000")
    adapter._poll_since()
    adapter._cursors.save(1, 90000)
    # Agent 7 is exposed now. Its old rows (3100..3129) are history.
    adapter._state.set_route(7, "route_7")

    await adapter._run_backfill(adapter._poll_since())

    assert backend.since_seen == [90000]
    assert got == [(1, 90100)]


async def test_a_row_for_an_agent_not_known_is_dropped_as_before() -> None:
    backend = _Backend([_row(450, 9)])
    adapter, got = _adapter(backend, agents=(1,))

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 400)

    await adapter._run_backfill(adapter._poll_since())

    assert got == []


async def test_the_reconnect_backfill_starts_from_the_lowest_agent_cursor(monkeypatch) -> None:
    backend = _Backend()
    adapter, _got = _adapter(backend)
    adapter._poll_since()
    adapter._cursors.save(1, 700)
    adapter._cursors.save(2, 300)
    seen: list[int] = []

    async def record(since: int) -> None:
        seen.append(since)

    adapter._run_backfill = record  # type: ignore[method-assign]
    adapter._mission_lane.schedule_reconcile = lambda: None  # type: ignore[method-assign]

    adapter._on_reconnect(700)  # the highest id the socket saw
    import asyncio
    await asyncio.sleep(0)

    assert seen == [300]


async def test_an_upgraded_gateway_resumes_every_agent_from_the_old_single_cursor() -> None:
    backend = _Backend([_row(480, 1), _row(481, 2), _row(482, 1), _row(482, 2)])
    adapter, got = _adapter(backend)
    # The pre per-agent install left one integer in $HERMES_HOME/bgos_last_id.
    adapter._last_id_path().write_text("480")

    await adapter._run_backfill(adapter._poll_since())

    assert backend.since_seen == [480]  # nothing before 480 is replayed
    # (480, agent 1) is the old cursor itself: already consumed. Nothing newer
    # is skipped for either agent.
    assert sorted(got) == [(1, 482), (2, 481), (2, 482)]


async def test_an_agent_removed_and_exposed_again_rejoins_at_the_head() -> None:
    old = [_row(3100 + i, 2) for i in range(30)]
    backend = _Backend([], [], old + [_row(4000, 1)])
    adapter, got = _adapter(backend)
    adapter._poll_since()
    adapter._cursors.save(1, 100)
    adapter._cursors.save(2, 100)
    await adapter._run_backfill(adapter._poll_since())

    # Agent 2 is away while agent 1 moves on to 3500.
    adapter._state.remove_assistant(2)
    adapter._cursors.save(1, 3500)
    await adapter._run_backfill(adapter._poll_since())

    # Agent 2 is back; 30 messages arrived for it while away, plus a new one for agent 1.
    adapter._state.set_route(2, "route_2")
    await adapter._run_backfill(adapter._poll_since())

    assert backend.since_seen[-1] == 3500
    assert got == [(1, 4000)]


async def test_boot_with_no_agent_exposed_then_a_stale_agent_exposed_rejoins_at_the_head() -> None:
    old = [_row(200 + i, 2) for i in range(30)]
    backend = _Backend(old)
    adapter, got = _adapter(backend, agents=())
    # An earlier run left agent 2 behind agent 1.
    adapter._cursors.since([1, 2])
    adapter._cursors.save(1, 900)
    adapter._cursors.save(2, 100)

    adapter._poll_since()  # nothing exposed at boot
    adapter._state.set_route(1, "route_1")
    adapter._state.set_route(2, "route_2")  # exposed while running

    await adapter._run_backfill(adapter._poll_since())

    assert backend.since_seen == [900]
    assert got == []


async def test_a_first_message_to_a_newly_exposed_agent_is_delivered_when_the_refresh_misses_it() -> None:
    # Agent 1 is known and idle; agent 9 was just exposed and its first row (90)
    # is polled before the gateway has learned it (the refresh is rate limited).
    backend = _Backend([_row(90, 9)], [])
    adapter, got = _adapter(backend, agents=(1,))

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)

    await adapter._run_backfill(adapter._poll_since())
    assert got == []  # unknown for now; the idle agent was carried past it

    # The next poll no longer returns the row, so the replay itself has to ask
    # the pairing again; this time the refresh learns agent 9.
    async def learn_agent_9() -> None:
        adapter._state.set_route(9, "route_9")

    adapter._refresh_pairing_scope = learn_agent_9  # type: ignore[method-assign]
    await adapter._run_backfill(adapter._poll_since())

    # The window reaches back to the held row (one below it), and the replay has
    # already delivered it, so the fetch returns it once more and it is not
    # delivered twice.
    assert backend.since_seen == [80, 89]
    assert got == [(9, 90)]


async def test_a_restart_hold_that_starts_during_the_replay_keeps_the_rest_for_later() -> None:
    backend = _Backend([_row(90, 9), _row(91, 9)], [], [])
    adapter, got = _adapter(backend, agents=(1,))

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    await adapter._run_backfill(adapter._poll_since())
    assert {(9, 90), (9, 91)} == set(adapter._dropped_unknown)

    adapter._state.set_route(9, "route_9")
    held = [False]
    original = adapter._handle_inbound

    async def hold_after_first(data: dict, **kw: Any) -> None:
        await original(data, **kw)
        held[0] = True

    adapter._handle_inbound = hold_after_first  # type: ignore[method-assign]
    adapter._intake_held = lambda: held[0]  # type: ignore[method-assign]
    await adapter._run_backfill(adapter._poll_since())

    assert got == [(9, 90)]
    assert list(adapter._dropped_unknown) == [(9, 91)]  # kept, not lost
    assert backend.since_seen == [0]  # nothing was fetched after the hold began


async def test_a_failing_pairing_refresh_neither_kills_the_poll_nor_loses_the_row() -> None:
    backend = _Backend([_row(90, 9)], [], [])
    adapter, got = _adapter(backend, agents=(1,))

    async def broken_refresh() -> None:
        raise AttributeError("whoami returned an empty body")

    adapter._refresh_pairing_scope = broken_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)

    await adapter._run_backfill(adapter._poll_since())  # the row's own refresh raises
    assert (9, 90) in adapter._dropped_unknown  # remembered, not lost
    await adapter._run_backfill(adapter._poll_since())  # the replay's refresh raises: no crash
    assert len(backend.since_seen) == 2

    async def learn_agent_9() -> None:
        adapter._state.set_route(9, "route_9")

    adapter._refresh_pairing_scope = learn_agent_9  # type: ignore[method-assign]
    await adapter._run_backfill(adapter._poll_since())

    assert got == [(9, 90)]


async def test_rows_held_by_a_restart_are_not_carried_past() -> None:
    backend = _Backend([_row(100, 8)])
    adapter, _got = _adapter(backend, agents=(1,))
    adapter._poll_since()
    adapter._cursors.save(1, 50)
    original = adapter._handle_inbound
    held = [False]

    async def hold_on_row(data: dict, **kw: Any) -> None:
        held[0] = True  # the restart is committed as the row is handled
        await original(data, **kw)

    adapter._handle_inbound = hold_on_row  # type: ignore[method-assign]
    adapter._intake_held = lambda: held[0]  # type: ignore[method-assign]

    await adapter._replay_inbound_since(adapter._poll_since())

    assert adapter._cursors.get(1) == 50  # not carried past the held row


async def test_a_held_message_is_not_delivered_twice_and_expires() -> None:
    backend = _Backend([_row(90, 9)], [], [])
    adapter, got = _adapter(backend, agents=(1,))

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    now = [1000.0]
    adapter._clock = lambda: now[0]  # type: ignore[assignment]
    adapter._poll_since()
    await adapter._run_backfill(adapter._poll_since())
    assert (9, 90) in adapter._dropped_unknown

    now[0] += 3601  # waited too long
    await adapter._run_backfill(adapter._poll_since())
    assert (9, 90) not in adapter._dropped_unknown

    adapter._state.set_route(9, "route_9")
    await adapter._run_backfill(adapter._poll_since())
    assert got == []


async def test_a_live_push_during_the_fetch_does_not_hide_an_older_row_of_the_response() -> None:
    adapter, got = _adapter(_Backend())

    class _RacyBackend:
        since_seen: list[int] = []

        async def fetch_inbound_since(self, since_message_id: int) -> dict[str, Any]:
            # A live push for agent 1 lands while the fetch is in flight.
            adapter._cursors.save(1, 500)
            return {"messages": [_row(300, 1)]}

    adapter._api = _RacyBackend()  # type: ignore[assignment]
    adapter._poll_since()
    adapter._cursors.save(1, 100)

    await adapter._run_backfill(adapter._poll_since())

    assert got == [(1, 300)]


async def test_a_newly_exposed_agent_is_not_rebased_past_its_own_unconsumed_row() -> None:
    backend = _Backend([])
    adapter, _got = _adapter(backend, agents=(1,))
    adapter._poll_since()  # agent 1 known; baseline recorded

    adapter._state.set_route(2, "route_2")  # agent 2 newly exposed, never away
    adapter._cursors.save(2, 300)  # it consumed row 300; row 301 is still pending
    adapter._cursors.save(1, 301)  # a sibling moved the head on

    adapter._poll_since()

    assert adapter._cursors.get(2) == 300


async def test_a_row_an_unknown_agent_already_consumed_is_not_delivered_again() -> None:
    # Agent 4 consumed message 13 earlier, then was unexposed across a restart;
    # agent 2's cursor still lags, so the poll starts at 12 and returns (13, 4).
    backend = _Backend([_row(13, 4)])
    adapter, got = _adapter(backend, agents=(2,))
    adapter._cursors.since([2, 4])
    adapter._cursors.save(2, 12)
    adapter._cursors.save(4, 13)
    adapter._state.remove_assistant(4)  # not known when the poll's rows are filtered

    async def learn_agent_4() -> None:
        adapter._state.set_route(4, "route_4")  # the row's own refresh learns it

    adapter._refresh_pairing_scope = learn_agent_4  # type: ignore[method-assign]

    await adapter._run_backfill(12)

    assert got == []


async def test_a_row_above_an_unknown_agents_own_cursor_still_reaches_the_handler() -> None:
    backend = _Backend([_row(14, 4)])
    adapter, got = _adapter(backend, agents=(2,))
    adapter._cursors.since([2, 4])
    adapter._cursors.save(2, 12)
    adapter._cursors.save(4, 13)
    adapter._state.remove_assistant(4)

    async def learn_agent_4() -> None:
        adapter._state.set_route(4, "route_4")

    adapter._refresh_pairing_scope = learn_agent_4  # type: ignore[method-assign]

    await adapter._run_backfill(12)

    assert got == [(4, 14)]


async def test_an_agent_exposed_while_running_is_pinned_to_its_own_cursor_at_once() -> None:
    adapter, _got = _adapter(_Backend(), agents=(1,))
    adapter._poll_since()
    adapter._cursors.save(1, 10)  # head is 10

    async def whoami() -> dict[str, Any]:
        return {"assistants": [
            {"assistant_id": 1, "agent_route": "route_1"},
            {"assistant_id": 2, "agent_route": "route_2"},
        ]}

    async def no_sync(_aid: int) -> None:
        return None

    adapter._api.whoami = whoami  # type: ignore[method-assign]
    adapter.sync_commands_for = no_sync  # type: ignore[method-assign]

    assert await adapter._refresh_pairing_scope() is True

    adapter._cursors.save(1, 12)  # a sibling's live message moves the head on
    assert adapter._cursors.get(2) == 10  # pinned, not following the head


async def test_an_agent_pinned_while_the_fetch_is_in_flight_does_not_lose_its_row() -> None:
    adapter, got = _adapter(_Backend(), agents=(1,))
    adapter._poll_since()
    adapter._cursors.save(1, 100)

    class _SlowBackend:
        async def fetch_inbound_since(self, since_message_id: int) -> dict[str, Any]:
            # While the request is in flight: a sibling consumes a later id (head
            # 200) and agent 2 becomes known and is pinned at the head.
            adapter._cursors.save(1, 200)
            adapter._state.set_route(2, "route_2")
            adapter._cursors.since(adapter._known_assistant_ids())
            return {"messages": [_row(150, 2)]}

    adapter._api = _SlowBackend()  # type: ignore[assignment]

    await adapter._run_backfill(adapter._poll_since())

    assert got == [(2, 150)]


class _SinceBackend:
    """A poll that honours since_message_id, like the real backend."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.since_seen: list[int] = []

    async def fetch_inbound_since(self, since_message_id: int) -> dict[str, Any]:
        self.since_seen.append(since_message_id)
        return {"messages": sorted(
            (r for r in self.rows if r["message_id"] > since_message_id),
            key=lambda r: r["message_id"],
        )[:200]}  # the real backend's LIMIT 200


async def test_a_row_held_for_an_unknown_agent_survives_a_restart_before_it_is_bound() -> None:
    backend = _SinceBackend([_row(150, 9)])

    # Process 1: agent 1 is known and idle; agent 9's first row arrives before
    # the gateway knows agent 9 (the pairing refresh is on cooldown).
    first, first_got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    first._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    first._poll_since()
    first._cursors.save(1, 80)
    await first._run_backfill(first._poll_since())
    assert first_got == []  # held, not delivered

    # The gateway restarts before agent 9 is bound; the new process shares the
    # state directory. Agent 9 is bound this time.
    second, second_got = _adapter(backend, agents=(1, 9))  # type: ignore[arg-type]
    await second._run_backfill(second._poll_since())

    assert second_got == [(9, 150)]


async def test_the_idle_carry_stops_below_a_kept_row_of_an_unknown_agent() -> None:
    backend = _SinceBackend([_row(150, 9)])
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)

    await adapter._run_backfill(adapter._poll_since())

    assert adapter._cursors.get(1) == 149
    assert adapter._cursors.head() == 149  # the head is capped below the row too


async def test_a_live_row_for_a_known_agent_cannot_carry_the_cursors_past_a_held_row_across_a_restart() -> None:
    backend = _SinceBackend([_row(150, 9), _row(200, 1)])
    first, first_got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    first._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    first._poll_since()
    first._cursors.save(1, 80)
    # Agent 9's row (150) is polled and held; agent 1 then gets a live push (200).
    await first._run_backfill(first._poll_since())
    await first._handle_inbound(_row(200, 1), batchable=False)
    assert (1, 200) in first_got

    # Restart before agent 9 is bound.
    second, second_got = _adapter(backend, agents=(1, 9))  # type: ignore[arg-type]
    await second._run_backfill(second._poll_since())

    assert (9, 150) in second_got


async def test_rows_above_a_held_row_that_known_agents_consumed_are_not_a_storm_after_a_restart() -> None:
    backend = _SinceBackend([_row(150, 9)])
    first, _first_got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    first._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    first._poll_since()
    first._cursors.save(1, 80)
    await first._run_backfill(first._poll_since())  # holds agent 9's row 150

    # Agent 1 then consumes 30 rows above it (live pushes; the backend has them).
    for i in range(30):
        row = _row(151 + i, 1)
        backend.rows.append(row)
        await first._handle_inbound(row, batchable=False)
    assert first._cursors.get(1) == 180

    # Restart; agent 9 is bound now. The window (from 149) holds 31 rows, but the
    # 30 agent 1 already consumed are filtered by its own cursor: not a storm.
    second, second_got = _adapter(backend, agents=(1, 9))  # type: ignore[arg-type]
    await second._run_backfill(second._poll_since())

    assert second_got == [(9, 150)]


async def test_a_row_served_again_after_the_hold_expired_does_not_start_a_new_hour(monkeypatch) -> None:
    import time as _time

    backend = _SinceBackend([_row(150, 9)])
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)
    await adapter._run_backfill(adapter._poll_since())  # served and held
    assert adapter._cursors.lowest_pending() == 150

    real = _time.time()
    monkeypatch.setattr("hermes_channel_bgos.cursors.time.time", lambda: real + 3700)
    adapter._cursors.release_held(9, 150)  # (the memory copy is gone too)
    adapter._cursors.save(1, 300)
    await adapter._run_backfill(adapter._poll_since())  # served again, hold expired

    assert adapter._cursors.lowest_pending() is None  # not a new hour
    assert adapter._cursors.since([1]) == 300  # the window is released


async def test_a_duplicate_of_a_consumed_row_releases_its_pending_record() -> None:
    adapter, got = _adapter(_Backend(), agents=(1, 9))
    adapter._cursors.record_pending(9, 150)
    await adapter._handle_inbound(_row(150, 9), batchable=False)  # delivered
    adapter._cursors.record_pending(9, 150)  # a late push records it again
    # (record_pending ignores a row already recorded, so release is observable)
    await adapter._handle_inbound(_row(150, 9), batchable=False)  # duplicate

    assert got == [(9, 150)]
    assert adapter._cursors.lowest_pending() is None


async def test_an_expired_hold_no_longer_caps_the_idle_carry_so_the_poll_does_not_stall(monkeypatch) -> None:
    import time as _time

    rows = [_row(100, 9)] + [_row(101 + i, 1) for i in range(300)]
    backend = _SinceBackend(rows)
    adapter, got = _adapter(backend, agents=(1, 2))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "0")
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.save(2, 99)
    await adapter._run_backfill(adapter._poll_since())  # holds 100

    real = _time.time()
    monkeypatch.setattr("hermes_channel_bgos.cursors.time.time", lambda: real + 7200)
    for _ in range(4):  # two hours on: the hold has ended
        await adapter._run_backfill(adapter._poll_since())

    # Agent 2 (idle) is carried past the given-up row and the window moves on.
    assert adapter._cursors.get(2) > 100
    assert adapter._cursors.since([1, 2]) > 100


async def test_held_rows_do_not_count_toward_the_storm_limit_and_a_known_agents_message_is_delivered() -> None:
    backend = _SinceBackend([_row(100 + i, 9) for i in range(30)] + [_row(500, 1)])
    adapter, got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)

    await adapter._run_backfill(adapter._poll_since())

    assert got == [(1, 500)]  # 30 held rows of an unknown agent are not a storm


async def test_a_storm_skip_still_records_the_rows_held_for_an_unknown_agent() -> None:
    rows = [_row(100 + i, 1) for i in range(30)] + [_row(200, 9)]
    backend = _SinceBackend(rows)
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 80)

    await adapter._run_backfill(adapter._poll_since())  # 30 known rows: a storm

    assert adapter._cursors.lowest_pending() == 200  # the unknown agent's row is kept


async def test_a_held_row_served_again_is_not_new_activity_for_the_self_update() -> None:
    backend = _SinceBackend([_row(150, 9)])
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)
    await adapter._run_backfill(adapter._poll_since())
    noted = [0]
    adapter._note_inbound_message = lambda: noted.__setitem__(0, noted[0] + 1)  # type: ignore[method-assign]

    await adapter._run_backfill(adapter._poll_since())  # served again

    assert noted[0] == 0


async def test_a_held_row_is_delivered_even_if_a_later_row_moved_the_agents_cursor_first() -> None:
    backend = _SinceBackend([_row(100, 9), _row(120, 9)])
    adapter, got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)
    backend.rows = [_row(100, 9)]
    await adapter._run_backfill(adapter._poll_since())  # holds 100
    assert adapter._cursors.lowest_pending() == 100

    # Agent 9 is bound and a LATER row (120) is consumed by a live push first.
    adapter._state.set_route(9, "route_9")
    adapter._cursors.since(adapter._known_assistant_ids())
    await adapter._handle_inbound(_row(120, 9), batchable=False)
    assert adapter._cursors.get(9) == 120
    adapter._dropped_unknown.clear()  # the process dies before the memory replay

    # After the restart the poll still hands over row 100 (its record is live) and
    # it is delivered, though agent 9's cursor is past it.
    second, second_got = _adapter(backend, agents=(1, 9))  # type: ignore[arg-type]
    await second._run_backfill(second._poll_since())

    assert (9, 100) in second_got
    assert second._cursors.lowest_pending() is None


async def test_more_unknown_rows_than_the_record_bound_do_not_stall_the_poll(monkeypatch) -> None:
    import time as _time

    held = [_row(100 + i, 9) for i in range(60)]  # more than the 50 records kept
    rest = [_row(200 + i, 1) for i in range(300)]
    backend = _SinceBackend(held + rest)
    adapter, got = _adapter(backend, agents=(1, 2))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "0")
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.save(2, 99)

    real = _time.time()
    for hour in range(4):
        monkeypatch.setattr(
            "hermes_channel_bgos.cursors.time.time", lambda h=hour: real + 3700 * h,
        )
        for _ in range(4):
            await adapter._run_backfill(adapter._poll_since())

    # Every row of agent 1 got through and the idle agent was carried on.
    assert len([g for g in got if g[0] == 1]) == 300
    assert adapter._cursors.get(2) > 200


async def test_a_row_with_no_assistant_id_cannot_pin_the_window(monkeypatch) -> None:
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "0")
    backend = _SinceBackend(
        [{**_row(100, 9), "assistant_id": None}] + [_row(101 + i, 1) for i in range(250)]
    )
    adapter, got = _adapter(backend, agents=(1, 2))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.save(2, 99)

    for _ in range(4):
        await adapter._run_backfill(adapter._poll_since())

    assert len([g for g in got if g[0] == 1]) == 250


async def test_an_agent_exposed_with_a_backlog_of_held_rows_is_not_a_storm_loop(monkeypatch) -> None:
    held = [_row(100 + i, 9) for i in range(30)]
    backend = _SinceBackend(held)
    first, _first_got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    first._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    first._poll_since()
    first._cursors.save(1, 80)
    await first._run_backfill(first._poll_since())  # holds 30 rows of agent 9
    assert first._cursors.lowest_pending() == 100

    # Restart (the memory copy is gone); agent 9 is exposed now, and agent 1 has
    # new rows. 30 held plus 3 new is over the storm limit of 25.
    backend.rows += [_row(500 + i, 1) for i in range(3)]
    second, second_got = _adapter(backend, agents=(1, 9))  # type: ignore[arg-type]
    await second._run_backfill(second._poll_since())

    assert sorted(g for g in second_got if g[0] == 9) == [(9, 100 + i) for i in range(30)]
    assert sorted(g for g in second_got if g[0] == 1) == [(1, 500), (1, 501), (1, 502)]


async def test_a_held_row_does_not_anchor_the_poll_window_while_this_process_holds_it(monkeypatch) -> None:
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "0")
    rows = [_row(100, 9)] + [_row(101 + i, 1) for i in range(450)]
    backend = _SinceBackend(rows)
    adapter, got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 99)

    for _ in range(5):
        await adapter._run_backfill(adapter._poll_since())

    # All 450 rows of the known agent arrive: the poll is not stuck behind the
    # first 200 rows above the held one. The row stays held (and recorded) for
    # a restart, which reaches back to it.
    assert len([g for g in got if g[0] == 1]) == 450
    assert adapter._cursors.lowest_pending() == 100
    restarted, _restarted_got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]
    assert restarted._cursors.since([1]) == 99


async def test_an_idle_known_agent_does_not_keep_the_window_anchored_at_a_held_row(monkeypatch) -> None:
    # Agent 1 is busy, agent 2 is idle, agent 9 (never exposed) has one row.
    monkeypatch.setenv("BGOS_BACKFILL_STORM_LIMIT", "0")
    rows = [_row(100, 9)] + [_row(101 + i, 1) for i in range(450)]
    backend = _SinceBackend(rows)
    adapter, got = _adapter(backend, agents=(1, 2))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.save(2, 99)

    for _ in range(6):
        await adapter._run_backfill(adapter._poll_since())

    assert len([g for g in got if g[0] == 1]) == 450  # nothing stuck behind 200 rows
    assert adapter._cursors.get(2) > 400  # the idle agent moved on too


async def test_a_record_left_by_an_earlier_process_for_a_row_no_longer_served_stops_anchoring() -> None:
    backend = _SinceBackend([_row(300 + i, 1) for i in range(10)])  # row (9, 100) is gone
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.record_pending(9, 100)  # an earlier process left it on disk
    assert adapter._cursors.since([1]) == 99

    await adapter._run_backfill(adapter._poll_since())  # the fetch spans id 100

    assert adapter._cursors.lowest_pending() is None
    assert adapter._cursors.since([1]) > 100


async def test_a_record_for_a_row_cut_off_by_the_page_limit_is_kept() -> None:
    backend = _SinceBackend([_row(150 + i, 1) for i in range(300)])
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.record_pending(9, 400)  # beyond what a full page of 200 reaches

    await adapter._run_backfill(adapter._poll_since())

    assert adapter._cursors.lowest_pending() == 400  # not proven gone


async def test_the_cursor_is_saved_before_the_pending_record_is_cleared() -> None:
    adapter, _got = _adapter(_Backend(), agents=(1, 9))
    adapter._cursors.record_pending(9, 150)
    order: list[str] = []
    real_save = adapter._cursors.save
    real_clear = adapter._cursors.clear_pending
    adapter._cursors.save = lambda a, m: (order.append("save"), real_save(a, m))[1]  # type: ignore[method-assign]
    adapter._cursors.clear_pending = lambda a, m: (order.append("clear"), real_clear(a, m))[1]  # type: ignore[method-assign]

    await adapter._handle_inbound(_row(150, 9), batchable=False)

    assert order == ["save", "clear"]


async def test_a_failed_replay_makes_the_window_reach_back_to_the_record_again() -> None:
    backend = _SinceBackend([_row(150, 9)])
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)
    await adapter._run_backfill(adapter._poll_since())  # held in memory and recorded
    adapter._cursors.save(1, 300)
    assert adapter._cursors.since([1]) == 300  # this process holds it: no anchor

    adapter._state.set_route(9, "route_9")

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("dispatch failed before the record was cleared")

    adapter._handle_inbound = boom  # type: ignore[method-assign]
    await adapter._replay_dropped_unknown()  # pops the memory copy, the call fails

    assert adapter._cursors.since([1]) == 149  # reaches back to the record again


class _Killed(BaseException):
    """The process stops here (not an Exception: nothing in the adapter catches it)."""


async def test_a_row_of_an_unknown_agent_survives_a_kill_during_the_pairing_refresh() -> None:
    # One message addressed to known agent 1 and unknown agent 9: two rows, one id.
    backend = _SinceBackend([_row(100, 1), _row(100, 9)])
    first, _first_got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def killed_refresh() -> None:
        raise _Killed()

    first._refresh_pairing_scope = killed_refresh  # type: ignore[method-assign]
    first._poll_since()
    first._cursors.save(1, 80)
    with pytest.raises(_Killed):
        await first._run_backfill(first._poll_since())
    assert first._cursors.get(1) == 100  # agent 1's row was consumed first

    # The new process has agent 9 bound; its row must still be fetched.
    second, second_got = _adapter(backend, agents=(1, 9))  # type: ignore[arg-type]
    await second._run_backfill(second._poll_since())

    assert second_got == [(9, 100)]


async def test_a_push_for_an_unknown_agent_is_recorded_while_a_restart_holds_intake() -> None:
    adapter, got = _adapter(_Backend(), agents=(1,))
    adapter._intake_held = lambda: True  # type: ignore[method-assign]

    await adapter._handle_inbound(_row(100, 9), batchable=False)

    assert got == []
    assert adapter._cursors.lowest_pending() == 100  # the restart will find it


async def test_a_storm_skip_still_hands_over_a_bound_agents_held_row() -> None:
    backend = _SinceBackend([_row(100, 9)])
    first, _first_got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    first._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    first._poll_since()
    first._cursors.save(1, 80)
    await first._run_backfill(first._poll_since())  # holds (9, 100)

    # Downtime: agent 1 has a backlog of 30 rows, and agent 9 is bound now.
    backend.rows += [_row(101 + i, 1) for i in range(30)]
    second, second_got = _adapter(backend, agents=(1, 9))  # type: ignore[arg-type]
    await second._run_backfill(second._poll_since())

    assert second_got == [(9, 100)]  # the backlog is skipped, the held row is not
    assert second._cursors.lowest_pending() is None


async def test_an_empty_or_malformed_body_does_not_release_the_records() -> None:
    class _BadBody:
        async def fetch_inbound_since(self, since_message_id: int) -> Any:
            return None  # a 200 with an empty body

    adapter, _got = _adapter(_BadBody(), agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.record_pending(9, 100)  # an earlier process left it

    await adapter._run_backfill(adapter._poll_since())

    assert adapter._cursors.lowest_pending() == 100


async def test_a_storm_skip_on_the_poll_that_first_sees_an_unknown_agents_row_still_holds_it() -> None:
    rows = [_row(100 + i, 1) for i in range(30)] + [_row(200, 9)]
    backend = _SinceBackend(rows)
    adapter, got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]

    async def no_refresh() -> None:
        return None

    adapter._refresh_pairing_scope = no_refresh  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)

    await adapter._run_backfill(adapter._poll_since())  # 30 known rows: a storm

    # The row recorded on this very poll is handed to the handler (held in memory)
    # and not skipped with the backlog.
    assert (9, 200) in adapter._dropped_unknown
    adapter._state.set_route(9, "route_9")
    await adapter._run_backfill(adapter._poll_since())
    assert (9, 200) in got


async def test_an_empty_list_body_does_not_release_the_records_either() -> None:
    class _EmptyList:
        async def fetch_inbound_since(self, since_message_id: int) -> Any:
            return {"messages": []}

    adapter, _got = _adapter(_EmptyList(), agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.record_pending(9, 100)  # an earlier process left it

    await adapter._run_backfill(adapter._poll_since())

    assert adapter._cursors.lowest_pending() == 100


async def test_a_list_of_non_dict_items_does_not_release_the_records() -> None:
    class _Junk:
        async def fetch_inbound_since(self, since_message_id: int) -> Any:
            return {"messages": [1, 2]}

    adapter, _got = _adapter(_Junk(), agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.record_pending(9, 100)

    await adapter._run_backfill(adapter._poll_since())

    assert adapter._cursors.lowest_pending() == 100


async def test_a_record_is_held_from_the_moment_it_is_written_so_a_concurrent_poll_keeps_it() -> None:
    adapter, _got = _adapter(_Backend(), agents=(1,))
    seen: list[set] = []

    async def inspecting_refresh() -> None:
        seen.append(adapter._cursors.unheld_pending_set())  # during the refresh await

    adapter._refresh_pairing_scope = inspecting_refresh  # type: ignore[method-assign]

    await adapter._handle_inbound(_row(100, 9), batchable=False)

    assert seen == [set()]  # recorded, and already held: not open to a stale release


async def test_a_storm_skip_on_a_full_page_stops_below_the_last_id_so_a_cut_sibling_row_stays_reachable() -> None:
    rows = [_row(101 + i, 1) for i in range(199)] + [_row(300, 1), _row(300, 9)]
    backend = _SinceBackend(rows)  # the real LIMIT 200 cuts (9, 300)
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 100)

    await adapter._run_backfill(adapter._poll_since())  # 200 known rows: a storm

    assert adapter._cursors.get(1) == 299  # not 300: the next poll still fetches id 300


async def test_rows_recorded_before_the_dispatch_loop_are_not_marked_held_until_a_handler_reaches_them() -> None:
    backend = _SinceBackend([_row(100, 9), _row(101, 8)])
    adapter, _got = _adapter(backend, agents=(1,))  # type: ignore[arg-type]
    seen: list[set] = []

    async def spy(data: dict, **_k: Any) -> None:
        seen.append(adapter._cursors.unheld_pending_set())

    adapter._handle_inbound = spy  # type: ignore[method-assign]
    adapter._poll_since()
    adapter._cursors.save(1, 80)

    await adapter._run_backfill(adapter._poll_since())

    # When the first handler runs, both rows are recorded and still reachable by a
    # restart (and by the intake resume), not hidden behind a mark with no memory copy.
    assert seen[0] == {(9, 100), (8, 101)}


async def test_a_dict_row_without_an_id_does_not_release_the_records() -> None:
    class _NoId:
        async def fetch_inbound_since(self, since_message_id: int) -> Any:
            return {"messages": [{"foo": 1}]}

    adapter, _got = _adapter(_NoId(), agents=(1,))  # type: ignore[arg-type]
    adapter._poll_since()
    adapter._cursors.save(1, 99)
    adapter._cursors.record_pending(9, 100)

    await adapter._run_backfill(adapter._poll_since())

    assert adapter._cursors.lowest_pending() == 100
