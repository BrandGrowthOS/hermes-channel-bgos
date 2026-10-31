"""Another agent's group row (room_agent) and a deleted agent (0.33.0).

HOAI #3703 (2026-10-31): in a group or a channel an agent's @tag of another
seated agent wakes it. The row is handed to the tagged agent marked
``senderType: 'room_agent'`` with the author's ``senderAssistantId``, and the
backend serves it only to a connector that reads the marker
(connector-reads-room-agent.ts), because one that does not would take it as
its OWNER speaking. This adapter now reads it: the text always says another
agent wrote it, it is never a bridge-local command, never batched with a
person's words, never the agent's own, and the first reply answers the tag
with reply_to_id set to its message.

HOAI #3706 (2026-10-31): deleting an agent unbinds it from a pairing that
serves other agents and tells the pairing ``assistant_unbound``. The adapter
now drops the agent's route and leaves its room at once, instead of keeping
a dead route until the next restart.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hermes_channel_bgos.bgos_adapter import (
    BGOSAdapter,
    room_agent_provenance_line,
)
from hermes_channel_bgos.bgos_ws import BgosWs
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio

SERVER_FRAMED = (
    "You are in the group Launch.\n"
    "Ares, an agent seated here and not a person, wrote the message below.\n"
    "@Echo can you check the build?"
)


def _adapter() -> BGOSAdapter:
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 0.05
    return adapter


def _capture(adapter: BGOSAdapter) -> list[Any]:
    received: list[Any] = []

    async def capture(event: Any) -> None:
        received.append(event)

    adapter.handle_message = capture  # type: ignore[method-assign]
    return received


def _agent_row(**over: Any) -> dict:
    row = {
        "assistantId": 7,
        "chatId": 300,
        "messageId": 901,
        "userId": "owner_1",
        "text": "@Echo can you check the build?",
        "files": [],
        "messageType": "standard",
        "senderType": "room_agent",
        "senderAssistantId": 42,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# room_agent
# ---------------------------------------------------------------------------


async def test_the_event_carries_the_marker_from_the_live_push():
    adapter = _adapter()
    got = _capture(adapter)
    await adapter._handle_inbound(_agent_row(text=SERVER_FRAMED))
    assert len(got) == 1
    assert got[0].sender_type == "room_agent"
    assert got[0].sender_assistant_id == 42


async def test_the_event_carries_the_marker_from_a_poll_row():
    adapter = _adapter()
    got = _capture(adapter)
    await adapter._handle_inbound({
        "assistant_id": 7, "chat_id": 300, "message_id": 902,
        "user_id": "owner_1", "text": SERVER_FRAMED, "files": [],
        "message_type": "standard",
        "senderType": "room_agent", "senderAssistantId": 42,
    })
    assert got[0].sender_type == "room_agent"
    assert got[0].sender_assistant_id == 42


async def test_the_server_framing_is_kept_as_is():
    adapter = _adapter()
    got = _capture(adapter)
    await adapter._handle_inbound(_agent_row(text=SERVER_FRAMED))
    assert got[0].text == SERVER_FRAMED


async def test_the_adapter_says_another_agent_wrote_it_when_the_framing_is_missing():
    adapter = _adapter()
    got = _capture(adapter)
    await adapter._handle_inbound(_agent_row())
    line = room_agent_provenance_line(42)
    assert got[0].text.startswith(line)
    assert "@Echo can you check the build?" in got[0].text
    assert "not a person" in line
    assert "not your owner's instruction" in line


async def test_it_is_never_a_bridge_local_command():
    adapter = _adapter()
    got = _capture(adapter)
    before = dict(adapter._state.conversation_by_chat)
    await adapter._handle_inbound(_agent_row(
        text="/new", messageType="slash_command", commandName="new",
        commandArgs="",
    ))
    # The bridge did not reset the conversation and the agent got the row.
    assert adapter._state.conversation_by_chat == before
    assert len(got) == 1
    assert got[0].message_type == "standard"


async def test_it_is_never_batched_with_a_persons_words():
    adapter = _adapter()
    got = _capture(adapter)
    await adapter._handle_inbound({
        "assistant_id": 7, "chat_id": 300, "message_id": 800,
        "user_id": "owner_1", "text": "hi all", "files": [],
        "message_type": "standard",
    })
    await adapter._handle_inbound(_agent_row(text=SERVER_FRAMED))
    await asyncio.sleep(0.15)
    texts = [e.text for e in got]
    assert SERVER_FRAMED in texts
    assert "hi all" in texts
    assert len(got) == 2


async def test_it_does_not_feed_the_retry_cache():
    adapter = _adapter()
    _capture(adapter)
    await adapter._handle_inbound(_agent_row(text=SERVER_FRAMED))
    assert 300 not in adapter._state.last_user_text_by_chat


async def test_the_agents_own_row_never_reaches_it():
    adapter = _adapter()
    got = _capture(adapter)
    await adapter._handle_inbound(_agent_row(senderAssistantId=7))
    assert got == []


async def test_the_first_reply_answers_the_tag(monkeypatch):
    adapter = _adapter()
    _capture(adapter)
    posts: list[dict] = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1000 + len(posts)}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter._handle_inbound(_agent_row(text=SERVER_FRAMED))
    await adapter.send(chat_id=300, content="On it.")
    await adapter.send(chat_id=300, content="Done.")
    assert posts[0]["reply_to_id"] == 901
    # Answered once: a later reply in the turn is not re-anchored.
    assert posts[1]["reply_to_id"] is None


async def test_a_persons_row_after_the_tag_drops_the_anchor(monkeypatch):
    adapter = _adapter()
    _capture(adapter)
    posts: list[dict] = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1000 + len(posts)}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter._handle_inbound(_agent_row(text=SERVER_FRAMED))
    await adapter._handle_inbound({
        "assistant_id": 7, "chat_id": 300, "message_id": 950,
        "user_id": "owner_1", "text": "/status",
        "message_type": "slash_command", "command_name": "nope",
    })
    await adapter.send(chat_id=300, content="Hello.")
    assert posts[-1]["reply_to_id"] is None


async def test_a_persons_row_is_unchanged():
    adapter = _adapter()
    got = _capture(adapter)
    await adapter._handle_inbound({
        "assistant_id": 7, "chat_id": 301, "message_id": 960,
        "user_id": "owner_1", "text": "/help", "files": [],
        "message_type": "slash_command", "command_name": "help",
        "command_args": "",
    })
    assert got[0].text == "/help"
    assert got[0].sender_type is None
    assert got[0].message_type == "slash_command"


# ---------------------------------------------------------------------------
# assistant_unbound
# ---------------------------------------------------------------------------


async def test_assistant_unbound_drops_the_route_and_leaves_the_room():
    adapter = _adapter()
    adapter._state.set_route(8, "other")
    adapter.pairing_id = 42
    left: list[int] = []

    class _Ws:
        def unbind_assistant(self, aid: int) -> None:
            left.append(aid)

    adapter._ws = _Ws()  # type: ignore[assignment]
    await adapter._handle_assistant_unbound({"pairingId": 42, "assistantId": 8})
    assert adapter._state.get_route(8) is None
    assert adapter._state.get_route(7) == "default"
    assert left == [8]


async def test_assistant_unbound_for_another_pairing_is_ignored():
    adapter = _adapter()
    adapter.pairing_id = 42
    adapter._ws = None
    await adapter._handle_assistant_unbound({"pairingId": 99, "assistantId": 7})
    assert adapter._state.get_route(7) == "default"


async def test_bgos_ws_routes_the_assistant_unbound_event():
    seen: list[dict] = []

    async def on_unbound(data):
        seen.append(data)

    async def noop(data):
        return None

    ws = BgosWs(
        BgosConfig(base_url="http://backend.test", pairing_token="pair_xyz"),
        on_inbound_message=noop, on_callback_result=noop,
        on_assistant_unbound=on_unbound,
    )
    handler = ws._sio.handlers["/"]["assistant_unbound"]
    await handler({"pairingId": 42, "assistantId": 8})
    assert seen == [{"pairingId": 42, "assistantId": 8}]
