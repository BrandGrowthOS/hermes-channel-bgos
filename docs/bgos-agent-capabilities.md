# BGOS Agent-Facing Capabilities — Canonical

**This file is the single source of truth** for what BGOS's frontend/backend expose to an agent connected through any channel plugin. Every plugin (Claude Code MCP, Hermes, OpenClaw, future) keeps its own agent-facing docs in sync with this one.

**Update workflow:** When a BGOS frontend feature changes (new message_type, new field on an existing DTO, new UI affordance, new limit), edit THIS file first. Then sync every plugin's agent-facing docs to match. See the "Propagation to plugins" section at the bottom for the exact checklist.

**Skill that enforces this:** `bgos-plugin-capability-sync` — activates when a user modifies frontend message-handling, adds a new `MessageType`, or changes a DTO field. Reminds the developer to bump the canonical here + propagate to all plugin docs.

---

## Capabilities

### 1. Message formatting

Agent replies render as markdown via `react-native-markdown-display` in `frontend/expo-app/src/components/MessageMarkdown.tsx`.

**Supported:** `**bold**`, `*italic*`, `` `inline code` ``, ` ```fenced code``` `, `[links](url)`, `#`/`##`/`###` headers, numbered + bulleted lists, `>` blockquotes.

**Not yet rendered natively:**
- Tables (markdown tables don't layout on mobile — not in the rules allowlist)
- Inline images via `![alt](url)` — use the file-attachment system instead
- Strikethrough, spoilers (Telegram-only syntax)

**Guidance for agents:** keep replies reasonably concise — users often on a phone. Don't rely on tables. For images, use the file path / URL mechanism appropriate to the plugin.

### 2. Inline option buttons (non-approval, async)

When an agent wants to offer the user 2–6 tappable choices without blocking, it sends a normal `message_type='standard'` message with an `options[]` array.

**Wire (from `backend/src/dto/create-chat-history.dto.ts` `CreateMessageOptionDto`):**
```ts
options: [
  { text: "Visible label", callbackData: "stable-id-sent-back-on-tap" }
]
```

Backend enforces **≤6 options** when `renderMode='inline'`. Rendered as a card below the message bubble with tappable chips (`frontend/expo-app/src/components/chat/InlineOptionsCard.tsx`).

**On tap:** the user's click fires a `button_clicked` event (and a `callback_result` fanned out to plugin sockets). Plugin receives `callback_data` = the option's `value` / `callbackData`, `message_id` = the prompt message, and correlates.

**Sentinels (reserved callback_data values the UI generates automatically):**
- `__skip__` — user tapped the built-in "Skip" affordance.
- `__custom__` — user tapped "Custom reply" and typed free text. The free text ALSO arrives as a normal user message; correlate by `message_id`.

**`style` field** — `"default" | "success" | "danger" | "primary"` — styles the chip color. Dropped by the backend whitelist today until Phase-F schema extension ships, but plugins should send it anyway for forward compatibility.

**Use for:** scheduled nudges, cron check-ins, proactive suggestions, any async flow where the user isn't actively waiting. The chips stay clickable indefinitely.

**Do NOT use for:** blocking questions where you need the answer to continue. Use `ask_user_input` instead.

### 3. `ask_user_input` — blocking multi-question modal

When the agent needs the user's answer to continue and they're actively in the chat, `ask_user_input` pops a polished sheet/modal (`frontend/expo-app/src/components/chat/AskUserInputSheet.tsx`).

**Wire (fields on `CreateMessageDto`):**
```ts
{
  messageType: "ask_user_input",
  askId: "<uuid>",           // Groups N questions into one carousel — omit on first, reuse for follow-ups
  askOrder: 1,               // 1-based position within the ask group (1-4 questions per carousel)
  allowFreeText: true,       // Default: true — adds "Custom reply" affordance
  allowSkip: true,           // Default: true — adds "Skip" affordance
  options: [
    { text: "Option A", callbackData: "a" },
    { text: "Option B", callbackData: "b" }
  ]
  // renderMode: "modal" is the default for ask_user_input
}
```

**Behavior:** carousel with `‹` `›` arrows when >1 question, Skip button, Custom-reply free-text fallback. Tool **blocks** until the user answers every question (option picked, free text submitted, or skipped). Returns structured answers as `button_clicked` / `ask_response` events.

**Use for:** choosing an approach, picking a destination, confirming intent before destructive action, multi-step wizards, surveys, onboarding.

**Do NOT use for:** open-ended questions (use a regular reply), pure yes/no on dangerous commands (use the approval system), or ANY question where the user isn't actively waiting (modals demand attention — use inline buttons for async).

**Limit:** 1–4 questions per carousel. Longer flows feel like an interrogation.

### 4. Dangerous-command approvals (4-button Telegram parity)

When an agent's tool invocation requires approval (per `approvals.mode` on the Hermes/gateway side), a message with `message_type='approval_request'` renders as a bubble with 4 styled buttons: **Allow once**, **Allow for session**, **Always allow**, **Deny**.

**Wire:**
```ts
{
  message_type: "approval_request",
  text: "<human-readable description>",
  approvalMeta: { command, session_key, approval_id, metadata },
  options: [
    { text: "Allow once",        callbackData: "ea:once:<id>",    style: "success" },
    { text: "Allow for session", callbackData: "ea:session:<id>", style: "success" },
    { text: "Always allow",      callbackData: "ea:always:<id>",  style: "default" },
    { text: "Deny",              callbackData: "ea:deny:<id>",    style: "danger"  }
  ]
}
```

Callback format matches Telegram's `ea:{choice}:{approval_id}` exactly. The adapter resolves verdicts synchronously via `resolve_gateway_approval(session_key, choice)`.

**Default timeout:** 60s fail-closed (configurable via `approvals.timeout_seconds`). Stale buttons no-op when tapped (adapter logs "already resolved").

**`approvalMeta` is stored as JSONB** (structural contents are agent/plugin-defined). Currently backend strips it via whitelist until Phase-F schema extension; plugins should send it for forward-compat.

### 5. Outbound files & media (agent → user)

BGOS renders five media kinds natively:

| Kind | MIME examples | Cap | UI |
|---|---|---|---|
| Image | JPEG, PNG, GIF, WebP, SVG, BMP, TIFF | 10 MB | Tappable thumbnail; tap opens fullscreen viewer |
| Video | MP4, WebM, MOV, AVI, MKV | 100 MB | Plays inline |
| Audio / voice | OGG, MP3, M4A | 25 MB | Voice bubble with scrubber |
| Document | PDF, TXT, CSV, DOC/DOCX, XLS/XLSX, PPT/PPTX, JSON, ZIP | 25 MB | Download card with filename + type emoji (📄, 🎵, 🎬) |

**Backend wire (`files[]` on `POST /messages`):**
```ts
files: [
  { fileName, fileMimeType, fileData?, s3Key?, size }
]
```

**Policy:** `fileData` is base64 when the file is < 500 KB; `s3Key` references an S3 object (uploaded via presigned PUT from `POST /integrations/files/upload-url`) otherwise. Each plugin can use whichever makes sense.

**One message can contain text + multiple files** — the bubble renders all in sequence.

### 6. Inbound files & media (user → agent)

The backend's `inbound_message` WS payload (sent to `assistant:<id>` rooms) carries a `files` array shaped:

```ts
files: [
  {
    id: number,
    filename: string,
    mime: string,
    url?: string,      // presigned S3 GET, ~1h TTL — present for files >= 500KB
    dataUri?: string,  // "data:<mime>;base64,..." — present for inline files <500KB
  }
]
```

How that surfaces to the agent depends on the plugin's internal model:

- **MCP / OpenClaw plugins** — the agent receives the `files[]` directly via the protocol's structured fields and can iterate them.
- **Hermes** — Hermes's `MessageEvent` has no first-class `files` slot, so the adapter inlines attachments into the message `text`: images become markdown image syntax (`![filename](url)`) so vision models pick them up automatically; other files become labeled link lines (`- [filename](url) (mime)`) under an `## Attachments from user` heading. Vision-capable models fetch the URL automatically; non-vision models can choose to fetch via HTTP GET.

URLs are presigned for ~1 hour. Inline `data:` URIs work indefinitely (the bytes are right there). Backend ALWAYS includes one of `url` or `dataUri` for every file — older backend versions emitted only `{id, filename, mime}` with no fetch path; treat that as an out-of-date deploy.

### 7. Slash commands

Two kinds:

**Bridge-local** (handled by the adapter, not forwarded to the agent):
- `/new` — reset this chat's conversation binding; next message starts fresh
- `/retry` — re-send the user's last message through the agent
- `/status` — show adapter health

Adapter intercepts these before `handle_message` is called. Agent never sees them.

**Native** (agent's own slash commands):
- Forwarded as a regular user message starting with `/`.
- Message arrives with `message_type='slash_command'` + `command_name` + `command_args` fields parsed out.

**Agents should:** declare their native slash-command catalog at plugin connect time via `PUT /integrations/assistants/:id/commands` (shape: `[{command, description, scope: "all"}, ...]`). The BGOS frontend's slash picker reads this and auto-suggests.

### 8. `tool_progress` — collapsible tool-call card

When an agent runs one or more tools during a turn, BGOS can render the activity as a dedicated tinted card that visually separates it from the agent's actual reply. Three states:

- **Running** (pulsing yellow dot, auto-expanded) — at least one tool is still executing for this turn. Card lists the tools as they land.
- **Done, collapsed** (hollow yellow ring, one-line summary) — all tools finished. Card auto-collapses to "Used N tools · …".
- **Done, expanded** — user tapped the row to re-open the full transcript.

**Wire (extends `CreateMessageDto` and `UpdateMessageDto`):**
```ts
{
  messageType: "tool_progress",
  text: "Working… · read_file, terminal",   // short summary (legacy-client fallback)
  toolProgress: {
    state: "running" | "done",
    tools: [
      {
        icon: "📖",           // emoji prefix (gateway/plugin chooses)
        name: "read_file",    // canonical tool name
        args: "/etc/hostname", // short args summary (≤120 chars)
        status: "running" | "done" | "error"
      }
    ]
  }
}
```

`options.tools` is append-only over the message's lifetime — each PATCH adds new entries or transitions `state`. POST creates the card; PATCH (with `toolProgress` field set) appends tools or flips `state="done"`.

Channel-agnostic by design — Hermes streams via PATCH edits as the gateway probes tool calls; OpenClaw/Gobot/Claude Code can POST a single done-state card per turn at end of turn. See spec: `docs/superpowers/specs/2026-05-15-tool-progress-message-type-design.md`.

**Use for:** any plugin emitting one or more tool calls during a turn where the user benefits from seeing what the agent did without it competing visually with the agent's actual reply.

**Do NOT use for:** the agent's text reply itself (use `standard`), buttons (use `standard` + `options[]`), approvals (use `approval_request`).

### 9. Reply-quote (Telegram-style quoted replies)

When the agent wants to anchor a message to a specific earlier one — answering a stale question, following up on a past commitment, surfacing a proactive nudge tied to an old thread — it sets `replyToId` to the source message id. BGOS stores a frozen plain-text snapshot of the source's text + sender alongside the new message and renders a tappable quoted header inside the receiving bubble. Tapping the header scrolls to the source and briefly flashes it.

**Wire (extends `CreateMessageDto` / `MessageWrapperDto`):**
```ts
{
  // ...all existing fields
  replyToId: number   // optional; source message id (same chat)
}
```

**Server behavior:**
- Same-chat constraint enforced — 400 if `replyToId` references a message in a different chat.
- Snapshot computed server-side at write time: plain-text-stripped source text (≤280 chars) + source sender. Frozen forever — source edits/deletes do not change the snapshot.
- `ON DELETE SET NULL` on the FK — deleting the source nullifies `replyToId` but the snapshot stays so the preview keeps rendering. Tap is a no-op with toast "Original message deleted".
- Snapshot for media-only sources: `"🎤 Voice message"` (audio) / `"📎 <filename>"` (file).

**When to use (agents):**
- ✅ Answering a question from N messages ago when the user would otherwise have to scroll up to figure out the context.
- ✅ Following up on your own past commitment ("you said you'd watch X — it just happened").
- ✅ Proactive notification triggered by cron / external webhook tied to an earlier specific message.
- ✅ Correcting or amending a specific earlier statement of yours.

**When NOT to use:**
- ❌ The reply addresses the immediately preceding user message — the quote is noise, alignment already implies it.
- ❌ The chat is fresh (≤2 turns) and there's no ambiguity.
- ❌ Pure acknowledgements ("Got it" / "On it") — nothing to anchor to.

Per-plugin wire syntax is in the cheat-sheet below. Spec: `BGOS/docs/superpowers/specs/2026-05-19-reply-quote-design.md`.

### 10. Conversation / chat model

- **DM-only** — no group chats, no forum topics, no threads.
- One BGOS chat maps to one agent conversation.
- The user can reset context via `/new` (bridge-local).
- Chat title is user-editable; agents can rename via `PATCH /chats/:id/title` (plugin-specific).
- **Not supported:** user-side message editing, reactions, stickers, typing indicators on the user side. (Quoted-reply threading is supported — see §9.)

**Chat addressing — server-authoritative (2026-05-30 hardening):**

- The **server** resolves the target chat. An agent NEVER invents, guesses, or increments a chat id.
- Address a reply ONLY via **(a)** the reply-context of the inbound event you are answering, or **(b)** the opaque **`sessionHandle`** carried on every inbound event / new-session notification. The handle is HMAC-signed and verified + decoded server-side; it is meaningless to any other agent and is non-enumerable. Send it back in the `sessionHandle` field instead of a raw `chatId`.
- Raw numeric `chatId` is **DEPRECATED**. During the rollout window (`ALLOW_RAW_CHATID=true`) it is still accepted but logged; once plugins ship handle addressing it will be rejected. **Plugins MUST NOT let an agent send to a `chatId`/`assistantId` it didn't receive in a prior inbound event or server-issued handle** — validate agent-supplied ids against the set the adapter actually received before dispatch.
- The server stamps the sender's user/assistant identity from the authenticated credential. A pairing/agent principal **cannot** persist `sender='user'` (no puppeting the human) and **cannot** claim a `fromAgent` identity that isn't its own registered `agent_peer` or its own assistant — cross-tenant and cross-assistant writes fail-closed at the server.

**Introductions — agent-may-request, user-approves:**

- An agent CANNOT open a peer (agent-to-agent) conversation with an un-introduced sibling. `send_to_peer`/`/peers/:id/send` to a peer with `introduced:false` returns `requires_introduction` — the server gates it; no plugin may bypass this.
- An agent MAY raise a PENDING introduction request via `POST /api/v1/peers/introductions/request` (`X-Caller-Assistant-Id` header, body `{ targetAssistantId }`; pairing tokens allowed). It surfaces to the **user** to grant or deny — the agent can never create the allow-edge itself.
- Inline `fromAgent` display-name/avatar renders only when the user has enabled the per-user "allow inline agent identities" toggle (default **off**); otherwise a neutral "Agent" badge is shown. Registered `agent_peer` and own-assistant identities are unaffected.

---

## Per-plugin syntax cheat-sheet

How the agent connected through each plugin actually invokes these capabilities.

### Claude Code MCP plugin (`bgos-claude-plugin`, private repo)

MCP server with typed tools:
- `reply` — text + files + inline buttons in one call. Fields: `chat_id`, `text`, `files[]`, `buttons[]`, `render_mode: "inline" | "modal"`, `reply_to_message_id` (optional, capability §9).
- `ask_user_input` — blocking modal. Fields: `chat_id`, `questions[]`.
- `edit_message`, `rename_chat` — message ops.

**Reply-quote (§9):** set `reply_to_message_id` on `reply` when you're anchoring to a specific older message. The MCP server forwards it as `replyToId` in the POST body; the backend computes + persists the frozen snapshot.

Agent learns this via the MCP server's `instructions` + per-tool `description` fields. Canonical source: `bgos-claude-plugin/server.ts` lines ~244–337 + tool-registration block.

**When you update this canonical, also update:** `bgos-claude-plugin/server.ts` — the MCP `instructions` string and per-tool `inputSchema` descriptions. Bump plugin version. `bun install`, restart Claude Code session with the new plugin.

### Hermes (`hermes-channel-bgos`, public repo)

Hermes agents learn about BGOS via a `PLATFORM_HINTS` entry in `agent/prompt_builder.py` (inserted by the fork patch at `hermes-fork-patch/0001-bgos-integration.patch`).

**Concrete syntax for agents:**
- **Text** — just reply normally, markdown is honored.
- **Files / media** — include `MEDIA:/absolute/path/to/file` lines in the reply. Handled natively by the adapter's `send_image/voice/video/document/animation` overrides.
- **Approvals** — agent doesn't initiate; Hermes's approvals system calls `adapter.send_exec_approval` when a sensitive tool fires. Rendered as the 4-button bubble.
- **Inline buttons (non-approval)** — embed a `[[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]` block in the reply. Lines inside the block are `Label | value` (pipe-separated), one per line, max 6. Adapter extracts the block and posts `options: [{text, callbackData}]` with `renderMode: 'inline'`. When the user taps a chip, the adapter receives `inbound_click` on the `assistant:<id>` WS room and synthesizes a user `MessageEvent` with `text = <clicked button label>` — the agent sees the tap as a normal user reply.
- **Reply-quote (§9)** — embed `[[BGOS_REPLY_TO]]<message_id>[[/BGOS_REPLY_TO]]` anywhere in the reply (single line). Adapter extracts the id, strips the marker from the text before posting, and forwards as `replyToId` on the backend POST. Use when answering a stale question or anchoring a proactive nudge to a specific earlier message.
- **`ask_user_input` modal** — **NOT YET WIRED.** Planned via `[[BGOS_ASK]]...[[/BGOS_ASK]]` marker. Until shipped, use sequential inline-button messages.
- **Slash commands from agent to user** — push via `PUT /integrations/assistants/:id/commands` (adapter's `sync_commands_for` method).
- **Home-channel cron** — set `BGOS_HOME_CHANNEL` env var on the Hermes server. Crons scheduled with `deliver="bgos"` route to that chat id.

**Phase 1 Telegram-parity UX (shipped in `hermes-channel-bgos` v0.5.0, 2026-05-12):**
- **Tool-progress card** (since v0.6.0, 2026-05-15) — the gateway's emoji-prefixed status text (`🔍 search`, `🧠 memory`, `🔧 patch`, `💻 shell`, `📖 read`, `🌐 navigate`, `📋 plan`, `⚡ default`) is intercepted by the adapter's `edit_message` override, parsed via `_parse_tool_progress_text`, and emitted as a dedicated `messageType="tool_progress"` card with structured `toolProgress: {state, tools[]}`. The streaming-preview cleanup at end of turn finalizes the card to `state="done"` so it auto-collapses on the frontend. Older BGOS clients without the renderer fall back to the `text` summary (no breakage). No agent-side syntax needed.
- **Streaming responses** — token-by-token responses stream into a single bubble that edits in place. Throttled to 1 edit per 1.5s per chat.
- **Typing indicator** — ephemeral "typing…" affordance during long tool calls / between progress edits. Emitted over the `typing` Socket.IO event.
- **Intermediate-preview cleanup** — when streaming completes, the gateway deletes the in-progress preview and posts a fresh final message so the user-visible timestamp reflects completion time.
- **In-place approval bubble edits** — clicking an approval button replaces the buttons with `✅ Approved once by <user>` (or `🔒 Approved permanently` / `❌ Denied` etc.). Bypasses the 1.5s edit throttle so the resolution lands immediately.
- **Per-user callback authorization** — same gate as inbound text. Fail-closed; operators set `BGOS_ALLOW_ALL_USERS=true` or `BGOS_ALLOWED_USERS=<csv>`.
- **`send_slash_confirm` 3-button UI** — Approve Once / Always 🔒 / Cancel for slash commands that need explicit ack (current caller: `/reload-mcp`). Callback `sc:<choice>:<confirm_id>`.
- **`send_update_prompt`** — yes/no inline buttons for Hermes's gateway update flow.
- **Long-message splitting** — auto-chunked at ~10K chars with `(i/N)` continuation suffixes; buttons + reply-to attach to chunk 1 only.
- **`format_message` MDv2 escape stripping** — Telegram-tuned prompt-emitted escapes (`\,` `\!` `\.` etc.) cleaned; CommonMark escapes (`\*` `\_` `\[` `\(`) preserved.
- **`send_multiple_images`** — up to 10 images into a single multi-file POST.
- **Adaptive inbound text batching** — rapid plain-text messages coalesce; adaptive flush window (≤0.24s short / ≤0.4s mid / 1.0s for ≥4KB chunks / 0.6s default). Slash commands and file-bearing messages bypass. `last_user_text_by_chat` gets the merged text so `/retry` replays the full input.

All these unlock because the adapter overrides the relevant `BasePlatformAdapter` methods (`edit_message`, `delete_message`, `send_typing`, `send_slash_confirm`, `send_update_prompt`) — the gates Hermes's gateway probes. No fork-patch changes required.

**Backend dependencies still in flight (graceful degradation if missing):**
- `DELETE /api/v1/messages/{id}` — needed for streaming-preview cleanup; without it, preview stays visible (cosmetic).
- WS `typing` event handler — without it, no typing indicator (cosmetic).
- `messageType="slash_confirm"` whitelist — without it, renders as `standard` with chips intact (buttons still work).
- Approval `style` / `row_index` on options — without it, buttons render flat (already documented in v0.4 troubleshooting).

**When you update this canonical, also update:** `hermes-channel-bgos/hermes-fork-patch/` — regenerate `0001-bgos-integration.patch` with the new `PLATFORM_HINTS` entry. Have users `git pull` + re-apply the patch on their fork, then restart Hermes.

### OpenClaw (`openclaw-channel-bgos`, BGOS monorepo)

Standalone daemon with method-based API:
- `BgosOutbound.sendText({assistantId, chatId, text, replyToMessageId?})` — plain.
- `BgosOutbound.sendButtons({assistantId, chatId, text, options, replyToMessageId?})` — inline options.
- `BgosOutbound.sendApprovalRequest({assistantId, chatId, text, meta, options?, replyToMessageId?})` — 4-button.
- `BgosOutbound.sendAgentError({assistantId, chatId, reason, replyToMessageId?})` — styled error bubble.

**Reply-quote (§9):** every send method accepts `replyToMessageId` and forwards it as `replyToId` on the backend POST. Use when anchoring to a specific earlier message — see §9 for when-to-use rules.

Today OpenClaw does NOT expose an agent-facing instruction document — the agent is assumed to know what BGOS supports from its own system prompt. **This canonical doc should be surfaced to OpenClaw agents** (either by injecting a summary into their system prompt at connect time, or by exposing a `bgos-capabilities` text resource the agent can query). Implementation TBD.

**When you update this canonical, also update:** `openclaw-channel-bgos/src/` — add or update whatever capability-description mechanism exists (TBD — see above). Bump `openclaw-channel-bgos/package.json` version. Release on npm or refresh the VPS install.

### Future plugins

Any new plugin (Gobot, ChatGPT, etc.) follows the `channel_integration_pattern.md` blueprint + agrees to:
1. Consume this canonical doc in its agent-facing surface (however is idiomatic for that channel).
2. Subscribe to updates — when a capability here changes, propagate.

---

## Propagation to plugins — checklist

When a BGOS frontend capability changes (new `MessageType`, new DTO field, new UI affordance, changed limit, etc.):

- [ ] **This file first** — update the relevant capability section. Be concrete about wire format + user-visible behavior.
- [ ] **`bgos-claude-plugin`** — update `server.ts` MCP `instructions` string + any affected tool `inputSchema` descriptions. Bump `package.json` version. Push; users `git pull` + re-run with `bun`.
- [ ] **`hermes-channel-bgos`** — update the `PLATFORM_HINTS` BGOS entry in `hermes-fork-patch/0001-bgos-integration.patch` (regenerate patch from a fresh Hermes clone). Update the "Per-plugin syntax cheat-sheet" → Hermes section here. Push; users `git pull` + re-apply patch + restart service.
- [ ] **`openclaw-channel-bgos`** — update the agent-facing capability mechanism (once one exists). Bump `package.json`. Release.
- [ ] **Sanity check** — cross-read the three plugin docs side-by-side: do they describe the same capability the same way? Fix drift before shipping.

**Who owns propagation:** whichever developer/agent shipped the frontend change. Don't merge the frontend PR until the plugin updates are also staged (or at least issues are filed).

---

## Versioning

This doc follows the `hermes-channel-bgos` repo's version. Minor bumps whenever a capability section is added or substantially revised. The file's last-modified date is in git; the content isn't dated inline.
