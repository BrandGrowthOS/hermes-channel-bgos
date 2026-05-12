# BGOS Hermes Adapter — Telegram Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the BGOS channel adapter to feature parity with Hermes's Telegram adapter — unlock the gateway-driven tool-progress / streaming / typing UX, polish approvals to match Telegram's in-place edit behavior, add outbound formatting + length splitting + media-group support, add inbound text batching.

**Architecture:** This package is a vendor plugin paired with a thin Hermes fork. The 80% unlock is overriding three optional methods on `BasePlatformAdapter` (`edit_message`, `delete_message`, `send_typing`) — the gateway's `GatewayRunner` then drives the whole tool-progress UI against those overrides automatically (see Hermes upstream `gateway/run.py:14370`: `if type(adapter).edit_message is BasePlatformAdapter.edit_message:` short-circuits the entire feature). Everything else is incremental polish layered on top.

**Tech Stack:** Python 3.11+, httpx (REST), python-socketio (WS), pytest+pytest-asyncio. Tests use the existing `MockBgosServer` (aiohttp + Socket.IO).

**Backend dependencies (out of scope for this plan; documented for later):** `DELETE /api/v1/messages/{id}`, WS `typing` event handler, client UI for streaming/edited message states. Adapter is engineered to degrade gracefully when backend support is missing — a 404 or 501 from the backend turns into `SendResult(success=False)` and the gateway falls back.

---

## Chunk 1 — UX Engine Unlock (the 80/20)

After this chunk: the BGOS chat shows animated tool-progress bubbles with emoji prefixes (🔍, 🧠, 💻, 🔧, ⚡…), streaming token edits for the final reply, typing indicators between tools, intermediate-preview cleanup. Cascades from three method overrides.

### Task 1.1: Add `BgosApi.delete_message(message_id)`

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_api.py` (add method after `patch_message`)
- Test: `tests/test_bgos_api.py` (add test class)

- [ ] **Step 1: Write the failing test** in `tests/test_bgos_api.py`:

```python
async def test_delete_message_sends_delete_request(mock_bgos_server):
    mock_bgos_server.on("DELETE", "/api/v1/messages/42").respond(204)
    config = BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok")
    async with BgosApi(config) as api:
        await api.delete_message(42)
    req = mock_bgos_server.last_request("DELETE", "/api/v1/messages/42")
    assert req.headers["X-BGOS-Pairing"] == "tok"


async def test_delete_message_swallows_404_as_None(mock_bgos_server):
    """Backend may legitimately return 404 if the message was already
    deleted or never existed. Adapter callers tolerate that — raise only
    on unexpected errors (5xx)."""
    mock_bgos_server.on("DELETE", "/api/v1/messages/9999").respond(404)
    config = BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok")
    async with BgosApi(config) as api:
        with pytest.raises(BgosApiError) as exc:
            await api.delete_message(9999)
        assert exc.value.status == 404
```

- [ ] **Step 2: Run test to verify it fails**: `pytest tests/test_bgos_api.py -k delete_message -v` → FAIL (`AttributeError: 'BgosApi' object has no attribute 'delete_message'`)

- [ ] **Step 3: Implement** in `src/hermes_channel_bgos/bgos_api.py` after `patch_message`:

```python
async def delete_message(self, message_id: int) -> None:
    """DELETE /api/v1/messages/{id}. Raises BgosApiError on 4xx/5xx
    (including 404 — callers decide whether to swallow). The adapter's
    delete_message override DOES swallow 404/501 and returns False so
    the gateway can fall back to leaving the message in place."""
    await self._request("DELETE", f"/api/v1/messages/{message_id}")
```

- [ ] **Step 4: Run test to verify it passes**: `pytest tests/test_bgos_api.py -k delete_message -v` → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_api.py tests/test_bgos_api.py
git commit -m "feat: add BgosApi.delete_message"
```

### Task 1.2: Add `BgosWs.emit_typing()`

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_ws.py` (add method after `stop`)
- Test: `tests/test_bgos_ws.py` (add test)

- [ ] **Step 1: Write the failing test** in `tests/test_bgos_ws.py`:

```python
async def test_emit_typing_emits_to_server(mock_bgos_server):
    """typing is a fire-and-forget WS event; backend forwards to clients."""
    config = BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok")
    ws = BgosWs(config, on_inbound_message=lambda d: None,
                on_callback_result=lambda d: None)
    ws.bind_pairing(7)
    ws.bind_assistants([3])

    # Capture typing events on the server side
    typing_events: list[dict] = []
    @mock_bgos_server._sio.on("typing")
    async def _typing(sid, data):
        typing_events.append(data)

    await ws.start()
    await mock_bgos_server.wait_for_socket_connection()
    await ws.emit_typing(chat_id=42, assistant_id=3)
    # Allow event to round-trip
    await asyncio.sleep(0.1)
    await ws.stop()

    assert len(typing_events) == 1
    assert typing_events[0]["chatId"] == 42
    assert typing_events[0]["assistantId"] == 3


async def test_emit_typing_is_noop_when_disconnected():
    """If WS isn't connected, emit_typing should NOT raise — typing is
    purely cosmetic; the gateway shouldn't see an error path here."""
    config = BgosConfig(base_url="http://nowhere", pairing_token="tok")
    ws = BgosWs(config, on_inbound_message=lambda d: None,
                on_callback_result=lambda d: None)
    # Never start — _sio.connected is False
    await ws.emit_typing(chat_id=1, assistant_id=1)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**: `pytest tests/test_bgos_ws.py -k emit_typing -v` → FAIL

- [ ] **Step 3: Implement** in `src/hermes_channel_bgos/bgos_ws.py` after `stop`:

```python
async def emit_typing(self, *, chat_id: int, assistant_id: int) -> None:
    """Emit a `typing` Socket.IO event so the backend can forward an
    ephemeral typing indicator to clients viewing this chat.

    Best-effort: if the WS isn't connected, returns silently. The
    backend may not handle this event yet — Socket.IO drops unknown
    events server-side, so this is forward-safe.
    """
    if not self._sio.connected:
        return
    try:
        await self._sio.emit("typing", {
            "chatId": chat_id,
            "assistantId": assistant_id,
        })
    except Exception:
        # Typing is cosmetic; never let it bubble up to the gateway.
        log.debug("emit_typing failed (non-fatal)", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**: `pytest tests/test_bgos_ws.py -k emit_typing -v` → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_ws.py tests/test_bgos_ws.py
git commit -m "feat: add BgosWs.emit_typing"
```

### Task 1.3: Implement `BGOSAdapter.edit_message()`

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (add method after `send`, before media block; ~line 630)
- Test: `tests/test_bgos_adapter.py` (add tests)

- [ ] **Step 1: Write failing tests** in `tests/test_bgos_adapter.py`:

```python
async def test_edit_message_calls_patch_message(monkeypatch):
    """edit_message → BgosApi.patch_message, returns SendResult with
    the same message_id so the gateway tracks the streaming bubble."""
    adapter = _make_adapter()
    captured = []
    async def fake_patch(message_id, *, text=None, approval_meta=None):
        captured.append({"message_id": message_id, "text": text})
        return {"id": message_id, "text": text}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)

    result = await adapter.edit_message(
        chat_id=100, message_id="55", content="updated text"
    )
    assert result.success is True
    assert result.message_id == "55"
    assert captured == [{"message_id": 55, "text": "updated text"}]


async def test_edit_message_returns_failure_on_404(monkeypatch):
    """Backend may legitimately reject edits (message too old, deleted,
    not editable) — that's not an error path that should crash the
    gateway. Return SendResult(success=False) so caller falls back to
    sending a fresh message."""
    from hermes_channel_bgos.bgos_api import BgosApiError
    adapter = _make_adapter()
    async def fake_patch(*a, **kw):
        raise BgosApiError(404, None, {"error": "not_found"})
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)

    result = await adapter.edit_message(
        chat_id=100, message_id="55", content="x"
    )
    assert result.success is False
    assert "not_editable" in (result.error or "") or "not_found" in (result.error or "")


async def test_edit_message_parses_buttons_block(monkeypatch):
    """edit_message must strip [[BGOS_BUTTONS]] markers from text and
    forward parsed options — same contract as send()."""
    adapter = _make_adapter()
    captured = []
    async def fake_patch(message_id, *, text=None, approval_meta=None, options=None, render_mode=None):
        captured.append({"text": text, "options": options})
        return {"id": message_id}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    content = "ack\n[[BGOS_BUTTONS]]\nYes | yes\nNo | no\n[[/BGOS_BUTTONS]]"
    await adapter.edit_message(chat_id=1, message_id="9", content=content)
    assert "BGOS_BUTTONS" not in captured[0]["text"]
    assert captured[0]["options"] == [
        {"text": "Yes", "callbackData": "yes"},
        {"text": "No",  "callbackData": "no"},
    ]


async def test_edit_message_drops_options_when_block_absent(monkeypatch):
    """When the agent edits previously-button-bearing message to plain text,
    options must be cleared from the row, not left stale."""
    adapter = _make_adapter()
    captured = []
    async def fake_patch(message_id, *, text=None, approval_meta=None, options=None, render_mode=None):
        captured.append({"options": options})
        return {"id": message_id}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    await adapter.edit_message(chat_id=1, message_id="9", content="just text")
    # When no buttons block present, send `options=[]` so backend clears
    # any pre-existing keyboard
    assert captured[0]["options"] == []
```

`_make_adapter()` is the existing helper in test_bgos_adapter.py — reuse it.

- [ ] **Step 2: Run tests to verify they fail**: `pytest tests/test_bgos_adapter.py -k edit_message -v` → FAIL (4 FAILED, AttributeError)

- [ ] **Step 3: Extend `BgosApi.patch_message`** in `src/hermes_channel_bgos/bgos_api.py` to accept `options` + `render_mode`:

```python
async def patch_message(
    self,
    message_id: int,
    *,
    text: str | None = None,
    approval_meta: dict | None = None,
    options: list[dict] | None = None,
    render_mode: str | None = None,
) -> dict:
    body: dict[str, Any] = {}
    if text is not None:
        body["text"] = text
    if approval_meta is not None:
        body["approvalMeta"] = approval_meta
    if options is not None:
        body["options"] = options
    if render_mode is not None:
        body["renderMode"] = render_mode
    return await self._request("PATCH", f"/api/v1/messages/{message_id}", json=body)
```

- [ ] **Step 4: Implement `edit_message`** in `src/hermes_channel_bgos/bgos_adapter.py` after `send` (~line 630):

```python
async def edit_message(
    self,
    chat_id: int | str,
    message_id: int | str,
    content: str,
    *,
    finalize: bool = False,
) -> SendResult:
    """Edit a previously-sent message via PATCH /api/v1/messages/{id}.

    The gateway's stream consumer and tool-progress loop call this
    repeatedly to animate streaming responses and edit-in-place tool
    bubbles (see Hermes `gateway/run.py:14370` — having edit_message
    overridden is what UNLOCKS the entire tool-progress UI, since the
    gateway short-circuits the whole code path when the adapter inherits
    the base-class default).

    `finalize` is a no-op for BGOS (the backend has no draft/finalize
    state machine — every edit is just an edit). Accepted for interface
    compatibility with Hermes's BasePlatformAdapter.

    Buttons: respects the same `[[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]`
    marker block as send(). When the block is absent we send
    `options=[]` so the backend CLEARS any prior keyboard — necessary
    for the typical streaming pattern where the first send carried
    buttons and the streamed update is plain text.

    Returns SendResult(success=False) on 4xx (message too old / not
    editable / deleted) so the gateway falls back to a fresh send().
    """
    cleaned_text, options, render_mode = _parse_buttons_block(content)
    try:
        await self._api.patch_message(
            int(message_id),
            text=cleaned_text,
            options=options if options is not None else [],
            render_mode=render_mode,
        )
    except BgosApiError as exc:
        if 400 <= exc.status < 500:
            return SendResult(success=False, error=f"not_editable_{exc.status}")
        raise
    return _send_result(message_id=str(message_id))
```

At the top of the file, ensure `BgosApiError` is imported:

```python
from .bgos_api import BgosApi, BgosApiError
```

- [ ] **Step 5: Run tests**: `pytest tests/test_bgos_adapter.py -k edit_message -v` → PASS

- [ ] **Step 6: Verify it overrides the base** by also checking the dispatching check Hermes uses:

```python
# Add to tests/test_bgos_adapter.py
def test_edit_message_overrides_base_unlocks_tool_progress():
    """The Hermes gateway short-circuits tool-progress UI when
    `type(adapter).edit_message is BasePlatformAdapter.edit_message`.
    Ensure our override is detected as different from the base."""
    from hermes_channel_bgos.bgos_adapter import BGOSAdapter, BasePlatformAdapter
    assert BGOSAdapter.edit_message is not BasePlatformAdapter.edit_message
```

Run: `pytest tests/test_bgos_adapter.py::test_edit_message_overrides_base_unlocks_tool_progress -v` → PASS

- [ ] **Step 7: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py src/hermes_channel_bgos/bgos_api.py tests/test_bgos_adapter.py
git commit -m "feat: implement edit_message override - unlocks gateway tool-progress UX"
```

### Task 1.4: Implement `BGOSAdapter.delete_message()`

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (add method after `edit_message`)
- Test: `tests/test_bgos_adapter.py`

- [ ] **Step 1: Write failing tests** in `tests/test_bgos_adapter.py`:

```python
async def test_delete_message_returns_true_on_success(monkeypatch):
    adapter = _make_adapter()
    called_with = []
    async def fake_del(mid):
        called_with.append(mid)
    monkeypatch.setattr(adapter._api, "delete_message", fake_del)
    ok = await adapter.delete_message(chat_id=1, message_id="42")
    assert ok is True
    assert called_with == [42]


async def test_delete_message_returns_false_on_404(monkeypatch):
    """Already-deleted / nonexistent — non-fatal; just return False so
    the gateway leaves the bubble in place."""
    from hermes_channel_bgos.bgos_api import BgosApiError
    adapter = _make_adapter()
    async def fake_del(mid):
        raise BgosApiError(404, None, {"error": "not_found"})
    monkeypatch.setattr(adapter._api, "delete_message", fake_del)
    ok = await adapter.delete_message(chat_id=1, message_id="42")
    assert ok is False


async def test_delete_message_returns_false_on_501(monkeypatch):
    """Backend doesn't implement DELETE yet — graceful degradation."""
    from hermes_channel_bgos.bgos_api import BgosApiError
    adapter = _make_adapter()
    async def fake_del(mid):
        raise BgosApiError(501, None, {"error": "not_implemented"})
    monkeypatch.setattr(adapter._api, "delete_message", fake_del)
    ok = await adapter.delete_message(chat_id=1, message_id="42")
    assert ok is False


def test_delete_message_overrides_base():
    from hermes_channel_bgos.bgos_adapter import BGOSAdapter, BasePlatformAdapter
    assert BGOSAdapter.delete_message is not BasePlatformAdapter.delete_message
```

- [ ] **Step 2: Run tests**: → FAIL

- [ ] **Step 3: Implement** after `edit_message`:

```python
async def delete_message(
    self,
    chat_id: int | str,
    message_id: int | str,
) -> bool:
    """Delete a previously-sent message via DELETE /api/v1/messages/{id}.

    Used by the gateway's stream consumer to clean up intermediate
    streaming-preview messages once the final answer is delivered as a
    fresh message (so the visible timestamp reflects completion time
    rather than start-of-stream).

    Returns False on any HTTP error (404 = already deleted; 501 =
    backend doesn't implement DELETE yet) so the caller falls back to
    leaving the message visible. Only re-raises 5xx that aren't 501,
    which would indicate a real backend incident.
    """
    try:
        await self._api.delete_message(int(message_id))
        return True
    except BgosApiError as exc:
        if 400 <= exc.status < 500 or exc.status == 501:
            log.debug("delete_message message_id=%s failed: %s", message_id, exc)
            return False
        raise
```

- [ ] **Step 4: Run tests**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter.py
git commit -m "feat: implement delete_message override"
```

### Task 1.5: Implement `BGOSAdapter.send_typing()`

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_bgos_adapter.py`

- [ ] **Step 1: Write failing tests** in `tests/test_bgos_adapter.py`:

```python
async def test_send_typing_emits_via_ws(monkeypatch):
    adapter = _make_adapter()
    # Adapter doesn't have a connected WS in this test — install a stub
    captured = []
    class _StubWs:
        async def emit_typing(self, *, chat_id, assistant_id):
            captured.append({"chat_id": chat_id, "assistant_id": assistant_id})
    adapter._ws = _StubWs()
    # send_typing needs to know which assistant_id is bound to this chat
    # Since BGOS is DM-only, any bound assistant is correct; pick the
    # only one in the state map
    adapter._state.set_route(7, "default")
    await adapter.send_typing(chat_id=42)
    assert captured == [{"chat_id": 42, "assistant_id": 7}]


async def test_send_typing_is_noop_when_ws_absent():
    adapter = _make_adapter()
    adapter._ws = None
    # Must not raise
    await adapter.send_typing(chat_id=42)


async def test_send_typing_swallows_exceptions(monkeypatch):
    """Cosmetic feature — never bubble errors back to the gateway."""
    adapter = _make_adapter()
    class _BrokenWs:
        async def emit_typing(self, **kw):
            raise RuntimeError("boom")
    adapter._ws = _BrokenWs()
    adapter._state.set_route(7, "default")
    await adapter.send_typing(chat_id=42)  # must not raise
```

- [ ] **Step 2: Run tests**: → FAIL

- [ ] **Step 3: Implement** at the end of the class:

```python
async def send_typing(
    self,
    chat_id: int | str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a typing indicator over WS for this chat.

    The gateway calls this between tool-progress edits and during
    long-running tool calls so the user sees the bot is still alive.
    BGOS is currently DM-only — one assistant per pairing — so we pick
    the only assistant in the route map. If multi-assistant pairings
    are added later, `metadata` could carry an explicit assistant_id.

    Cosmetic — never raises.
    """
    if self._ws is None:
        return
    # Pick an assistant_id from metadata if provided, else fall back to
    # the first assistant bound to this pairing.
    assistant_id: int | None = None
    if metadata and isinstance(metadata, dict):
        candidate = metadata.get("assistant_id")
        if isinstance(candidate, int):
            assistant_id = candidate
    if assistant_id is None:
        for aid in self._state.assistant_route:
            assistant_id = aid
            break
    if assistant_id is None:
        return
    try:
        await self._ws.emit_typing(
            chat_id=int(chat_id), assistant_id=assistant_id,
        )
    except Exception:
        log.debug("send_typing failed (non-fatal)", exc_info=True)
```

- [ ] **Step 4: Run tests**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter.py
git commit -m "feat: implement send_typing over WS"
```

### Task 1.6: Per-chat edit throttle (1.5s) — protect backend from edit storms

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_bgos_adapter.py`

- [ ] **Step 1: Write failing test** — verifying that edits within 1.5s of each other for the same chat are coalesced:

```python
async def test_edit_message_throttled_to_one_per_chat_per_interval(monkeypatch):
    """Inside the 1.5s throttle window, only the LAST content is flushed
    to the backend — intermediate edits are dropped. Mirrors Telegram's
    pattern (run.py:14382 _PROGRESS_EDIT_INTERVAL = 1.5)."""
    import time
    adapter = _make_adapter()
    adapter._edit_throttle_seconds = 0.1  # tiny window so test runs fast
    call_log: list[str] = []
    async def fake_patch(mid, *, text=None, **kw):
        call_log.append(text)
        return {"id": mid}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)

    # Three rapid edits, same chat, same message_id
    await adapter.edit_message(chat_id=1, message_id="9", content="v1")
    await adapter.edit_message(chat_id=1, message_id="9", content="v2")
    await adapter.edit_message(chat_id=1, message_id="9", content="v3")
    # Within the 0.1s window, only the first edit goes out immediately.
    # The pending v3 flushes once the window expires.
    assert "v1" in call_log
    # Wait for the deferred flush
    await asyncio.sleep(0.2)
    assert call_log[-1] == "v3"
    # v2 was superseded mid-window and never sent
    assert "v2" not in call_log


async def test_edits_to_different_chats_are_independent(monkeypatch):
    """Throttle is per-chat — two chats can each get one edit per interval."""
    adapter = _make_adapter()
    adapter._edit_throttle_seconds = 0.1
    call_log: list[tuple[int, str]] = []
    async def fake_patch(mid, *, text=None, **kw):
        call_log.append((mid, text))
        return {"id": mid}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)

    await adapter.edit_message(chat_id=1, message_id="9", content="chat1")
    await adapter.edit_message(chat_id=2, message_id="10", content="chat2")
    # Both go out immediately — different chats, no throttle interference
    assert (9, "chat1") in call_log
    assert (10, "chat2") in call_log
```

- [ ] **Step 2: Run tests**: → FAIL

- [ ] **Step 3: Implement the throttle** in `__init__`:

```python
# Add to __init__ (around line 318, after _poll_task):
self._edit_throttle_seconds: float = 1.5
self._pending_edits: dict[int, asyncio.Task] = {}  # chat_id → pending flush task
self._last_edit_at: dict[int, float] = {}            # chat_id → monotonic ts
self._pending_edit_content: dict[int, tuple[int, str]] = {}  # chat_id → (message_id, content)
```

Replace the body of `edit_message` with throttle logic:

```python
async def edit_message(
    self, chat_id, message_id, content, *, finalize: bool = False,
) -> SendResult:
    cleaned_text, options, render_mode = _parse_buttons_block(content)
    chat_key = int(chat_id)
    mid_int = int(message_id)
    now = asyncio.get_event_loop().time()
    last = self._last_edit_at.get(chat_key, 0.0)
    elapsed = now - last
    if elapsed >= self._edit_throttle_seconds:
        # Fire immediately; cancel any pending flush since we just spoke
        pending = self._pending_edits.pop(chat_key, None)
        if pending and not pending.done():
            pending.cancel()
        self._pending_edit_content.pop(chat_key, None)
        result = await self._do_patch(mid_int, cleaned_text, options, render_mode)
        self._last_edit_at[chat_key] = now
        return result
    # Within window — stash the latest content, schedule a deferred flush
    self._pending_edit_content[chat_key] = (mid_int, cleaned_text)
    if chat_key not in self._pending_edits or self._pending_edits[chat_key].done():
        wait_for = self._edit_throttle_seconds - elapsed
        self._pending_edits[chat_key] = asyncio.create_task(
            self._deferred_flush(chat_key, wait_for, options, render_mode)
        )
    # Caller still wants a SendResult — pretend success; the deferred
    # flush will surface real failures via logging.
    return _send_result(message_id=str(mid_int))


async def _do_patch(
    self, mid_int: int, text: str, options, render_mode,
) -> SendResult:
    try:
        await self._api.patch_message(
            mid_int, text=text,
            options=options if options is not None else [],
            render_mode=render_mode,
        )
    except BgosApiError as exc:
        if 400 <= exc.status < 500:
            return SendResult(success=False, error=f"not_editable_{exc.status}")
        raise
    return _send_result(message_id=str(mid_int))


async def _deferred_flush(
    self, chat_key: int, wait_for: float, options, render_mode,
) -> None:
    try:
        await asyncio.sleep(wait_for)
        pending = self._pending_edit_content.pop(chat_key, None)
        if pending is None:
            return
        mid_int, text = pending
        try:
            await self._do_patch(mid_int, text, options, render_mode)
        except Exception:
            log.warning("deferred edit flush failed chat=%d msg=%d",
                        chat_key, mid_int, exc_info=True)
        self._last_edit_at[chat_key] = asyncio.get_event_loop().time()
    except asyncio.CancelledError:
        return
```

Adjust earlier tests in this chunk: the test calling `edit_message` once and expecting immediate result still works (first call → `elapsed >= throttle`). The test for buttons/options/no-options-clears still works.

- [ ] **Step 4: Run all edit_message tests**: `pytest tests/test_bgos_adapter.py -k edit_message -v` → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter.py
git commit -m "feat: throttle edit_message to 1 per 1.5s per chat"
```

### Task 1.7: Wire `disconnect()` to cancel pending flushes

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (extend `disconnect`)
- Test: `tests/test_bgos_adapter.py`

- [ ] **Step 1: Write failing test**:

```python
async def test_disconnect_cancels_pending_edit_flushes(monkeypatch):
    """Pending deferred-flush tasks must be cancelled on disconnect so
    the event loop doesn't leak unawaited tasks."""
    adapter = _make_adapter()
    adapter._edit_throttle_seconds = 5.0  # long, ensures task pends
    monkeypatch.setattr(adapter._api, "patch_message",
                        lambda *a, **kw: asyncio.sleep(0, result={"id": 1}))
    await adapter.edit_message(chat_id=1, message_id="9", content="v1")  # immediate
    await adapter.edit_message(chat_id=1, message_id="9", content="v2")  # pending
    assert any(not t.done() for t in adapter._pending_edits.values())
    await adapter.disconnect()
    assert all(t.done() for t in adapter._pending_edits.values())
```

- [ ] **Step 2: Run test**: → FAIL

- [ ] **Step 3: Extend `disconnect`** at line 580:

```python
async def disconnect(self) -> None:
    # ... existing logic ...
    # Cancel pending edit flushes
    for task in list(self._pending_edits.values()):
        if not task.done():
            task.cancel()
    self._pending_edits.clear()
    self._pending_edit_content.clear()
    # ... rest of existing logic
```

(Place these lines BEFORE the existing `self._poll_task.cancel()` so all task cancels are grouped.)

- [ ] **Step 4: Run test**: → PASS

- [ ] **Step 5: Run full suite**: `pytest -q` → all PASS (95+ tests including new ones)

- [ ] **Step 6: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter.py
git commit -m "fix: cancel pending edit flushes on disconnect"
```

### Task 1.8: Bump version + update README "Status" section

**Files:**
- Modify: `pyproject.toml` (version bump 0.4.0 → 0.5.0)
- Modify: `README.md` (Status note, capability bullets)
- Modify: `docs/bgos-agent-capabilities.md` (mark edit/delete/typing as shipped)

- [ ] **Step 1: Bump version** in `pyproject.toml`:
```toml
version = "0.5.0"
```

- [ ] **Step 2: Update README.md** — add a row to capability bullets near the top of the file, e.g. after the "Status" paragraph:
"v0.5.0 unlocks the gateway-driven tool-progress UI: agent runs now show emoji-prefixed tool bubbles, edit-in-place streaming, typing indicators, and intermediate-preview cleanup. Requires no Hermes fork changes; works out of the box on a `git pull` of the vendor package."

- [ ] **Step 3: Update docs/bgos-agent-capabilities.md** — mark Tool Progress, Streaming, Typing, Edit/Delete as shipped (move from "Not yet wired" to a Phase 1 shipped list).

- [ ] **Step 4: Commit**:
```bash
git add pyproject.toml README.md docs/bgos-agent-capabilities.md
git commit -m "chore: bump 0.4.0 -> 0.5.0; document edit/delete/typing"
```

### Task 1.9: Run code review

- [ ] Use the `Agent` tool with a code-reviewer prompt against the diff `git diff main...HEAD` for the worktree branch. Review for: graceful error handling, no logic leaks across the throttle, no test flake from monotonic timing, correct interface against `BasePlatformAdapter`.

---

## Chunk 2 — Approval Polish

After this chunk: clicking an approval button replaces it in-place with "✅ Approved once by @user" matching Telegram; slash-confirm 3-button UI works; per-user callback authz is enforced.

### Task 2.1: Edit approval message in-place on callback

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (`_handle_callback`, ~line 972)
- Test: `tests/test_approval_handler.py`

- [ ] **Step 1: Write failing test** in `tests/test_approval_handler.py`:

```python
async def test_approval_callback_edits_message_in_place(monkeypatch):
    """After resolving an approval, the original bubble should be edited
    to show the choice + user — matches Telegram's UX where buttons
    disappear and the message shows the resolution."""
    adapter = _make_adapter()
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_gateway_approval",
        lambda sk, choice: None,
    )
    captured_patches = []
    async def fake_patch(mid, *, text=None, options=None, **kw):
        captured_patches.append({"message_id": mid, "text": text, "options": options})
        return {"id": mid}
    monkeypatch.setattr(adapter._api, "patch_message", fake_patch)
    # Seed an approval the gateway issued
    adapter._approval_state[7] = "session-abc"
    # Backend callback_result event includes message_id of the approval bubble
    await adapter._handle_callback({
        "callback_data": "ea:once:7",
        "user_id": "user_42",
        "message_id": 99,
    })
    assert captured_patches[0]["message_id"] == 99
    assert "Approved once" in captured_patches[0]["text"]
    assert "user_42" in captured_patches[0]["text"]
    # Buttons removed (empty options list)
    assert captured_patches[0]["options"] == []
```

- [ ] **Step 2: Run test**: → FAIL

- [ ] **Step 3: Extend `_handle_callback`** to call `edit_message`:

```python
# In _handle_callback after `_self_mod.resolve_gateway_approval(session_key, choice)`
# but inside the same branch (~line 1003):
choice_labels = {
    "once":    "✅ Approved once",
    "session": "✅ Approved for session",
    "always":  "🔒 Approved permanently",
    "deny":    "❌ Denied",
}
label = choice_labels.get(choice, choice)
user_id = data.get("user_id") or ""
patched_text = f"{label}" + (f" by {user_id}" if user_id else "")
msg_id = data.get("message_id")
chat_id = data.get("chat_id") or data.get("chatId")
if isinstance(msg_id, int) and isinstance(chat_id, int):
    # Side-step the throttle (cosmetic, but instant matters here)
    try:
        await self._api.patch_message(
            msg_id, text=patched_text, options=[],
        )
    except Exception:
        log.warning("approval-callback message edit failed", exc_info=True)
```

- [ ] **Step 4: Run test**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_approval_handler.py
git commit -m "feat: edit approval message in-place after callback"
```

### Task 2.2: Per-user authorization on callbacks

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (`_handle_callback`)
- Test: `tests/test_callback_router.py`

- [ ] **Step 1: Write failing test**:

```python
async def test_approval_callback_rejects_unauthorized_user(monkeypatch):
    """When BGOS_ALLOWED_USERS is set, callbacks from users NOT in that
    set should be silently dropped — same model as inbound message auth.
    Telegram's pattern: telegram.py:405 _is_callback_user_authorized."""
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "false")
    monkeypatch.setenv("BGOS_ALLOWED_USERS", "user_authorized")
    adapter = _make_adapter()
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_gateway_approval",
        lambda sk, choice: pytest.fail("should not resolve when unauthorized"),
    )
    adapter._approval_state[1] = "session-x"
    await adapter._handle_callback({
        "callback_data": "ea:once:1",
        "user_id": "user_intruder",
        "message_id": 99,
        "chat_id": 1,
    })
    # Approval still pending — state not cleared
    assert 1 in adapter._approval_state


async def test_approval_callback_allows_authorized_user(monkeypatch):
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "false")
    monkeypatch.setenv("BGOS_ALLOWED_USERS", "user_42")
    adapter = _make_adapter()
    resolved = []
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_gateway_approval",
        lambda sk, choice: resolved.append((sk, choice)),
    )
    monkeypatch.setattr(adapter._api, "patch_message",
                        lambda *a, **kw: asyncio.sleep(0, result={"id": 1}))
    adapter._approval_state[1] = "session-x"
    await adapter._handle_callback({
        "callback_data": "ea:once:1",
        "user_id": "user_42",
        "message_id": 99,
        "chat_id": 1,
    })
    assert resolved == [("session-x", "once")]
```

- [ ] **Step 2: Run tests**: → FAIL

- [ ] **Step 3: Implement** — add a `_is_callback_user_authorized(user_id)` method on `BGOSAdapter`:

```python
def _is_callback_user_authorized(self, user_id: str | None) -> bool:
    """Mirrors the inbound auth gate (BGOS_ALLOW_ALL_USERS /
    BGOS_ALLOWED_USERS) for callback events. Without this check, a
    malicious user who could trigger backend callback_result delivery
    could resolve approvals targeted at someone else."""
    if os.environ.get("BGOS_ALLOW_ALL_USERS", "false").lower() == "true":
        return True
    allowed = os.environ.get("BGOS_ALLOWED_USERS", "").strip()
    if not allowed:
        return False  # Same fail-closed default as Telegram
    allowed_set = {u.strip() for u in allowed.split(",") if u.strip()}
    return user_id is not None and str(user_id) in allowed_set
```

Then at the top of `_handle_callback` before processing:

```python
user_id = data.get("user_id")
if not self._is_callback_user_authorized(user_id):
    log.info("dropping unauthorized callback from user_id=%s", user_id)
    return
```

- [ ] **Step 4: Run tests**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_callback_router.py
git commit -m "feat: per-user authz on callback events"
```

### Task 2.3: `send_slash_confirm()` 3-button UI

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_approval_handler.py`

- [ ] **Step 1: Write failing test**:

```python
async def test_send_slash_confirm_renders_three_buttons(monkeypatch):
    adapter = _make_adapter()
    captured = []
    async def fake_post(**kw):
        captured.append(kw)
        return {"id": 50}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    result = await adapter.send_slash_confirm(
        chat_id=1, title="Reload MCP?",
        message="This invalidates the provider prompt cache.",
        session_key="sess-1", confirm_id="conf-abc",
    )
    assert result.success is True
    options = captured[0]["options"]
    callbacks = [o["callbackData"] for o in options]
    assert callbacks == [
        "sc:once:conf-abc",
        "sc:always:conf-abc",
        "sc:cancel:conf-abc",
    ]
    assert adapter._slash_confirm_state["conf-abc"] == "sess-1"


async def test_slash_confirm_callback_resolves(monkeypatch):
    """sc:<choice>:<id> callbacks route to slash_confirm.resolve()."""
    adapter = _make_adapter()
    resolved = []
    # Provide a stub since Hermes isn't installed
    import hermes_channel_bgos.bgos_adapter as mod
    mod.resolve_slash_confirm = lambda sk, cid, choice: (
        resolved.append((sk, cid, choice)) or None
    )
    adapter._slash_confirm_state["conf-1"] = "sess-99"
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    monkeypatch.setattr(adapter._api, "patch_message",
                        lambda *a, **kw: asyncio.sleep(0, result={"id": 1}))
    await adapter._handle_callback({
        "callback_data": "sc:once:conf-1",
        "user_id": "u_1",
        "message_id": 50,
        "chat_id": 1,
    })
    assert resolved == [("sess-99", "conf-1", "once")]
    assert "conf-1" not in adapter._slash_confirm_state
```

- [ ] **Step 2: Run tests**: → FAIL

- [ ] **Step 3: Implement** at module-top, add a try/import stub for `resolve_slash_confirm`:

```python
try:  # pragma: no cover
    from gateway.slash_confirm import resolve as resolve_slash_confirm  # type: ignore
except ImportError:
    def resolve_slash_confirm(session_key, confirm_id, choice):  # type: ignore[no-redef]
        raise RuntimeError("Hermes not installed — tests monkeypatch this")
```

Add a regex:
```python
_SLASH_CONFIRM_CALLBACK_RE = re.compile(r"^sc:(once|always|cancel):(.+)$")
```

In `__init__`, add:
```python
self._slash_confirm_state: dict[str, str] = {}
```

Implement `send_slash_confirm`:

```python
async def send_slash_confirm(
    self,
    chat_id: int | str,
    title: str,
    message: str,
    session_key: str,
    confirm_id: str,
    metadata: dict[str, Any] | None = None,
) -> SendResult:
    """Three-option slash-command confirmation (mirrors
    `gateway/platforms/telegram.py:2119`). Buttons:
      Approve Once / Always Approve 🔒 / Cancel
    Callback shape: sc:<choice>:<confirm_id>
    """
    options = [
        {"text": "✅ Approve Once",     "callbackData": f"sc:once:{confirm_id}",
         "style": "success", "row_index": 0},
        {"text": "🔒 Always Approve", "callbackData": f"sc:always:{confirm_id}",
         "style": "success", "row_index": 0},
        {"text": "❌ Cancel",          "callbackData": f"sc:cancel:{confirm_id}",
         "style": "danger",  "row_index": 1},
    ]
    body_text = f"**{title}**\n\n{message}" if title else message
    resp = await self._api.post_message(
        chat_id=int(chat_id),
        text=body_text,
        sender="assistant",
        message_type="slash_confirm",
        options=options,
    )
    self._slash_confirm_state[confirm_id] = session_key
    message_id = resp.get("id") if isinstance(resp, dict) else None
    return _send_result(message_id=message_id)
```

In `_handle_callback`, add routing BEFORE the existing approval branch:

```python
m_sc = _SLASH_CONFIRM_CALLBACK_RE.match(cb)
if m_sc is not None:
    choice = m_sc.group(1)
    confirm_id = m_sc.group(2)
    session_key = self._slash_confirm_state.pop(confirm_id, None)
    if session_key is None:
        log.info("stale slash-confirm click confirm_id=%s", confirm_id)
        return
    import hermes_channel_bgos.bgos_adapter as _self_mod
    _self_mod.resolve_slash_confirm(session_key, confirm_id, choice)
    # Edit in place
    choice_labels = {
        "once":   "✅ Approved once",
        "always": "🔒 Always approve",
        "cancel": "❌ Cancelled",
    }
    text = choice_labels.get(choice, choice)
    user_id = data.get("user_id") or ""
    if user_id:
        text += f" by {user_id}"
    msg_id = data.get("message_id")
    if isinstance(msg_id, int):
        try:
            await self._api.patch_message(msg_id, text=text, options=[])
        except Exception:
            log.warning("slash-confirm message edit failed", exc_info=True)
    return
```

- [ ] **Step 4: Run tests**: → PASS

- [ ] **Step 5: Run full suite**: `pytest -q` → all PASS

- [ ] **Step 6: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_approval_handler.py
git commit -m "feat: implement send_slash_confirm with 3-button UI"
```

### Task 2.4: Code review

- [ ] Dispatch code-reviewer subagent against `git diff <chunk-1-end>...HEAD`.

---

## Chunk 3 — Outbound Polish

After this chunk: messages over the BGOS length cap are auto-split with `(1/N)` markers; multi-image media groups POST as one message with N files; reply_to_id wiring is end-to-end-verified; raw markdown leaks (`*foo*` MDv2 escapes) are cleaned.

### Task 3.1: `format_message` for BGOS-native markdown

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_bgos_adapter.py`

- [ ] **Step 1: Write failing test**:

```python
def test_format_message_strips_telegram_specific_escapes():
    """If an agent generates MarkdownV2-style escapes (a leftover habit
    from Telegram-tuned prompts), strip them so BGOS doesn't render the
    backslashes as visible characters."""
    adapter = _make_adapter()
    raw = r"Hello\, world\! See https\://example\.com"
    out = adapter.format_message(raw)
    assert out == "Hello, world! See https://example.com"


def test_format_message_preserves_real_markdown():
    adapter = _make_adapter()
    raw = "**bold** _italic_ [link](https://x)"
    assert adapter.format_message(raw) == "**bold** _italic_ [link](https://x)"
```

- [ ] **Step 2: Run tests**: → FAIL

- [ ] **Step 3: Implement** at end of class:

```python
# MarkdownV2 escapes Telegram-tuned prompts emit. BGOS's app renders
# CommonMark — these backslashes survive as visible characters and
# look ugly. Strip them defensively. Real CommonMark escape sequences
# (\\, \*, \_, \[, \], \(, \)) ARE preserved for users who legitimately
# want them.
_MDV2_LEAK_RE = re.compile(r"\\([,.!?:;@\-+=()\[\]<>{}|#])")

def format_message(self, content: str) -> str:
    """Translate the agent's outbound text into BGOS-native form.

    BGOS's mobile app renders CommonMark via the same library Telegram
    Web uses, which means Telegram MarkdownV2 escape sequences like
    `\\,` or `\\!` show up as visible backslashes. Strip the Telegram-
    specific ones; leave real CommonMark escapes alone.
    """
    return _MDV2_LEAK_RE.sub(r"\1", content)
```

Wire it into `send()` and `edit_message()` before `_parse_buttons_block` so cleanup happens once at the boundary:

```python
# In send():
cleaned_text, options, render_mode = _parse_buttons_block(
    self.format_message(content)
)
# In edit_message():
cleaned_text, options, render_mode = _parse_buttons_block(
    self.format_message(content)
)
```

- [ ] **Step 4: Run tests**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter.py
git commit -m "feat: format_message strips Telegram MDv2 escape leakage"
```

### Task 3.2: Long-message splitting with (1/N) continuations

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_bgos_adapter.py`

The current backend message text cap is ~10,000 chars (verify; pick conservative). Past that, BGOS rejects. Telegram's logic is at `telegram.py:1457`.

- [ ] **Step 1: Write failing test**:

```python
async def test_send_splits_long_messages(monkeypatch):
    adapter = _make_adapter()
    adapter._max_message_length = 100  # tiny for test
    posts = []
    async def fake_post(**kw):
        posts.append(kw["text"])
        return {"id": len(posts)}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    big = "x" * 250
    result = await adapter.send(chat_id=1, content=big)
    # 250 / 100 = 3 chunks, each suffixed with (i/N)
    assert len(posts) == 3
    assert posts[0].endswith("(1/3)")
    assert posts[1].endswith("(2/3)")
    assert posts[2].endswith("(3/3)")
    # All-content concatenated equals input (modulo suffixes)
    body = "".join(p.rsplit("\n", 1)[0] for p in posts)
    assert body == big
    # Result message_id reflects the LAST chunk so streaming targets it
    assert result.message_id == "3"
```

- [ ] **Step 2: Run test**: → FAIL

- [ ] **Step 3: Implement** — add a constant and helper:

```python
# Pick conservative — backend's hard cap is in flux; this is the comfortable
# zone for mobile rendering. Configurable for tests.
_DEFAULT_MAX_MESSAGE_LENGTH = 10_000
```

In `__init__`:
```python
self._max_message_length: int = _DEFAULT_MAX_MESSAGE_LENGTH
```

Replace `send()` body with a chunk-aware loop:

```python
async def send(
    self, chat_id, content, reply_to=None, metadata=None,
) -> SendResult:
    formatted = self.format_message(content)
    cleaned_text, options, render_mode = _parse_buttons_block(formatted)
    chunks = self._chunk_text(cleaned_text)
    last_result: SendResult | None = None
    for i, chunk in enumerate(chunks, start=1):
        suffix = f"\n({i}/{len(chunks)})" if len(chunks) > 1 else ""
        # Buttons + reply_to apply ONLY to the first chunk; remaining
        # chunks are pure continuation text (mirrors telegram.py).
        is_first = (i == 1)
        resp = await self._api.post_message(
            chat_id=int(chat_id),
            text=chunk + suffix,
            sender="assistant",
            message_type="standard",
            options=(options or None) if is_first else None,
            render_mode=render_mode if is_first else None,
            reply_to_id=(int(reply_to) if reply_to is not None and is_first else None),
        )
        message_id = resp.get("id") if isinstance(resp, dict) else None
        if isinstance(chat_id, int) and isinstance(message_id, int):
            self._state.last_assistant_message_by_chat[chat_id] = message_id
        last_result = _send_result(message_id=message_id)
    return last_result or _send_result(message_id=None)


def _chunk_text(self, text: str) -> list[str]:
    if len(text) <= self._max_message_length:
        return [text]
    chunks: list[str] = []
    remaining = text
    cap = self._max_message_length - 8  # reserve for "\n(99/99)" suffix
    while len(remaining) > cap:
        # Try to break on a newline near the cap
        split_at = remaining.rfind("\n", 0, cap)
        if split_at < cap // 2:
            split_at = cap
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
```

- [ ] **Step 4: Run test**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter.py
git commit -m "feat: split long messages with (i/N) continuations"
```

### Task 3.3: `send_multiple_images()` as single multi-file POST

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_outbound_media.py`

- [ ] **Step 1: Write failing test**:

```python
async def test_send_multiple_images_posts_single_message(monkeypatch):
    """Telegram batches up to 10 images into a single sendMediaGroup call
    that renders as a carousel. BGOS's equivalent: one POST /messages
    with a files[] array of size N."""
    from hermes_channel_bgos.bgos_adapter import BGOSAdapter
    adapter = _make_adapter()
    posts = []
    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    # Three tiny images
    images = [
        (b"\x89PNG...img1", "a.png", "image/png"),
        (b"\x89PNG...img2", "b.png", "image/png"),
        (b"\x89PNG...img3", "c.png", "image/png"),
    ]
    await adapter.send_multiple_images(
        chat_id=42, images=images, caption="three pics",
    )
    assert len(posts) == 1
    assert len(posts[0]["files"]) == 3
    assert posts[0]["text"] == "three pics"
```

- [ ] **Step 2: Run test**: → FAIL

- [ ] **Step 3: Implement**:

```python
async def send_multiple_images(
    self,
    chat_id: int | str,
    images: list[tuple[bytes, str, str]],
    *,
    caption: str | None = None,
    reply_to: int | None = None,
) -> SendResult:
    """Send up to N images as a single message with a files[] array.

    `images` is a list of `(bytes, filename, mime)` tuples. The backend
    renders 2+ images as a carousel. Backend caps the array at 10 (same
    as Telegram's sendMediaGroup).
    """
    attachments = []
    for blob, filename, mime in images[:10]:
        attachments.append(await self._upload_and_attach(
            file_bytes=blob, filename=filename, mime=mime,
        ))
    resp = await self._api.post_message(
        chat_id=int(chat_id),
        text=caption or "",
        sender="assistant",
        message_type="standard",
        files=attachments,
        reply_to_id=int(reply_to) if reply_to is not None else None,
    )
    message_id = resp.get("id") if isinstance(resp, dict) else None
    return _send_result(message_id=message_id)
```

- [ ] **Step 4: Run test**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_outbound_media.py
git commit -m "feat: send_multiple_images bundles into single multi-file POST"
```

### Task 3.4: Code review

- [ ] Dispatch code-reviewer subagent against `git diff <chunk-2-end>...HEAD`.

---

## Chunk 4 — Inbound Richness

After this chunk: rapid successive inbound user messages get batched into a single agent dispatch with a 200ms–2s adaptive flush window.

### Task 4.1: Adaptive text batching for rapid inbound user messages

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_bgos_adapter_inbound.py`

- [ ] **Step 1: Write failing test**:

```python
async def test_rapid_text_messages_are_batched(monkeypatch):
    """Mobile clients sometimes split a long voice-memo transcription
    into multiple <4KB chunks. The adapter aggregates them with the same
    adaptive flush windows Telegram uses: short flush for short text,
    longer for big paragraphs (telegram.py:3803)."""
    adapter = _make_adapter()
    adapter._state.set_route(7, "default")
    adapter._text_batch_window = 0.05  # speed up tests
    received: list[str] = []
    async def capture(event):
        received.append(event.text)
    adapter.handle_message = capture

    # Send three rapid messages from same user/chat
    for piece in ("part one. ", "part two. ", "part three."):
        await adapter._handle_inbound({
            "assistant_id": 7,
            "chat_id": 42,
            "message_id": int(time.time() * 1000) % 1_000_000,
            "user_id": "u",
            "text": piece,
        })
    # Wait past the flush window
    await asyncio.sleep(0.2)
    # Single batched dispatch
    assert len(received) == 1
    assert received[0] == "part one. part two. part three."
```

- [ ] **Step 2: Run test**: → FAIL

- [ ] **Step 3: Implement** — defer dispatch via a per-chat task. Add to `__init__`:

```python
self._text_batch_window: float = 0.6  # default flush window (seconds)
self._pending_text_batches: dict[int, dict] = {}
self._pending_text_tasks: dict[int, asyncio.Task] = {}
```

Refactor `_handle_inbound` — wrap the dispatch path so standard text messages enter the batch buffer instead of going direct:

```python
# After the assistant_id/route resolution and bridge-local check,
# add a branch BEFORE the gateway-event wrapping:
if (
    event.message_type == "standard"
    and event.text
    and not event.files
):
    self._enqueue_text_batch(event, agent_visible_text, gateway_route=route)
    return
```

Then add:

```python
def _enqueue_text_batch(self, event, text: str, *, gateway_route: str) -> None:
    """Buffer rapid text messages for adaptive-window dispatch.

    Window size adapts to the latest chunk's length — short chunks get
    a tighter flush (less wait for the next typed line); long ones
    suggest a paste / transcribed paragraph and get a longer wait."""
    chat_key = event.chat_id
    batch = self._pending_text_batches.get(chat_key)
    if batch is None:
        batch = {
            "event": event,
            "texts": [text],
            "route": gateway_route,
        }
        self._pending_text_batches[chat_key] = batch
    else:
        batch["texts"].append(text)
        batch["event"] = event  # latest message_id wins
    # Reschedule: cancel any pending flush, start a fresh one with
    # the adapted window.
    existing = self._pending_text_tasks.get(chat_key)
    if existing and not existing.done():
        existing.cancel()
    last = batch["texts"][-1]
    if len(last) <= 320:
        window = min(self._text_batch_window, 0.24)
    elif len(last) <= 1024:
        window = min(self._text_batch_window, 0.4)
    else:
        window = self._text_batch_window
    self._pending_text_tasks[chat_key] = asyncio.create_task(
        self._flush_text_batch(chat_key, window)
    )


async def _flush_text_batch(self, chat_key: int, window: float) -> None:
    try:
        await asyncio.sleep(window)
    except asyncio.CancelledError:
        return
    batch = self._pending_text_batches.pop(chat_key, None)
    if batch is None:
        return
    self._pending_text_tasks.pop(chat_key, None)
    event = batch["event"]
    merged = "".join(batch["texts"])
    # Re-dispatch using the original handle path, with merged text
    event.text = merged
    self._save_last_id(event.message_id)
    self._state.last_user_text_by_chat[event.chat_id] = merged
    # Use the same gateway-event wrap as _handle_inbound
    if _GatewayMessageEvent is not None and _GatewayMessageType is not None:
        try:
            source = self.build_source(
                chat_id=str(event.chat_id),
                user_id=str(event.user_id) if event.user_id else None,
            )
        except AttributeError:
            await self.handle_message(event)
            return
        gateway_event = _GatewayMessageEvent(
            text=merged,
            message_type=_GatewayMessageType.TEXT,
            source=source,
            message_id=str(event.message_id),
            raw_message=event,
            internal=(event.user_id == "" or event.user_id is None),
        )
        await self.handle_message(gateway_event)
    else:
        await self.handle_message(event)
```

Also extend `disconnect()` to cancel pending text-batch tasks (same pattern as Task 1.7).

- [ ] **Step 4: Run test**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter_inbound.py
git commit -m "feat: adaptive text batching for rapid inbound messages"
```

### Task 4.2: Code review

- [ ] Dispatch code-reviewer subagent.

---

## Chunk 5 — Advanced UI (Stretch)

After this chunk: agent's `/model` command renders a paginated picker; the gateway's update flow renders a yes/no prompt.

### Task 5.1: `send_update_prompt()` — yes/no inline

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py`
- Test: `tests/test_approval_handler.py`

- [ ] **Step 1: Write failing test**:

```python
async def test_send_update_prompt_renders_yes_no(monkeypatch):
    adapter = _make_adapter()
    posts = []
    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}
    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send_update_prompt(
        chat_id=1, prompt="Restore stashed config?", default_hint="no",
    )
    options = posts[0]["options"]
    assert {o["callbackData"] for o in options} == {"update_prompt:y", "update_prompt:n"}
```

- [ ] **Step 2: Run test**: → FAIL

- [ ] **Step 3: Implement**:

```python
async def send_update_prompt(
    self,
    chat_id: int | str,
    prompt: str,
    default_hint: str | None = None,
) -> SendResult:
    """Yes/No inline prompt for the gateway's update flow (stash
    restore, config migration). Mirrors telegram.py:2006."""
    text = f"⚕ **Update needs your input:**\n\n{prompt}"
    if default_hint:
        text += f"\n\n_default: {default_hint}_"
    options = [
        {"text": "✓ Yes", "callbackData": "update_prompt:y",
         "style": "success", "row_index": 0},
        {"text": "✗ No",  "callbackData": "update_prompt:n",
         "style": "default", "row_index": 0},
    ]
    resp = await self._api.post_message(
        chat_id=int(chat_id),
        text=text,
        sender="assistant",
        message_type="standard",
        options=options,
    )
    message_id = resp.get("id") if isinstance(resp, dict) else None
    return _send_result(message_id=message_id)
```

- [ ] **Step 4: Run test**: → PASS

- [ ] **Step 5: Commit**:
```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_approval_handler.py
git commit -m "feat: send_update_prompt yes/no UI"
```

### Task 5.2: Code review

- [ ] Dispatch code-reviewer.

---

## Final wrap

- [ ] **Run full test suite**: `pytest -v` → expect 95 + new tests, all passing.
- [ ] **Update README**: refresh capability bullets, list shipped features.
- [ ] **Final commit**: version bump if not already done, summary docs.
- [ ] **Push branch**: prepare for user to pull on their Hermes server.
