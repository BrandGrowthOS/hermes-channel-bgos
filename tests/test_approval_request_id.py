"""Approval cards are bound to the Hermes request they show.

Hermes v0.22.6 (hermes-agent PR #87554) calls `send_exec_approval` with
`request_id=...` on every call, and v0.23.0 adds
`tools.approval.resolve_gateway_approval_card(session_key, choice, request_id)`
to the published plugin API. Since v0.21.6 the runner also hands an approval to
`send_exec_approval` only when the adapter class says it renders buttons
(`supports_exec_approval_buttons()`); otherwise the user gets the plain text
"/approve" prompt.

These tests pin three things:

- the hook, probed and called exactly the way v0.22.6 does it, posts the
  four-button card instead of being skipped or raising TypeError;
- a tap on an old card whose request already settled (timed out, answered by a
  typed /approve) approves nothing and answers "Approval expired", while the
  newer request the person has not seen stays pending;
- a settle on Hermes' agent thread at any point of the tap handler never sends
  the tap down the first in first out path, so it can never approve the next
  request (the interleaving is forced, not timed);
- card ids restart at 1 in every adapter instance, so a tap is also bound to
  the BGOS message its card was posted as: an old live card (gateway restart,
  adapter rebuilt on reconnect) never answers the new card with the same id;
- on an older Hermes (no request id, no card resolver, no settle observer) the
  tap still resolves first in first out, as before.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

import hermes_channel_bgos.bgos_adapter as adapter_mod
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


class FakeApprovalQueue:
    """A small model of Hermes' `tools.approval` gateway queue (v0.23.0)."""

    def __init__(self) -> None:
        self.queues: dict[str, list[dict[str, Any]]] = {}
        self.results: dict[str, str] = {}
        self.calls: list[tuple] = []

    def enqueue(self, session_key: str, request_id: str) -> None:
        self.queues.setdefault(session_key, []).append(
            {"request_id": request_id, "observers": []},
        )

    def pending(self, session_key: str) -> list[str]:
        return [e["request_id"] for e in self.queues.get(session_key, [])]

    def _settle(self, entry: dict[str, Any], reason: str) -> None:
        observers, entry["observers"] = entry["observers"], []
        for cb in observers:
            cb(reason)

    def resolve_gateway_approval(
        self, session_key, choice, resolve_all=False, reason=None, request_id=None,
    ) -> int:
        self.calls.append(("resolve", session_key, choice, request_id))
        queue = self.queues.get(session_key) or []
        if not queue:
            return 0
        if request_id:
            targets = [e for e in queue if e["request_id"] == request_id]
        elif resolve_all:
            targets = list(queue)
        else:
            targets = [queue[0]]
        for entry in targets:
            queue.remove(entry)
            self.results[entry["request_id"]] = choice
            self._settle(entry, "resolved")
        return len(targets)

    def resolve_gateway_approval_card(self, session_key, choice, request_id) -> int:
        self.calls.append(("card", session_key, choice, request_id))
        if request_id:
            return self.resolve_gateway_approval(session_key, choice, request_id=request_id)
        return self.resolve_gateway_approval(session_key, choice)

    def observe_gateway_settle(self, session_key, request_id, observer) -> bool:
        for entry in self.queues.get(session_key, []):
            if entry["request_id"] == request_id:
                entry["observers"].append(observer)
                return True
        return False

    def time_out(self, session_key: str, request_id: str) -> None:
        queue = self.queues.get(session_key, [])
        entry = next(e for e in queue if e["request_id"] == request_id)
        queue.remove(entry)
        self._settle(entry, "timeout")


def _adapter(
    monkeypatch, *, first_message_id: int = 500,
) -> tuple[BGOSAdapter, list[dict], list[dict]]:
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    posts: list[dict] = []
    patches: list[dict] = []
    next_id = iter(range(first_message_id, first_message_id + 100))

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": next(next_id)}

    async def fake_patch(mid, *, text=None, options=None, **kw):
        patches.append({"message_id": mid, "text": text, "options": options})
        return {"id": mid}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    return adapter, posts, patches


def _install(monkeypatch, fake: FakeApprovalQueue, *, card=True, observe=True) -> None:
    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", fake.resolve_gateway_approval)
    monkeypatch.setattr(
        adapter_mod, "resolve_gateway_approval_card",
        fake.resolve_gateway_approval_card if card else None,
    )
    monkeypatch.setattr(
        adapter_mod, "observe_gateway_settle",
        fake.observe_gateway_settle if observe else None,
    )


async def _send_like_v0226(adapter: BGOSAdapter, *, session_key: str, request_id: str | None):
    """The exact call shape of hermes-agent v0.22.6 gateway/run_turn_runner.py."""
    flags = {"allow_permanent": True, "allow_session": True, "smart_denied": False}
    return await adapter.send_exec_approval(
        chat_id="11", command="rm -rf build", session_key=session_key,
        description="Remove the build folder?", metadata=None,
        request_id=request_id, **flags,
    )


def _tap(approval_id: int, choice: str, message_id: int) -> dict:
    return {
        "callback_data": f"ea:{choice}:{approval_id}",
        "user_id": "user_1", "message_id": message_id, "chat_id": 11,
    }


# --- The hook on v0.22.6 ------------------------------------------------------


def _renders_exec_approval_buttons(adapter_cls: type) -> bool:
    """hermes-agent v0.22.6 gateway/run_turn_runner.py, verbatim logic."""
    probe = getattr(adapter_cls, "supports_exec_approval_buttons", None)
    if callable(probe) and issubclass(adapter_cls, adapter_mod.BasePlatformAdapter):
        return bool(probe())
    return getattr(adapter_cls, "send_exec_approval", None) is not None


async def test_v0226_runner_hands_the_approval_to_our_hook():
    assert _renders_exec_approval_buttons(BGOSAdapter) is True


async def test_hook_called_like_v0226_returns_buttons(monkeypatch):
    adapter, posts, _ = _adapter(monkeypatch)
    _install(monkeypatch, FakeApprovalQueue())

    result = await _send_like_v0226(adapter, session_key="s", request_id="req-a")

    assert result.success is True
    assert [o["text"] for o in posts[0]["options"]] == [
        "Allow once", "Allow for session", "Always allow", "Deny",
    ]
    assert posts[0]["message_type"] == "approval_request"


async def test_hook_accepts_an_argument_hermes_adds_later(monkeypatch):
    adapter, posts, _ = _adapter(monkeypatch)
    _install(monkeypatch, FakeApprovalQueue())
    sig = inspect.signature(BGOSAdapter.send_exec_approval)
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    await adapter.send_exec_approval(
        chat_id=11, command="x", session_key="s", description="d",
        request_id="r", some_future_flag=True,
    )
    assert len(posts) == 1


# --- A stale card never approves the next request -----------------------------


async def test_old_card_after_timeout_approves_nothing_and_newer_stays_pending(monkeypatch):
    adapter, _, patches = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake)

    fake.enqueue("s", "req-a")
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")  # approval 1
    fake.time_out("s", "req-a")
    fake.enqueue("s", "req-b")
    await _send_like_v0226(adapter, session_key="s", request_id="req-b")  # approval 2

    await adapter._handle_callback(_tap(1, "always", 500))

    assert fake.pending("s") == ["req-b"]
    assert fake.results == {}
    assert patches[-1]["message_id"] == 500
    assert "Approval expired" in patches[-1]["text"]
    assert patches[-1]["options"] == []

    # The newer card still answers its own request.
    await adapter._handle_callback(_tap(2, "once", 501))
    assert fake.results == {"req-b": "once"}
    assert fake.pending("s") == []


async def test_old_card_after_typed_approve_approves_nothing(monkeypatch):
    """No settle observer (it is not on Hermes' published API): the card
    resolver alone still refuses to answer a request other than its own."""
    adapter, _, patches = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake, observe=False)

    fake.enqueue("s", "req-a")
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")
    fake.resolve_gateway_approval("s", "once")  # the person typed /approve
    fake.enqueue("s", "req-b")
    await _send_like_v0226(adapter, session_key="s", request_id="req-b")

    await adapter._handle_callback(_tap(1, "always", 500))

    assert fake.pending("s") == ["req-b"]
    assert fake.results == {"req-a": "once"}
    assert "Approval expired" in patches[-1]["text"]
    assert ("card", "s", "always", "req-a") in fake.calls


async def test_settle_clears_the_stored_card(monkeypatch):
    adapter, _, _ = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake)

    fake.enqueue("s", "req-a")
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")
    assert adapter._approval_state == {
        1: adapter_mod._ApprovalCard("s", request_id="req-a", message_id=500),
    }

    fake.time_out("s", "req-a")

    assert adapter._approval_state == {}


async def test_card_for_a_request_that_settled_before_it_was_posted_is_not_kept(monkeypatch):
    adapter, posts, _ = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake)

    # req-a is not pending anymore when the card goes out.
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")

    assert len(posts) == 1
    assert adapter._approval_state == {}


async def test_a_second_tap_on_an_answered_card_keeps_its_answer(monkeypatch):
    adapter, _, patches = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake)

    fake.enqueue("s", "req-a")
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")
    await adapter._handle_callback(_tap(1, "once", 500))
    await adapter._handle_callback(_tap(1, "deny", 500))

    assert fake.results == {"req-a": "once"}
    assert len(patches) == 1
    assert "Approved once" in patches[0]["text"]


# --- A settle racing the tap never falls back to first in first out ----------


class _SettleAfterRead(dict):
    """The card store, with Hermes' agent thread settling `request_id` right
    after the tap handler's Nth read of card `approval_id`. This forces, one
    by one, every interleaving a real thread race can produce between the
    handler's dict operations (the review saw 324 of 20000 in a thread race)."""

    def __init__(self, base, *, approval_id, after_read, settle) -> None:
        super().__init__(base)
        self._id, self._after, self._settle = approval_id, after_read, settle
        self._reads = 0
        self._busy = False
        self.fired = False

    def _read(self, key, value):
        if key == self._id and not self._busy:
            self._reads += 1
            if self._reads == self._after:
                self._busy = True  # the observer pops this dict too
                try:
                    self._settle()
                finally:
                    self._busy = False
                self.fired = True
        return value

    def get(self, key, *default):
        return self._read(key, super().get(key, *default))

    def __getitem__(self, key):
        return self._read(key, super().__getitem__(key))

    def pop(self, key, *default):
        return self._read(key, super().pop(key, *default))


@pytest.mark.parametrize("after_read", [1, 2])
async def test_settle_between_reads_of_the_card_never_approves_the_next_request(
    monkeypatch, after_read,
):
    """req-a (card 1) times out on the agent thread while the person taps
    "Always allow" on card 1; req-b (card 2) is a different command pending in
    the same session. The tap must approve nothing, at every point."""
    adapter, _, patches = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake)
    fake.enqueue("s", "req-a")
    fake.enqueue("s", "req-b")
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")  # card 1, message 500
    await _send_like_v0226(adapter, session_key="s", request_id="req-b")  # card 2, message 501

    store = _SettleAfterRead(
        adapter._approval_state, approval_id=1, after_read=after_read,
        settle=lambda: fake.time_out("s", "req-a"),
    )
    adapter._approval_state = store
    await adapter._handle_callback(_tap(1, "always", 500))

    assert store.fired, "the forced settle never ran: the test proves nothing"
    assert fake.results == {}
    assert fake.pending("s") == ["req-b"]
    assert not any(c[3] is None for c in fake.calls), fake.calls
    assert patches[-1]["message_id"] == 500
    assert "Approval expired" in patches[-1]["text"]

    # Card 2 still answers req-b, and only req-b.
    await adapter._handle_callback(_tap(2, "once", 501))
    assert fake.results == {"req-b": "once"}


async def test_id_aware_hermes_never_resolves_a_card_first_in_first_out(monkeypatch):
    """Belt: once the running Hermes resolves by request id, a card that has
    no request id (it should never happen) approves nothing rather than the
    oldest request in the session."""
    adapter, _, patches = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake)
    fake.enqueue("s", "req-a")
    fake.enqueue("s", "req-b")
    await _send_like_v0226(adapter, session_key="s", request_id=None)

    await adapter._handle_callback(_tap(1, "always", 500))

    assert fake.results == {}
    assert fake.pending("s") == ["req-a", "req-b"]
    assert fake.calls == []
    assert "Approval expired" in patches[-1]["text"]


async def test_id_aware_resolver_without_card_helper_never_goes_first_in_first_out(
    monkeypatch,
):
    """Same belt on v0.22.6 / v0.22.7: resolve_gateway_approval takes
    request_id, there is no card helper."""
    adapter, _, patches = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake, card=False, observe=False)
    fake.enqueue("s", "req-a")
    await _send_like_v0226(adapter, session_key="s", request_id=None)

    await adapter._handle_callback(_tap(1, "once", 500))

    assert fake.results == {}
    assert fake.pending("s") == ["req-a"]
    assert "Approval expired" in patches[-1]["text"]


# --- Card ids restart per adapter instance: a tap is bound to its message ----


async def test_old_live_card_after_a_gateway_restart_approves_nothing(monkeypatch):
    """Card A (`ea:*:1`, message 500) is still live in the app when the
    gateway restarts; the next approval posts card B, again `ea:*:1`, as
    message 600. A tap on card A must not answer card B."""
    fake = FakeApprovalQueue()
    old, _, _ = _adapter(monkeypatch)
    _install(monkeypatch, fake)
    fake.enqueue("s", "req-a")
    await _send_like_v0226(old, session_key="s", request_id="req-a")
    fake.queues.clear()  # the old request dies with the old process

    new, posts, patches = _adapter(monkeypatch, first_message_id=600)
    fake.enqueue("s2", "req-b")  # even in another session
    await _send_like_v0226(new, session_key="s2", request_id="req-b")
    assert posts[0]["options"][0]["callbackData"] == "ea:once:1"

    await new._handle_callback(_tap(1, "always", 500))

    assert fake.results == {}
    assert fake.pending("s2") == ["req-b"]
    assert patches[-1]["message_id"] == 500
    assert "Approval expired" in patches[-1]["text"]

    # The tap on the old card did not use up card B.
    await new._handle_callback(_tap(1, "once", 600))
    assert fake.results == {"req-b": "once"}
    assert "Approved once" in patches[-1]["text"]


async def test_old_card_after_an_adapter_rebuild_approves_nothing(monkeypatch):
    """Hermes rebuilds platform adapters on reconnect. The old request stays
    pending in tools.approval, the new instance counts from 1 again. A tap on
    the old card (camelCase payload, as the live WS sends it) must not
    resolve the new instance's card 1."""
    fake = FakeApprovalQueue()
    old, _, _ = _adapter(monkeypatch)
    _install(monkeypatch, fake)
    fake.enqueue("s", "req-a")
    await _send_like_v0226(old, session_key="s", request_id="req-a")  # ea:*:1, message 500

    new, _, _ = _adapter(monkeypatch, first_message_id=600)
    fake.enqueue("s", "req-b")
    await _send_like_v0226(new, session_key="s", request_id="req-b")  # ea:*:1, message 600

    await new._handle_callback(
        {"callbackData": "ea:always:1", "userId": "user_1", "messageId": 500, "chatId": 11},
    )

    assert fake.results == {}
    assert fake.pending("s") == ["req-a", "req-b"]


async def test_tap_without_the_cards_message_id_approves_nothing(monkeypatch):
    adapter, _, _ = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake)
    fake.enqueue("s", "req-a")
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")

    await adapter._handle_callback({"callback_data": "ea:always:1", "user_id": "user_1"})

    assert fake.results == {}
    assert fake.pending("s") == ["req-a"]
    await adapter._handle_callback(_tap(1, "once", 500))
    assert fake.results == {"req-a": "once"}


# --- Older Hermes ---------------------------------------------------------------


async def test_older_hermes_without_request_id_resolves_first_in_first_out(monkeypatch):
    adapter, _, patches = _adapter(monkeypatch)
    resolved: list[tuple] = []

    def old_resolve(session_key, choice):  # pre v0.22.6 two argument call
        resolved.append((session_key, choice))

    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", old_resolve)
    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval_card", None)
    monkeypatch.setattr(adapter_mod, "observe_gateway_settle", None)

    await adapter.send_exec_approval(
        chat_id=11, command="x", session_key="s", description="d",
        allow_permanent=True, allow_session=True, smart_denied=False,
    )
    await adapter._handle_callback(_tap(1, "session", 500))

    assert resolved == [("s", "session")]
    assert "Approved for session" in patches[-1]["text"]


async def test_hermes_with_request_id_but_no_card_resolver_targets_the_request(monkeypatch):
    """v0.22.6 and v0.22.7 pass request_id and their resolve_gateway_approval
    takes it, but have no resolve_gateway_approval_card yet."""
    adapter, _, patches = _adapter(monkeypatch)
    fake = FakeApprovalQueue()
    _install(monkeypatch, fake, card=False, observe=False)

    fake.enqueue("s", "req-a")
    await _send_like_v0226(adapter, session_key="s", request_id="req-a")
    fake.time_out("s", "req-a")
    fake.enqueue("s", "req-b")

    await adapter._handle_callback(_tap(1, "once", 500))

    assert fake.pending("s") == ["req-b"]
    assert ("resolve", "s", "once", "req-a") in fake.calls
    assert "Approval expired" in patches[-1]["text"]
