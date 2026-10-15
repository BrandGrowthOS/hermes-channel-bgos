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
