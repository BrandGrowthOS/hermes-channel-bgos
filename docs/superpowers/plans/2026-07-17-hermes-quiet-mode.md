# Hermes Quiet Mode Implementation Plan

> **For agentic workers:** This plan is executed as small codex work packets, each spec'd from a task below, implemented by GPT 5.6 via the codex CLI, and adversarially reviewed by the orchestrator. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hermes agent chats read like texting a person: tool and function-call JSON never renders as chat text; robot talk folds into the existing tool_progress card; maintenance prompts become quiet event cards with buttons; failures become a friendly agent_error card with a details toggle; quiet is ON by default with a per-agent off switch.

**Architecture:** All classification happens adapter-side (hermes-channel-bgos, Python) in a new pure module `quiet_mode.py`, wired into the existing `send()` / `edit_message()` paths and the three prompt senders. The BGOS app gains exactly two render capabilities that already have backend support: an `agent_error` card branch and option chips on event cards. Zero backend changes: `event`, `eventMeta`, `options`, and `agent_error` are already accepted by `CreateMessageDto`.

**Tech Stack:** Python 3.12 + pytest/pytest-asyncio (adapter), React Native + zod (app frontend).

## Global constraints

- Quiet ("tidy") is the DEFAULT. "everything" mode reproduces v0.18.1 behavior byte-for-byte (tool card interception stays; no stripping; no new card routing).
- NO em dashes and NO en dashes in any new code, comment, or copy (regex `[–—]` must find nothing in the diff).
- No version bumps, no deploys, no publishes, no merges to main.
- Classifier bias: when in doubt, classify as prose (show it). Suppression must never lose content: everything suppressed lands in a card row or the fail-safe promotion.
- Stay off files owned by sibling branches (add-agent wizard UI + installer; copy sweep). Adapter files are safe; in the app touch ONLY `MessageBubble.tsx`, `EventCard.tsx`, and the new `AgentErrorCard.tsx`.
- The existing 375-test adapter suite must stay green (`python -m pytest -q` with `.venv`, Python 3.12).

## Evidence anchors (verified 2026-07-17)

- Fallback leak: `bgos_adapter.py:1937-1976` (edit path `_do_patch` fallback), `1721-1729` + `1771-1815` (send path posts unclassified text as `standard`).
- Card pipeline: `_handle_tool_progress_edit` `bgos_adapter.py:2027` (REPLACES tools each call), `_finalize_tool_progress_card` `:2134`, summary builder `:739`.
- Preview lifecycle: preview send -> edits -> `delete_message` -> fresh final `send()` (`bgos_adapter.py:2199-2204`). No content-based final/preview distinction exists.
- `send_update_prompt` `:2970` posts `standard` + Yes/No chips; `send_slash_confirm` `:2920` posts `messageType="slash_confirm"` which the backend REJECTS (`slash_confirm` is not in `MessageType` enum; `CreateMessageDto.messageType` is `@IsEnum` at `backend/src/dto/create-chat-history.dto.ts:139-142`), so the documented "degrades to text+chips" is actually a 400 today.
- `/status` jargon: `bgos_adapter.py:3421-3434`.
- Voice prefixes: built at `voice_rpc.py:467-510`, injected as inbound turn text; agent replies may echo them.
- Backend accepts with no changes: `MessageType.AGENT_ERROR` + `MessageType.EVENT` in enum; `eventMeta` (`EventMetaDto`: source<=64, title<=300, peek<=300, payload free-form) and `options` declared on `CreateMessageDto`. `eventMeta` is create-only (not in `UpdateMessageDto`).
- Frontend router: `MessageBubble.tsx:2945-3025`; `agent_error` declared in `MessageTypeSchema` but has no branch (falls through to plain bubble). `EventCard.tsx` has NO options support. Mis-shaped payloads degrade to plain text bubbles.
- Gateway error convention: leading `❌` lines (`gateway/run.py:13782,13950,15029,15118,15220`), `⏳ Working` heartbeat `gateway/run.py:20116`.

## Design decisions (locked)

1. **Per-agent off switch lives adapter-side.** There is no plugin-readable per-assistant field in the backend today (verified), and adding one is new backend surface = scope creep. Switch = bridge-local `/quiet` command per agent chat, persisted at `$HERMES_HOME/bgos_chat_style.json` keyed by assistant id. Env `BGOS_CHAT_STYLE=everything` flips the host-wide default. Priority: per-agent > env > "tidy". The app-profile "Chat style" radio from the design mock is noted as follow-up (bgos-plugin-capability-sync), not built.
2. **"everything" == exactly v0.18.1 behavior** including the emoji tool card interception that shipped in v0.6.0. The proposal's "truly raw" open question is resolved as: no NEW quiet behaviors apply (no stripping, no dump routing, no event-card prompts, technical /status), but shipped behavior is not regressed.
3. **agent_error details ride `eventMeta`** (`{source:"agent", title, payload:{details}}`) so the backend needs zero changes; the new frontend card reads `text` as the friendly sentence and `eventMeta.payload.details` behind the "Show details" toggle.
4. **Update prompt + slash confirm become `messageType="event"`** with `eventMeta` + the SAME options/callbacks as today (gateway-side resolution unchanged). This also fixes the slash_confirm 400.
5. **Suppressed dump edits APPEND rows** to the live tool card (never replace: the emoji grammar rows are an accumulated authoritative list, dumps are incremental narration).
6. **Fail-safe promotion:** every suppression records the raw text per chat and arms/refreshes a timer (default 5.0s, class attr `_quiet_failsafe_seconds`). A posted prose reply clears it. On fire with no reply since: post the recorded text as one `standard` message (capped at 2000 chars with a trailing ellipsis-free truncation marker `[truncated]`), then clear. Cleared on disconnect.

---

### Task A: `quiet_mode.py` pure module (classifier + style store + prefix strip)

**Files:**
- Create: `src/hermes_channel_bgos/quiet_mode.py`
- Test: `tests/test_quiet_mode.py`

**Interfaces (Produces):**
```python
TIDY = "tidy"
EVERYTHING = "everything"

@dataclass(frozen=True)
class RobotTalk:
    kind: str                  # "tool_dump" | "error"
    rows: list[dict]           # tool_progress entries for kind=="tool_dump"
    friendly: str              # short human sentence for kind=="error", else ""
    details: str               # full raw text (both kinds)

def classify_robot_talk(text: str | None) -> RobotTalk | None: ...
    # None => prose (chat-worthy). Conservative: EVERY non-blank line must
    # match a robot shape for tool_dump; error requires the FIRST non-blank
    # line to start with one of the gateway failure emoji.

def strip_voice_prefixes(text: str) -> str: ...
    # removes ONE leading "[voice consult]" or "[voice dispatch]" prefix
    # (plus following whitespace) from the very start only.

def load_default_style() -> str: ...
    # env BGOS_CHAT_STYLE, exact-string "everything" => EVERYTHING, else TIDY.

class ChatStyleStore:
    def __init__(self, path: Path | None = None): ...
        # default path: Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        #               / "bgos_chat_style.json"
    def style_for(self, assistant_id: int | None) -> str: ...
        # per-agent override or load_default_style()
    def set_style(self, assistant_id: int, style: str) -> None: ...
        # persists {"<assistant_id>": "everything"|"tidy"} atomically (tmp+rename);
        # storing the default style REMOVES the key.
```

**Robot shapes (tool_dump), line-wise, after `.strip()`:**
- JSON call object: line starts with `{`, `json.loads` succeeds, and the object contains a `"name"` key or an `"arguments"`/`"parameters"` key. Row: `{"icon": "⚙️", "name": <name or "call">, "args": <compact json <=120 chars>, "status": "done"}`.
- Call syntax: regex `^[A-Za-z_][A-Za-z0-9_.]*\((?:[^()]|\([^()]*\))*\)\s*(?:\(×\d+\))?$`. Row name = identifier, args = inside parens (<=120).
- Bare dedup tail: `^\(×\d+\)$` (contributes no row, but counts as robot line).
- A text with zero rows after matching (only tails) classifies as tool_dump with one row `{"icon": "⚙️", "name": "output", "args": "", "status": "done"}`? NO: return a tool_dump with rows possibly empty; the caller skips card emit for empty rows and only records suppression.

**Error shape:** first non-blank line starts with `❌` or `⚠️` (with or without variation selector). friendly = that line minus the leading emoji/whitespace, truncated to 200 chars; details = full text.

**Prose bias examples that MUST classify as prose (None):** normal sentences; markdown with code fences; a reply that CONTAINS a JSON snippet among prose lines; multilingual text; a line like `Ok (done)`; text starting with an emoji that is a real reply, e.g. `🎉 Done! Your Saturday is planned.` (emoji + prose does not match the call/JSON shapes).

- [ ] **Step A1:** Write `tests/test_quiet_mode.py` covering: JSON dump single/multi-line, call-syntax with `(×3)` tail, mixed prose+JSON => None, fenced code reply => None, error line `❌ Hermes update failed.` => kind error with friendly text, `⚠️`-led error, prefix strip both prefixes + idempotent on non-prefixed, style store default tidy, env everything, per-agent set/persist/reload, atomic file shape.
- [ ] **Step A2:** Run: `python -m pytest tests/test_quiet_mode.py -q` => FAIL (module missing).
- [ ] **Step A3:** Implement `quiet_mode.py` (pure stdlib; no adapter imports).
- [ ] **Step A4:** Run: full suite green.
- [ ] **Step A5:** Commit `feat(quiet): classifier + chat-style store + voice prefix strip`.

### Task B: wire quiet mode into send/edit + fail-safe + agent_error emission

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (send `:1644`, edit_message `:1861`, `_handle_tool_progress_edit` `:2027`, state dicts `~:879`, disconnect cleanup `~:1537`)
- Modify: `src/hermes_channel_bgos/bgos_api.py` (`post_message` gains `event_meta: dict | None = None` -> `body["eventMeta"]`)
- Test: `tests/test_quiet_wiring.py`

**Interfaces (Consumes A):** `classify_robot_talk`, `strip_voice_prefixes`, `ChatStyleStore`, `load_default_style`.

**Behavior (quiet only; everything mode short-circuits to current code):**
- `send()`: existing emoji intercept stays FIRST and unchanged. Then (no options, no media): `classify_robot_talk(cleaned_text)`:
  - tool_dump: append rows to live card via `_handle_tool_progress_edit(chat_key, 0, rows, None, replace=False)` (emit skipped if rows empty), record suppression + arm fail-safe, return the card SendResult. Do NOT finalize the card first (the dump belongs to the running turn).
  - error: post `message_type="agent_error"`, `text=friendly`, `event_meta={"source": "agent", "title": friendly[:300], "payload": {"details": details}}`. Counts as a reply (clears fail-safe). Return its SendResult.
  - prose: `strip_voice_prefixes`; if the stripped text is empty return without posting; else current path; a successful post clears suppression + cancels the fail-safe timer.
- `edit_message()`: after the existing emoji parse returns None: classify. tool_dump/error both become APPENDED rows (error rows use icon `⚠️`, name `error`); return `_send_result(message_id=mid_int)` placeholder WITHOUT calling `_do_patch`; record suppression + arm fail-safe. Prose: strip prefixes, then current throttle/patch path.
- `_handle_tool_progress_edit` gains keyword `replace: bool = True`; `replace=False` extends the tracked list (cap total rows at 50 to respect `ArrayMaxSize(50)`; drop oldest overflow) and skips the preview-mid mapping.
- Fail-safe: per-chat `_quiet_suppressed_text`, `_quiet_failsafe_task` dicts; `_quiet_failsafe_seconds = 5.0` class attr; timer posts the recorded text as `standard` (2000-char cap + `\n[truncated]`), clears state; cancelled/cleared on prose reply and on `disconnect()`.
- Style resolution: `self._chat_style_store.style_for(self._state.assistant_id_by_chat.get(chat_key))`, computed once per call.

- [ ] **Step B1:** Write `tests/test_quiet_wiring.py` (mock server fixtures, mirroring `test_tool_progress.py` style):
  - `test_function_call_json_edit_never_patches_chat_text` (DoD test): edit with `{"name":"calendar_read","arguments":{...}}` => NO PATCH of message text with that content; card created/patched with a `calendar_read` row.
  - `test_function_call_json_send_never_posts_standard`: same via send(); no `messageType=="standard"` POST containing the JSON.
  - `test_dump_rows_append_not_replace`: emoji line first, then dump edit => card tools contain both.
  - `test_prose_send_passes_through_and_clears_failsafe`.
  - `test_failsafe_promotes_suppressed_text` (set `_quiet_failsafe_seconds=0.05`).
  - `test_everything_mode_passes_dump_as_chat` (off switch honored end-to-end).
  - `test_voice_prefix_stripped_in_quiet` / `test_voice_prefix_kept_in_everything`.
  - `test_error_send_posts_agent_error_with_event_meta`.
- [ ] **Step B2:** Run new file => FAIL. **Step B3:** implement. **Step B4:** full suite green. **Step B5:** commit `feat(quiet): route robot talk into cards, agent_error, fail-safe promotion`.

### Task C: quiet surfaces for update prompt, slash confirm, /status + `/quiet` toggle

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (`send_update_prompt` `:2970`, `send_slash_confirm` `:2920`, `_handle_bridge_local` `:3381`)
- Modify: `src/hermes_channel_bgos/commands_sync.py` (`BRIDGE_LOCAL_COMMANDS` + manifest descriptions)
- Test: `tests/test_quiet_surfaces.py`

**Behavior (quiet):**
- `send_update_prompt`: `message_type="event"`, `event_meta={"source": "update", "title": "Small update ready", "peek": <first line of prompt, <=300>}`, text = current full body, options unchanged.
- `send_slash_confirm`: `message_type="event"`, `event_meta={"source": "confirm", "title": <title or "Please confirm">, "peek": <first line of message>}`, options + `_slash_confirm_state` unchanged.
- `/status`: friendly text (no pairing id, no message ids) + `message_type="event"`, `event_meta={"source": "status", "title": "Connected and healthy", "peek": "<N> agent(s) online", "payload": {technical fields}}`.
- `/quiet` bridge-local: no args => report current style in plain words; `off` => everything; `on` => tidy; persists via `ChatStyleStore.set_style`; unknown arg => short usage line. Add to `BRIDGE_LOCAL_COMMANDS` and the synced manifest (description <=100 chars, plain words, no jargon).
- Everything mode: all four keep exactly today's output (slash_confirm keeps `slash_confirm` type; document the known 400 in the docstring).

- [ ] **Step C1:** tests first (each surface x quiet/everything, /quiet persistence + manifest content). **Step C2:** FAIL run. **Step C3:** implement. **Step C4:** suite green. **Step C5:** commit `feat(quiet): event-card prompts, friendly /status, /quiet toggle`.

### Task D: BGOS app: agent_error card + event-card option chips

**Files:**
- Create: `frontend/expo-app/src/components/chat/AgentErrorCard.tsx`
- Modify: `frontend/expo-app/src/components/chat/MessageBubble.tsx` (guard + branch before fallthrough)
- Modify: `frontend/expo-app/src/components/chat/EventCard.tsx` (options chips)

**Behavior:**
- `isAgentErrorMessage`: `messageType === "agent_error"`. Branch renders `AgentErrorCard`: soft red styling (reuse the `cardError` idiom from `ToolProgressBubble.tsx:409-413` and EventCard's layout), title row `⚠️ + message text first line`, optional dim second line (`eventMeta.peek`), ghost button `Show details` / `Hide details` toggling a monospace pane with `eventMeta.payload.details` (string) or pretty JSON of payload. No details payload => no button.
- `EventCard`: when the message carries `options`, render the same chip row the standard bubble uses (reuse the existing inline-options component + tap wiring; chips disappear after tap exactly like standard messages). Keep collapsed-by-default; chips visible even when collapsed.
- Copy rules: human words, no jargon, no em/en dashes.
- Verify: `npx tsc --noEmit` = 0 in `frontend/expo-app`.

- [ ] **Step D1:** codex studies existing option-chip wiring, implements, self-checks tsc. **Step D2:** orchestrator adversarial review. **Step D3:** commit `feat(chat): agent_error card + option chips on event cards`.

### Task E: docs + capability canon

**Files:**
- Modify: `docs/bgos-agent-capabilities.md` (Hermes cheat-sheet section + §12 note that Hermes posts outbound assistant-sender events for maintenance prompts + new agent_error emission note)
- Modify: `README.md` (Quiet mode section: default, `/quiet`, `BGOS_CHAT_STYLE`, fail-safe)

**Content requirements:** document the tidy default, the strict classifier, the fail-safe promotion, `/quiet on|off`, `BGOS_CHAT_STYLE`, agent_error wire shape (text + eventMeta.payload.details), event-card prompts, and the slash_confirm fix. Note (do NOT perform) propagation to openclaw/gobot/claude/codex plugins per bgos-plugin-capability-sync.

- [ ] **Step E1:** codex drafts; orchestrator reviews for accuracy against shipped code. **Step E2:** commit `docs(quiet): capabilities canon + README`.

## Verification (Definition of Done)

- [ ] Full adapter suite green, output pasted in report (`python -m pytest -q`).
- [ ] New tests exist and pass: function-call suppression as chat + status card surfacing, allowlist (prose passes), off switch (everything mode), fail-safe promotion.
- [ ] `npx tsc --noEmit` exits 0 in `frontend/expo-app` (app worktree touched).
- [ ] `grep -RInE '[–—]'` over the diff of both repos finds nothing in added lines.
- [ ] Full diff adversarially reviewed; fix-loop log in report.
- [ ] Branch `feat/hermes-quiet-mode` pushed in both repos; PRs opened, not merged; no version bumps.
