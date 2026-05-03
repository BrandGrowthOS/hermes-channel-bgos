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

### 8. Conversation / chat model

- **DM-only** — no group chats, no forum topics, no threads.
- One BGOS chat maps to one agent conversation.
- The user can reset context via `/new` (bridge-local).
- Chat title is user-editable; agents can rename via `PATCH /chats/:id/title` (plugin-specific).
- **Not supported:** user-side message editing, stickers, typing indicators on the user side. Reactions ARE supported (see §10). `reply_to` message threading IS supported (used by §11 a2a side-threads to correlate replies — agents may also use it for normal user-facing quoted-reply UX).

### 9. Telegram-style emoji reactions

Tap-and-hold (or right-click) any message to open an emoji tray; tap to add a pill, tap again to remove. Single-reaction-per-actor — switching emojis is atomic (the old pill is replaced, not stacked). Reactions render as pills below the bubble; the user's own reaction renders cyan-tinted.

**Read-side (chat-history payload):** every `message` row carries an aggregated `reactions[]` array shaped `{ emoji, count, mine, actorIds[] }`. Aggregation runs server-side so the agent doesn't have to count.

**Write-side (agent → user):** `POST /api/v1/messages/:messageId/reactions` with body `{ emoji, fromAgent? }`. The optional `fromAgent` block tags the reaction with the calling agent's identity (registry id or inline name+avatar). Default emoji set is `👍 ❤️ 🔥 🎉 😂 🤔` plus the full Unicode picker.

**WS events:** `MESSAGE_REACTION_ADDED` / `MESSAGE_REACTION_REMOVED` carry `actor`, `emoji`, and `replaced_emoji` (set when the actor switched). Agents receive these on the same room as inbound messages.

**Use for:** lightweight inter-agent acknowledgements (✅ "received, working on it") that don't burn a turn. The recipient agent's runtime should poll for new reactions on its own messages or subscribe to the WS events.

### 10. Agent-to-agent identity (`fromAgent`)

When an n8n LLM (or any peer AI) injects a message into a BGOS chat, the message renders in a distinct cyan bubble with the agent's name + avatar header instead of looking like a human user message.

**Wire (on `POST /messages` and `POST /send-message`):**
```ts
fromAgent: {
  peerId?: number;            // registry id from agent_peers table (preferred)
  externalId?: string;        // agent's own opaque id (≤ 128 chars)
  name?: string;              // display name (≤ 80 chars)
  avatarUrl?: string;         // https only (≤ 2048 chars)
  color?: string;             // hex
  type?: string;              // 'a-z0-9_-' ≤ 32 chars
}
```

**Hybrid resolver:** registry > inline > null. If `peerId` matches a row in `agent_peers`, that row's display fields win. Otherwise inline fields are persisted on the message row and rendered as-is.

**On the receiving agent's webhook:** payload now carries `fromAgent`, `humanInLoop: true`, and a derived `systemHint` string (see §11 for limits) so the recipient's LLM knows it's talking to a peer rather than a human.

### 11. Cross-channel agent-to-agent (peer side-conversations)

The big one. Any agent on any channel can:

1. **Discover** the user's other assistants (peer agents) via `GET /api/v1/peers`.
2. **Send** a message into a peer's side-thread via `POST /api/v1/peers/:targetAssistantId/send` — the user sees the live exchange unfold inline as a minimalist `<SideConversationCard>` rendered against the parent message in the originator's chat. The peer receives the message tagged with `fromAgent` (see §10) so its LLM knows it's a peer, not a human.
3. **Wait for the peer's reply** synchronously (`waitForReply: true`, capped at 85s server-side) or fire-and-forget then poll the side-thread later.
4. **Close the conversation** with a one-line summary via `POST /api/v1/peers/conversations/close` — flips the card from live (pulsing dot + last 2 turns) to completed-collapsed (static dot + summary line).
5. **Check peer presence** via `GET /api/v1/peers/:peerAssistantId/status` before sending — `{ online, lastSeenAt, hasOpenConversation, conversationId, turnHolderId }`.

**Auth:** every peer call carries the channel's existing auth (`X-API-Key` for n8n, `X-BGOS-Pairing` for Hermes/OpenClaw/Gobot, MCP env for Claude Code) **plus** a new `X-Caller-Assistant-Id` header set to the calling assistant's id. The backend uses this to enforce the introduction matrix and tag the message with the originator.

**Wire (peer endpoints):**

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/api/v1/peers` | — | `[{ assistantId, name, avatarUrl, color, introduced, expiresAt }]` |
| `GET` | `/api/v1/peers/:peerAssistantId/status` | — | `{ online, lastSeenAt, hasOpenConversation, conversationId, turnHolderId }` |
| `POST` | `/api/v1/peers/:targetAssistantId/send` | `{ text, parentMessageId, waitForReply?, timeoutSeconds?, turnState? }` | `{ status, sideThreadChatId, messageId, conversationId, turnState, reply? }` |
| `POST` | `/api/v1/peers/conversations/close` | `{ peerAssistantId, summary? }` | `{ closed, conversationId }` |
| `POST` | `/api/v1/peers/threads/:parentMessageId/complete` | `{ summary }` | `{ ok: true }` |
| `GET` | `/api/v1/peers/inbox` | — | `{ chats: [{ id, assistantId, kind: 'main' \| 'a2a' }] }` (use this for plugin discovery so a2a chats aren't dropped) |

**`status` values:** `'sent'` (success — proceed) or `'requires_introduction'` (200 not 4xx so the calling agent can degrade gracefully and ask the user to enable the row in the BGOS Agent Permissions matrix).

**`turnState` lifecycle hint:**
- `'expecting_reply'` (default) — yields the turn to the peer; their next reply uses your `messageId` as `replyToId`.
- `'more_coming'` — keeps the turn; you intend to send more updates back-to-back.
- `'final'` — closes the conversation server-side; further sends auto-open a new conversation.

**Auto-close:** server cron closes idle peer conversations after 15 minutes with a generic summary. Always prefer calling close yourself with a real summary so the user sees what happened.

**Reply correlation:** when the recipient agent replies, it MUST set `replyToId` to the inbound peer message id. Without it, the originator's `waitForReply` polling falls back to positional matching (works for 1:1 threads but breaks fan-in).

**Idempotency / retry rule:** **do NOT retry `send_to_peer` after a 504 or network timeout** — the message is already saved server-side. Either drop `waitForReply` (cap is 85s anyway) and poll the side-thread later via `GET /api/v1/peers/threads/{parentMessageId}`, or accept the timeout.

**Security model:** discovery is automatic — every agent sees the names+avatars of every other assistant on the user's account. `system_hint` is intentionally omitted to prevent capability leakage between agents. Sending requires an enabled `agent_introductions` row from caller→target (asymmetric — Ava→Hades enabled does NOT imply Hades→Ava). `expires_at` non-null = ephemeral allow-once; null = persistent allow-always.

**WS events:**
- `side_thread_message` — new turn in a side conversation, rendered inside the card.
- `side_thread_completed` — card flips to collapsed-state.
- `introduction_changed` — Agent Permissions matrix updated.
- `peer_conversation_closed` — server cron auto-closed an idle conversation.

**When to use:**
- Your reply needs information another agent on this user's account is better suited for (e.g., DevOps agent → Infra agent for an AWS query).
- Hand-off a multi-step task to a domain-specialist peer.
- Async ack/coordination between agents working on a shared workflow.

**When NOT to use:**
- A simple slash-command would do (e.g., the user can `/reroute hades …` themselves).
- The peer is offline AND you need the answer in this turn — call `peer_status` first or fall back to suggesting the peer in your reply.
- The user has not enabled the introduction — call `list_peers`, see `introduced: false`, then ask the user "Want me to ask Hades?" and stop. Don't auto-send.

**Pre-send pattern (recommended):**
1. Send your own "Looping in <peer>…" reply FIRST and capture its `messageId`.
2. Pass that `messageId` as `parentMessageId` to `send_to_peer` so the SideConversationCard anchors against your own reply.
3. When the exchange ends, call close with a one-line synthesis so the card collapses cleanly.

---

## Per-plugin syntax cheat-sheet

How the agent connected through each plugin actually invokes these capabilities.

### Claude Code MCP plugin (`bgos-claude-plugin`, private repo)

MCP server with typed tools:
- `reply` — text + files + inline buttons + `reply_to_id` (for a2a side-thread correlation). Fields: `chat_id`, `text`, `files[]`, `buttons[]`, `render_mode: "inline" | "modal"`, `reply_to_id?`.
- `ask_user_input` — blocking modal. Fields: `chat_id`, `questions[]`.
- `edit_message`, `rename_chat` — message ops.
- **a2a peer tools (§11):** `list_peers`, `peer_status`, `send_to_peer`, `complete_peer_thread`, `complete_side_thread`. Mapped 1:1 to the canonical endpoints. The plugin's `discoverChats` reads `GET /api/v1/peers/inbox` so a2a chats are polled too.

Agent learns this via the MCP server's `instructions` + per-tool `description` fields. Canonical source: `bgos-claude-plugin/server.ts` lines ~244–337 + tool-registration block.

**When you update this canonical, also update:** `bgos-claude-plugin/server.ts` — the MCP `instructions` string and per-tool `inputSchema` descriptions. Bump plugin version. `bun install`, restart Claude Code session with the new plugin.

### Hermes (`hermes-channel-bgos`, public repo)

Hermes agents learn about BGOS via a `PLATFORM_HINTS` entry in `agent/prompt_builder.py` (inserted by the fork patch at `hermes-fork-patch/0001-bgos-integration.patch`).

**Concrete syntax for agents:**
- **Text** — just reply normally, markdown is honored.
- **Files / media** — include `MEDIA:/absolute/path/to/file` lines in the reply. Handled natively by the adapter's `send_image/voice/video/document/animation` overrides.
- **Approvals** — agent doesn't initiate; Hermes's approvals system calls `adapter.send_exec_approval` when a sensitive tool fires. Rendered as the 4-button bubble.
- **Inline buttons (non-approval)** — embed a `[[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]` block in the reply. Lines inside the block are `Label | value` (pipe-separated), one per line, max 6. Adapter extracts the block and posts `options: [{text, callbackData}]` with `renderMode: 'inline'`. When the user taps a chip, the adapter receives `inbound_click` on the `assistant:<id>` WS room and synthesizes a user `MessageEvent` with `text = <clicked button label>` — the agent sees the tap as a normal user reply.
- **`ask_user_input` modal** — **NOT YET WIRED.** Planned via `[[BGOS_ASK]]...[[/BGOS_ASK]]` marker. Until shipped, use sequential inline-button messages.
- **a2a peer collaboration (§11)** — bridge-local slash commands the user/agent can type:
  - `/peers` — list discoverable peer assistants (markdown table; `introduced ✓/✗`).
  - `/peer-status <name|id>` — print online/offline + open-conversation state.
  - `/peer-send <name|id> <text>` — send to peer; uses the most recent agent message in the chat as `parentMessageId`. Uses `--wait` to block on reply (default fire-and-forget).
  - `/peer-complete <summary>` — close the most recent open peer conversation.
  - The agent itself can also embed `[[BGOS_PEER_SEND name="Hades" text="..."]]` markers in its reply text — adapter extracts and dispatches identically to the slash form.
- **Slash commands from agent to user** — push via `PUT /integrations/assistants/:id/commands` (adapter's `sync_commands_for` method).
- **Home-channel cron** — set `BGOS_HOME_CHANNEL` env var on the Hermes server. Crons scheduled with `deliver="bgos"` route to that chat id.

**When you update this canonical, also update:** `hermes-channel-bgos/hermes-fork-patch/` — regenerate `0001-bgos-integration.patch` with the new `PLATFORM_HINTS` entry. Have users `git pull` + re-apply the patch on their fork, then restart Hermes.

### OpenClaw (`openclaw-channel-bgos`, BGOS monorepo)

Standalone daemon with method-based API:
- `BgosOutbound.sendText({assistantId, chatId, text})` — plain.
- `BgosOutbound.sendButtons({assistantId, chatId, text, options})` — inline options.
- `BgosOutbound.sendApprovalRequest({assistantId, chatId, text, meta, options?})` — 4-button.
- `BgosOutbound.sendAgentError({assistantId, chatId, reason})` — styled error bubble.
- **a2a peer methods (§11):** `BgosPeerClient.listPeers({callerAssistantId})`, `peerStatus({callerAssistantId, peerAssistantId})`, `sendToPeer({callerAssistantId, targetAssistantId, text, parentMessageId, waitForReply?, timeoutSeconds?, turnState?})`, `completePeerThread({callerAssistantId, peerAssistantId, summary?})`, `completeSideThread({callerAssistantId, parentMessageId, summary})`. The daemon also surfaces these as bridge-local slash commands `/peers`, `/peer-status`, `/peer-send`, `/peer-complete` so users can drive them by hand.

OpenClaw agents learn about BGOS via the `BGOS_AGENT_HINTS` system-prompt addendum injected by the daemon at dispatch time (mirrors Gobot's mechanism).

**When you update this canonical, also update:** `openclaw-channel-bgos/src/agent-hints.ts` — keep the addendum in sync. Bump `openclaw-channel-bgos/package.json` version. Release on npm or refresh the VPS install.

### Gobot (`gobot-channel-bgos` + `gobot-bgos-fork`)

Plugin pairs with Gobot via a fork-side adapter. Agents learn about BGOS via the `BGOS_AGENT_HINTS` system-prompt addendum (`gobot-channel-bgos/src/agent-hints.ts`) injected on every dispatch.

**Concrete surface:**
- Text + media + buttons + approvals — embedded markers (see Hermes section + agent-hints).
- **a2a peer methods (§11):** `BgosPeerClient` exposes `listPeers / peerStatus / sendToPeer / completePeerThread / completeSideThread`. Embedded markers `[[BGOS_PEER_SEND name="..." text="..." wait=false]]` and `[[BGOS_PEER_COMPLETE summary="..."]]` work in agent replies — the adapter extracts and dispatches them.
- The `ReplyHandle` passed to the fork's `dispatch()` gains a `peers` namespace: `replyHandle.peers.list()`, `peers.send({ targetAssistantId, text, parentMessageId, waitForReply? })`, `peers.complete({ peerAssistantId, summary? })`, `peers.status({ peerAssistantId })`.

**When you update this canonical, also update:** `gobot-channel-bgos/src/agent-hints.ts` + `gobot-channel-bgos/src/bgos-peer-client.ts`. Bump version. Republish if the fork pulls from npm.

### n8n nodes (`n8n-nodes-bgos`)

`BGOSAction` node has a **Peer Agent** resource with four operations:
- **List Peers** (`listPeers`) — input: caller assistant id. Output: peer list with `introduced` flag.
- **Send to Peer** (`sendToPeer`) — input: caller, target id, parent message id, text, optional `waitForReply`, `timeoutSeconds`, `turnState`. Output: `{status, sideThreadChatId, messageId, conversationId, reply?}`.
- **Complete Peer Thread** (`completePeerThread`) — input: caller, peer id, optional summary. Output: `{closed, conversationId}`.
- **Peer Status** (`peerStatus`) — input: caller, peer id. Output: `{online, lastSeenAt, hasOpenConversation, conversationId, turnHolderId}`.

All operations use the existing `BGOS API` credential (`X-API-Key`) and the node sends `X-Caller-Assistant-Id` automatically from the `Caller Assistant ID` parameter.

**When you update this canonical, also update:** `bgos-n8n-nodes/nodes/BGOSAction/BGOSAction.node.ts` peer-resource section + `bgos-n8n-nodes/nodes/BGOSAction/techWebhook.ts`. Bump `package.json` version, republish to npm.

### Future plugins

Any new plugin (ChatGPT, etc.) follows the `channel_integration_pattern.md` blueprint + agrees to:
1. Consume this canonical doc in its agent-facing surface (however is idiomatic for that channel).
2. Surface §10 (`fromAgent`) and §11 (peer endpoints) — without those, the integration is missing the cross-agent collaboration capability.
3. Subscribe to updates — when a capability here changes, propagate.

---

## Propagation to plugins — checklist

When a BGOS frontend capability changes (new `MessageType`, new DTO field, new UI affordance, changed limit, etc.):

- [ ] **This file first** — update the relevant capability section. Be concrete about wire format + user-visible behavior.
- [ ] **`bgos-claude-plugin`** — update `server.ts` MCP `instructions` string + any affected tool `inputSchema` descriptions. Bump `package.json` version. Push; users `git pull` + re-run with `bun`.
- [ ] **`hermes-channel-bgos`** — update the `PLATFORM_HINTS` BGOS entry in `hermes-fork-patch/0001-bgos-integration.patch` (regenerate patch from a fresh Hermes clone). Update the "Per-plugin syntax cheat-sheet" → Hermes section here. Push; users `git pull` + re-apply patch + restart service.
- [ ] **`openclaw-channel-bgos`** — update `src/agent-hints.ts` + the peer-client surface in `src/bgos-peer-client.ts`. Bump `package.json`. Release.
- [ ] **`gobot-channel-bgos`** — update `src/agent-hints.ts` + `src/bgos-peer-client.ts` + the `ReplyHandle` plumbing in `src/inbound-handler.ts`. Bump `package.json`. Republish if the fork consumes from npm.
- [ ] **`bgos-n8n-nodes`** — update `nodes/BGOSAction/BGOSAction.node.ts` resource definitions + `nodes/BGOSAction/techWebhook.ts` peer helpers + `nodes/BGOSAction/handler/eventHandler.ts` switch. Bump `package.json` version, republish to npm.
- [ ] **Sanity check** — cross-read the five plugin docs side-by-side: do they describe the same capability the same way? Fix drift before shipping.

**Who owns propagation:** whichever developer/agent shipped the frontend change. Don't merge the frontend PR until the plugin updates are also staged (or at least issues are filed).

---

## Versioning

This doc follows the `hermes-channel-bgos` repo's version. Minor bumps whenever a capability section is added or substantially revised. The file's last-modified date is in git; the content isn't dated inline.
