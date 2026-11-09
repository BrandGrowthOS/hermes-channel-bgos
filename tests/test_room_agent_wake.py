"""Another agent's message in a group or a channel wakes this agent, and the
agent's own message never does.

The rows are shaped like the ones the BGOS backend sends:

- the live push (``inbound_message``): camelCase, ``senderType: 'room_agent'``
  with the author's ``senderAssistantId``, no human sender block, ``userId``
  set to the RECEIVING agent's owner, and ``groupContext.chatKind``;
- the inbound poll (``GET /integrations/inbound``): snake_case keys with the
  same camelCase ``senderType`` and ``senderAssistantId``, plus ``chat_kind``.

Each case runs on both lanes and in both a group (``room``) and a channel.
The own row case is the reply loop guard: the agent must never be woken by
what it wrote itself.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio

OWN_ASSISTANT_ID = 7
OTHER_ASSISTANT_ID = 42
CHAT_ID = 300

SERVER_FRAMED = (
    "You are in the group Launch.\n"
    "Ares, an agent seated here and not a person, wrote the message below.\n"
    "@Echo can you check the build?"
)

LANES = ("push", "poll")
CHAT_KINDS = ("room", "channel")


def _adapter() -> BGOSAdapter:
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    adapter._state.set_route(OWN_ASSISTANT_ID, "default")
    # Short batch window: a row that is batched still reaches the agent
    # inside the test's settle sleep.
    adapter._text_batch_window = 0.05
    return adapter


def _capture(adapter: BGOSAdapter) -> list[Any]:
    received: list[Any] = []

    async def capture(event: Any) -> None:
        received.append(event)

    adapter.handle_message = capture  # type: ignore[method-assign]
    return received


def _push_frame(chat_kind: str, *, message_id: int, **marker: Any) -> dict:
    frame: dict[str, Any] = {
        "assistantId": OWN_ASSISTANT_ID,
        "userId": "owner_1",
        "chatId": CHAT_ID,
        "messageId": message_id,
        "text": SERVER_FRAMED,
        "files": [],
        "messageType": "standard",
        "groupContext": {"chatKind": chat_kind, "chatTitle": "Launch"},
    }
    frame.update(marker)
    return frame


def _poll_row(chat_kind: str, *, message_id: int, **marker: Any) -> dict:
    row: dict[str, Any] = {
        "id": message_id,
        "message_id": message_id,
        "chat_id": CHAT_ID,
        "text": SERVER_FRAMED,
        "files": [],
        "chat_kind": chat_kind,
        "groupContext": {"chatKind": chat_kind, "chatTitle": "Launch"},
        "message_type": "standard",
        "command_name": None,
        "command_args": None,
        "created_at": "2026-11-09T08:00:00.000Z",
        "assistant_id": OWN_ASSISTANT_ID,
        "agent_route": "default",
        "user_id": "owner_1",
    }
    row.update(marker)
    return row


async def _deliver(adapter: BGOSAdapter, lane: str, chat_kind: str, **marker: Any) -> None:
    """Hand one row to the adapter on the given lane, the way BGOS would."""
    if lane == "push":
        await adapter._handle_inbound(_push_frame(chat_kind, message_id=901, **marker))
    else:
        row = _poll_row(chat_kind, message_id=901, **marker)

        async def fetch_inbound_since(since: int) -> dict:
            return {"messages": [row]}

        adapter._api.fetch_inbound_since = fetch_inbound_since  # type: ignore[method-assign]
        await adapter._run_backfill(0)
    await asyncio.sleep(0.15)


def _room_agent(author: int | None) -> dict:
    marker: dict[str, Any] = {"senderType": "room_agent"}
    if author is not None:
        marker["senderAssistantId"] = author
    return marker


@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", LANES)
async def test_another_agents_message_wakes_the_agent_as_another_agent(lane, chat_kind):
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(adapter, lane, chat_kind, **_room_agent(OTHER_ASSISTANT_ID))
    assert len(got) == 1, "another agent's message must wake the agent once"
    event = got[0]
    # The agent is told who wrote it: another agent, never its owner.
    assert getattr(event, "sender_type", None) == "room_agent"
    assert getattr(event, "sender_assistant_id", None) == OTHER_ASSISTANT_ID
    assert "not a person" in event.text


@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", LANES)
async def test_the_agents_own_message_does_not_wake_it(lane, chat_kind):
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(adapter, lane, chat_kind, **_room_agent(OWN_ASSISTANT_ID))
    assert got == [], "the agent must never be woken by its own message"


@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", LANES)
async def test_another_agents_message_with_no_author_does_not_wake_it(lane, chat_kind):
    # The marker without a readable author: it cannot be told apart from the
    # agent's own message, so it is not delivered (the Claude Code plugin's
    # rule, isOwnRoomAgentMessage). Waking on it could start a reply loop.
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(adapter, lane, chat_kind, **_room_agent(None))
    assert got == []


@pytest.mark.parametrize("lane", LANES)
async def test_the_agents_own_message_does_not_rearm_the_loop_guards(lane):
    # A genuine inbound message re-arms the boards and peer marker loop
    # guards. The agent's own message is not one: it must leave them as
    # they were.
    adapter = _adapter()
    _capture(adapter)
    adapter._boards_consecutive_results[CHAT_ID] = 3
    adapter._peer_consecutive_results[CHAT_ID] = 3
    await _deliver(adapter, lane, "room", **_room_agent(OWN_ASSISTANT_ID))
    assert adapter._boards_consecutive_results.get(CHAT_ID) == 3
    assert adapter._peer_consecutive_results.get(CHAT_ID) == 3


@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", LANES)
async def test_a_persons_message_wakes_the_agent_unmarked(lane, chat_kind):
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(adapter, lane, chat_kind, text="@Echo hi")
    assert len(got) == 1
    assert getattr(got[0], "sender_type", None) is None
    assert got[0].text == "@Echo hi"
