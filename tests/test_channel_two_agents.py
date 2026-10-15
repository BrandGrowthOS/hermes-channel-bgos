"""Two agents on ONE gateway, a channel message addressed to both.

The backend delivers it as one poll row per agent, both with the SAME message
id. Each agent must get its own copy: through the live poll, and across a
restart that lands between the two agents' turns (a single gateway cursor moved
past the id on the first agent's row and the second agent's row was never
fetched again).

This file only uses names that exist on both sides of the per-agent cursor
change, so the same tests run red on the old code and green on the new code.
"""
from __future__ import annotations

from typing import Any

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig

AGENT_A = 11
AGENT_B = 12


def _row(message_id: int, assistant_id: int) -> dict[str, Any]:
    """A row the way `GET /integrations/inbound` serves it: one per (message, agent)."""
    return {
        "id": message_id,
        "message_id": message_id,
        "chat_id": 900,
        "text": "@a @b what do you think",
        "user_id": "user_1",
        "assistant_id": assistant_id,
        "message_type": "standard",
    }


class _PollBackend:
    """The poll: every row with id > since_message_id, ASC by id."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.since_seen: list[int] = []

    async def fetch_inbound_since(self, since_message_id: int) -> dict[str, Any]:
        self.since_seen.append(since_message_id)
        return {
            "messages": sorted(
                (r for r in self.rows if r["message_id"] > since_message_id),
                key=lambda r: r["message_id"],
            )
        }


def _gateway(backend: _PollBackend) -> tuple[BGOSAdapter, list[tuple[str, int]]]:
    """One gateway process serving both agents, recording who got what."""
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    adapter._state.set_route(AGENT_A, "route_a")
    adapter._state.set_route(AGENT_B, "route_b")
    adapter._api = backend  # type: ignore[assignment]
    got: list[tuple[str, int]] = []

    async def capture(event: Any) -> None:
        got.append((event.agent_route, event.message_id))

    adapter.handle_message = capture  # type: ignore[method-assign]
    return adapter, got


def _poll_start(adapter: BGOSAdapter) -> int:
    """Where the poll loop starts: the same call the loop makes."""
    if hasattr(adapter, "_poll_since"):
        return adapter._poll_since()
    return adapter._load_last_id()


async def test_both_agents_receive_it_from_the_same_poll() -> None:
    backend = _PollBackend([_row(500, AGENT_A), _row(500, AGENT_B)])
    adapter, got = _gateway(backend)

    await adapter._run_backfill(_poll_start(adapter))

    assert sorted(got) == [("route_a", 500), ("route_b", 500)]


async def test_the_second_agent_still_gets_it_when_the_gateway_restarts_between_the_two_turns() -> None:
    backend = _PollBackend([_row(500, AGENT_A), _row(500, AGENT_B)])

    # Process 1 starts polling (both agents known), reaches agent A's turn,
    # then dies before agent B's.
    first, first_got = _gateway(backend)
    _poll_start(first)
    await first._handle_inbound(_row(500, AGENT_A), batchable=False)
    assert first_got == [("route_a", 500)]

    # Process 2 starts from what process 1 persisted.
    second, second_got = _gateway(backend)
    await second._run_backfill(_poll_start(second))

    # B gets it; A is not handed the same message a second time.
    assert second_got == [("route_b", 500)]
