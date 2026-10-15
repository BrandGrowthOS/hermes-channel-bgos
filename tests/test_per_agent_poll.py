"""The adapter's REST poll with per-agent cursors: it starts from the lowest
known cursor, keeps each row only past ITS agent's cursor, and carries an idle
agent up to what the poll covered so it cannot pin the window."""
from __future__ import annotations

from typing import Any

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

    assert backend.since_seen == [80, 90]  # the row was not fetched again
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

    now[0] += 601  # waited too long
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
