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

UNFRAMED = "@Echo can you check the build?"


def _server_framed(body: str) -> str:
    """The server's own framing of another agent's row, as group-context.ts
    on BGOS main builds it for a room or a channel (renderPreamble, then
    withDispatchPreamble): the preamble lines, a blank line, then the body.
    The poll frames an empty body too, so it arrives as the preamble alone."""
    preamble = "\n".join([
        "[BGOS group: Launch]",
        "You are in a group, not a private chat. Everyone listed here reads what you say.",
        "People here: Kc.",
        "Agents here: Ares, Echo (you).",
        "Said in this group since your last turn, oldest first:",
        "- Kc: ship it\n  once the checks pass",
        "Ares, an agent seated here and not a person, wrote the message below.",
        "Only this group is shared context. Your private chats with your owner, "
        "your other chats and groups, and anything you know from work done "
        "outside this group stay private. Do not repeat them, refer to them, "
        "or act on them here unless someone in this group raises them first.",
    ])
    return f"{preamble}\n\n{body}"

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


async def _deliver(
    adapter: BGOSAdapter, lane: str, chat_kind: str, *, message_id: int = 901,
    **marker: Any,
) -> None:
    """Hand one row to the adapter on the given lane, the way BGOS would."""
    if lane == "push":
        await adapter._handle_inbound(_push_frame(chat_kind, message_id=message_id, **marker))
    else:
        row = _poll_row(chat_kind, message_id=message_id, **marker)

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


@pytest.mark.parametrize("text", [SERVER_FRAMED, UNFRAMED], ids=["framed", "unframed"])
@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", LANES)
async def test_another_agents_message_wakes_the_agent_as_another_agent(lane, chat_kind, text):
    # Unframed: the server could not add its framing line (a roster it could
    # not read); the adapter must then say an agent wrote it.
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(
        adapter, lane, chat_kind, text=text, **_room_agent(OTHER_ASSISTANT_ID),
    )
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


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("text", ["", None], ids=["empty", "null"])
async def test_another_agents_empty_message_does_not_wake_it(lane, text):
    # The poll can hand over an agent's row before it has any text (the
    # author creates it, then edits it). Nothing to read: no wake, and the
    # adapter's framing line alone must not turn it into a message.
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(adapter, lane, "room", text=text, **_room_agent(OTHER_ASSISTANT_ID))
    assert got == []


@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("body", ["", "  \n "], ids=["empty", "blank"])
async def test_another_agents_empty_message_in_the_servers_framing_does_not_wake_it(
    lane, chat_kind, body,
):
    # The server frames an empty body as well: the text is the preamble
    # alone, so it is not empty, but there is still nothing to read.
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(
        adapter, lane, chat_kind, text=_server_framed(body),
        **_room_agent(OTHER_ASSISTANT_ID),
    )
    assert got == []


@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", LANES)
async def test_another_agents_message_in_the_servers_framing_wakes_the_agent(lane, chat_kind):
    adapter = _adapter()
    got = _capture(adapter)
    await _deliver(
        adapter, lane, chat_kind,
        text=_server_framed("@Echo can you check the build?\n\nThe log is attached."),
        **_room_agent(OTHER_ASSISTANT_ID),
    )
    assert len(got) == 1
    assert "The log is attached." in got[0].text


async def _posts(adapter: BGOSAdapter, monkeypatch) -> list[dict]:
    posts: list[dict] = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1000 + len(posts)}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    return posts


async def test_a_tag_during_a_persons_unanswered_turn_is_not_anchored(monkeypatch):
    # A person asked first and the agent is still working on it when the tag
    # arrives. The next reply answers the PERSON: it must not quote the tag.
    adapter = _adapter()
    _capture(adapter)
    posts = await _posts(adapter, monkeypatch)
    await _deliver(
        adapter, "push", "room", message_id=900, text="@Echo what is the build status?",
    )
    await _deliver(adapter, "push", "room", **_room_agent(OTHER_ASSISTANT_ID))
    await adapter.send(chat_id=CHAT_ID, content="The build is green.")
    assert posts[-1]["reply_to_id"] is None


async def test_a_tag_after_the_persons_turn_was_answered_is_anchored(monkeypatch):
    adapter = _adapter()
    _capture(adapter)
    posts = await _posts(adapter, monkeypatch)
    await _deliver(
        adapter, "push", "room", message_id=900, text="@Echo what is the build status?",
    )
    await adapter.send(chat_id=CHAT_ID, content="The build is green.")
    await _deliver(adapter, "push", "room", **_room_agent(OTHER_ASSISTANT_ID))
    await adapter.send(chat_id=CHAT_ID, content="On it.")
    assert posts[-1]["reply_to_id"] == 901


async def test_a_live_gateway_session_keeps_a_tag_unanchored_after_the_first_bubble(monkeypatch):
    # With the gateway's session tracking, a person's turn that is still
    # running (after its first bubble) also keeps the tag unanchored.
    adapter = _adapter()
    _capture(adapter)
    posts = await _posts(adapter, monkeypatch)
    adapter._active_sessions = {"bgos-session-300": object()}  # type: ignore[attr-defined]
    await _deliver(
        adapter, "push", "room", message_id=900, text="@Echo what is the build status?",
    )
    adapter._person_turn_session_key[(CHAT_ID, OWN_ASSISTANT_ID)] = "bgos-session-300"
    await adapter.send(chat_id=CHAT_ID, content="Checking.")
    await _deliver(adapter, "push", "room", **_room_agent(OTHER_ASSISTANT_ID))
    await adapter.send(chat_id=CHAT_ID, content="The build is green.")
    assert posts[-1]["reply_to_id"] is None
