"""Another agent's tag next to a person's turn, and two agents of one pairing
seated in the same group.

- A tag never cuts into a person's running turn. Hermes queues an internal
  event behind the running turn and never interrupts or steers it
  (hermes-agent v0.23.0, gateway/run_busy.py,
  ``_handle_active_session_busy_message``), while a person's message in the
  same session interrupts it. So another agent's row reaches the gateway as
  internal.
- One pairing can serve two agents seated in the same group (one gateway,
  multiplexed profiles). Each agent's turn runs in a task of its own, and its
  reply posts as that agent, whichever agent's row arrived last. The tag a
  reply answers, and whether a person's turn is still running, belong to one
  agent in one chat, not to the chat.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from tests.mocks.mock_hermes import (
    MessageEvent as GatewayMessageEvent,
    MessageType as GatewayMessageType,
)


pytestmark = pytest.mark.asyncio

AGENT_A = 7  # route "default"
AGENT_B = 8  # route "shadow", on the same pairing
TAGGER = 42  # another owner's agent seated in the group
CHAT_ID = 300
CHAT_KINDS = ("room", "channel")
ROUTES = {AGENT_A: "default", AGENT_B: "shadow"}


def _adapter() -> BGOSAdapter:
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    for assistant_id, route in ROUTES.items():
        adapter._state.set_route(assistant_id, route)
    # Short batch window: a person's row still reaches the agent inside the
    # settle sleep.
    adapter._text_batch_window = 0.05
    return adapter


def _gateway_wrap(adapter: BGOSAdapter, monkeypatch) -> None:
    """Dispatch through the gateway's own MessageEvent, as on a Hermes install."""
    monkeypatch.setattr(
        bgos_adapter_module, "_GatewayMessageEvent", GatewayMessageEvent,
    )
    monkeypatch.setattr(bgos_adapter_module, "_GatewayMessageType", GatewayMessageType)
    monkeypatch.setattr(
        adapter,
        "build_source",
        lambda *, chat_id, user_id: SimpleNamespace(
            chat_id=chat_id, user_id=user_id, profile=None,
        ),
        raising=False,
    )


def _capture(adapter: BGOSAdapter) -> list[Any]:
    received: list[Any] = []

    async def capture(event: Any) -> None:
        received.append(event)

    adapter.handle_message = capture  # type: ignore[method-assign]
    return received


def _row(
    lane: str,
    *,
    message_id: int,
    to: int,
    text: str,
    chat_kind: str = "room",
    tagged_by: int | None = None,
) -> dict:
    """One row for agent `to`, the way BGOS sends it: the live push
    (camelCase) or the inbound poll (snake_case, camelCase marker)."""
    marker: dict[str, Any] = (
        {"senderType": "room_agent", "senderAssistantId": tagged_by}
        if tagged_by is not None
        else {}
    )
    if lane == "push":
        return {
            "assistantId": to,
            "userId": "owner_1",
            "chatId": CHAT_ID,
            "messageId": message_id,
            "text": text,
            "files": [],
            "messageType": "standard",
            "groupContext": {"chatKind": chat_kind, "chatTitle": "Launch"},
            **marker,
        }
    return {
        "id": message_id,
        "message_id": message_id,
        "chat_id": CHAT_ID,
        "text": text,
        "files": [],
        "chat_kind": chat_kind,
        "groupContext": {"chatKind": chat_kind, "chatTitle": "Launch"},
        "message_type": "standard",
        "command_name": None,
        "command_args": None,
        "created_at": "2026-11-09T08:00:00.000Z",
        "assistant_id": to,
        "agent_route": ROUTES.get(to, "default"),
        "user_id": "owner_1",
        **marker,
    }


async def _deliver(adapter: BGOSAdapter, lane: str, row: dict) -> None:
    if lane == "push":
        await adapter._handle_inbound(row)
    else:
        async def fetch_inbound_since(since: int) -> dict:
            return {"messages": [row]}

        adapter._api.fetch_inbound_since = fetch_inbound_since  # type: ignore[method-assign]
        await adapter._run_backfill(0)
    await asyncio.sleep(0.15)


def _backend(adapter: BGOSAdapter, monkeypatch, chat_kind: str = "room") -> list[dict]:
    """GET /chats/:id and /send-message. A group's chat row names only its
    carrier (the creator's first agent), here agent A."""
    posts: list[dict] = []

    async def get_chat(chat_id: int) -> dict:
        return {"id": chat_id, "kind": chat_kind, "assistantId": AGENT_A}

    async def post_send_message(**kw: Any) -> dict:
        posts.append(kw)
        return {"message": {"id": 2000 + len(posts)}}

    monkeypatch.setattr(adapter._api, "get_chat", get_chat)
    monkeypatch.setattr(adapter._api, "post_send_message", post_send_message)
    return posts


class _Turns:
    """The gateway's shape for a turn: handle_message returns at once and
    the turn runs in a task of its own (BasePlatformAdapter
    ``_start_session_processing``), which is where every send of that turn
    comes from. A turn here waits until the test says what it replies."""

    def __init__(self, adapter: BGOSAdapter) -> None:
        self._adapter = adapter
        self._replies: dict[int, asyncio.Queue] = {}
        adapter.handle_message = self._handle_message  # type: ignore[method-assign]

    def _queue(self, assistant_id: int) -> asyncio.Queue:
        return self._replies.setdefault(assistant_id, asyncio.Queue())

    async def _handle_message(self, event: Any) -> None:
        row = getattr(event, "raw_message", None) or event
        replies = self._queue(row.assistant_id)
        chat_id = row.chat_id

        async def turn() -> None:
            await self._adapter.send(chat_id=chat_id, content=await replies.get())

        asyncio.get_running_loop().create_task(turn())

    async def reply(self, assistant_id: int, text: str) -> None:
        """The oldest waiting turn of this agent replies."""
        await self._queue(assistant_id).put(text)
        await asyncio.sleep(0.05)


# --- A tag never cuts into a person's running turn ---------------------------


@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
@pytest.mark.parametrize("lane", ("push", "poll"))
async def test_another_agents_tag_reaches_the_gateway_as_internal(lane, chat_kind, monkeypatch):
    adapter = _adapter()
    _gateway_wrap(adapter, monkeypatch)
    got = _capture(adapter)
    await _deliver(adapter, lane, _row(
        lane, message_id=901, to=AGENT_A, chat_kind=chat_kind,
        text="@Achilles can you check the build?", tagged_by=TAGGER,
    ))
    assert len(got) == 1
    assert got[0].internal is True, (
        "a tag must queue behind a person's running turn, never interrupt it"
    )


@pytest.mark.parametrize("lane", ("push", "poll"))
async def test_a_persons_message_reaches_the_gateway_as_the_persons(lane, monkeypatch):
    adapter = _adapter()
    _gateway_wrap(adapter, monkeypatch)
    got = _capture(adapter)
    await _deliver(adapter, lane, _row(
        lane, message_id=902, to=AGENT_A, text="@Achilles what is the status?",
    ))
    assert len(got) == 1
    assert got[0].internal is False


# --- Two agents of one pairing in one group -----------------------------------


@pytest.mark.parametrize("wrap", ("adapter_event", "gateway_event"))
@pytest.mark.parametrize("chat_kind", CHAT_KINDS)
async def test_each_agents_reply_posts_as_that_agent(chat_kind, wrap, monkeypatch):
    adapter = _adapter()
    if wrap == "gateway_event":
        _gateway_wrap(adapter, monkeypatch)
    turns = _Turns(adapter)
    posts = _backend(adapter, monkeypatch, chat_kind)
    await _deliver(adapter, "push", _row(
        "push", message_id=900, to=AGENT_A, chat_kind=chat_kind, text="@Achilles status?",
    ))
    await _deliver(adapter, "push", _row(
        "push", message_id=901, to=AGENT_B, chat_kind=chat_kind, text="@Shadow status?",
    ))
    # Agent B's row arrived last; agent A answers first.
    await turns.reply(AGENT_A, "Achilles: green.")
    await turns.reply(AGENT_B, "Shadow: green.")
    assert [(p["assistant_id"], p["text"]) for p in posts] == [
        (AGENT_A, "Achilles: green."),
        (AGENT_B, "Shadow: green."),
    ]


async def test_a_tag_is_answered_by_the_agent_it_tagged(monkeypatch):
    adapter = _adapter()
    turns = _Turns(adapter)
    posts = _backend(adapter, monkeypatch)
    # Another owner's agent tags A; meanwhile a person asks B, and B
    # answers first.
    await _deliver(adapter, "push", _row(
        "push", message_id=950, to=AGENT_A, text="@Achilles can you check the build?",
        tagged_by=TAGGER,
    ))
    await _deliver(adapter, "push", _row(
        "push", message_id=951, to=AGENT_B, text="@Shadow what time is it?",
    ))
    await turns.reply(AGENT_B, "Shadow: noon.")
    await turns.reply(AGENT_A, "Achilles: the build is green.")
    shadow, achilles = posts
    assert shadow["assistant_id"] == AGENT_B
    assert shadow["reply_to_id"] != 950, "B's reply must not quote a tag addressed to A"
    assert achilles["assistant_id"] == AGENT_A
    assert achilles["reply_to_id"] == 950, "A's reply answers the tag addressed to A"


async def test_a_persons_running_turn_with_one_agent_does_not_hold_back_the_others_tag(monkeypatch):
    adapter = _adapter()
    turns = _Turns(adapter)
    posts = _backend(adapter, monkeypatch)
    # A person asked B, and B is still working on it when A is tagged.
    await _deliver(adapter, "push", _row(
        "push", message_id=960, to=AGENT_B, text="@Shadow summarise the thread",
    ))
    await _deliver(adapter, "push", _row(
        "push", message_id=961, to=AGENT_A, text="@Achilles can you check the build?",
        tagged_by=TAGGER,
    ))
    await turns.reply(AGENT_A, "Achilles: on it.")
    await turns.reply(AGENT_B, "Shadow: here is the summary.")
    achilles, shadow = posts
    assert achilles["reply_to_id"] == 961
    assert shadow["reply_to_id"] != 961


async def test_a_second_profiles_running_turn_keeps_its_tag_unanchored_after_its_first_bubble(
    monkeypatch,
):
    # hermes-agent v0.23.0 namespaces a session key by the source's profile
    # (BasePlatformAdapter._source_session_key, build_session_key(profile=)).
    # A key built without the profile never matches agent B's live session,
    # so B's running turn read as finished and the tag was anchored on it.
    adapter = _adapter()
    _gateway_wrap(adapter, monkeypatch)
    monkeypatch.setattr(
        bgos_adapter_module,
        "profile_for_route",
        lambda route, **_: None if route == "default" else route,
    )

    def source_session_key(source: Any) -> str:
        return f"agent:{source.profile or 'main'}:bgos:group:{source.chat_id}"

    monkeypatch.setattr(adapter, "_source_session_key", source_session_key, raising=False)
    adapter._active_sessions = {}  # type: ignore[attr-defined]
    _capture(adapter)
    posts = _backend(adapter, monkeypatch)
    await _deliver(adapter, "push", _row(
        "push", message_id=970, to=AGENT_B, text="@Shadow what is the build status?",
    ))
    # The gateway runs B's turn in B's profile, and B posts a first bubble.
    adapter._active_sessions["agent:shadow:bgos:group:300"] = object()  # type: ignore[attr-defined]
    await adapter.send(chat_id=CHAT_ID, content="Shadow: checking.")
    await _deliver(adapter, "push", _row(
        "push", message_id=971, to=AGENT_B, text="@Shadow can you check the build?",
        tagged_by=TAGGER,
    ))
    await adapter.send(chat_id=CHAT_ID, content="Shadow: the build is green.")
    assert posts[-1]["assistant_id"] == AGENT_B
    assert posts[-1]["reply_to_id"] is None, "the person's answer must not quote the tag"


def _tap(*, on_card_of: int, message_id: int) -> dict:
    """A person's tap on a card one agent posted (`inbound_click`)."""
    return {
        "assistantId": on_card_of,
        "userId": "owner_1",
        "chatId": CHAT_ID,
        "messageId": message_id,
        "optionId": "opt_1",
        "callbackData": "opt_1",
        "buttonText": "Ship it",
    }


async def test_a_tap_on_an_agents_card_is_answered_by_that_agent(monkeypatch):
    adapter = _adapter()
    turns = _Turns(adapter)
    posts = _backend(adapter, monkeypatch)
    await adapter._handle_inbound_click(_tap(on_card_of=AGENT_A, message_id=2500))
    # B's row arrives while A's turn for the tap is still running.
    await _deliver(adapter, "push", _row(
        "push", message_id=980, to=AGENT_B, text="@Shadow status?",
    ))
    await turns.reply(AGENT_A, "Achilles: shipping.")
    assert (posts[-1]["assistant_id"], posts[-1]["text"]) == (
        AGENT_A, "Achilles: shipping.",
    )


async def test_a_tap_on_one_agents_card_does_not_drop_the_others_tag(monkeypatch):
    adapter = _adapter()
    turns = _Turns(adapter)
    posts = _backend(adapter, monkeypatch)
    await _deliver(adapter, "push", _row(
        "push", message_id=990, to=AGENT_A, text="@Achilles can you check the build?",
        tagged_by=TAGGER,
    ))
    await adapter._handle_inbound_click(_tap(on_card_of=AGENT_B, message_id=2600))
    await turns.reply(AGENT_A, "Achilles: the build is green.")
    achilles = [p for p in posts if p["assistant_id"] == AGENT_A]
    assert achilles[-1]["reply_to_id"] == 990, "a tap on B's card is not an answer to A's tag"
