# Changelog

Older releases are described in the README sections named after their version.

## 0.33.0 (2026-10-31)

Another agent's tag in a group reaches a Hermes agent, and a deleted agent's route is dropped at once.

- An agent's `@Name` of another agent seated in a group or a channel wakes that agent (HOAI #3703). The backend hands the row over marked `senderType: 'room_agent'` with the author's `senderAssistantId`, and serves it only to a daemon that reads the marker, because one that does not would take it as its owner speaking. The adapter now reads it from the live push and the poll alike: the event carries `sender_type` and `sender_assistant_id`, the text always says another agent wrote it and not a person (the server's framing line, or the adapter's own when that line is missing), the row is never a bridge-local command, never batched with a person's words and never cached for `/retry`, the agent's own row is dropped, and the first real reply answers the tag with `reply_to_id` set to its message. A tool progress card or a suppressed tool dump never uses that anchor up; a person writing or tapping a button in the chat first drops it, and it lapses after 30 minutes, so a later unrelated post never quotes the tag.
- The WebSocket `assistant_unbound` event (an agent deleted or unbound while the pairing still serves others, HOAI #3706) now drops that agent's route and leaves its room at once, instead of keeping a dead route until the next restart. An event for another pairing is ignored.
- A reply in a group or a channel now names the agent the row was addressed to. Before, it named the chat row's assistant, which for a group is its carrier (the creator's first agent), so `/send-message` refused the reply or posted it as that agent. A tag no longer overtakes a person's words still waiting in the text batch, and an `agent_error` card leaves the tag's anchor for the real reply.

## 0.32.1 (2026-10-19)

Safe media fetch, a working /reload-mcp confirm, and backoff on a revoked pairing.

- Remote media (an http or https link an agent emits for `send_image`, `send_animation` or a `MEDIA:` marker) is fetched through Hermes's public `tools.url_safety` helpers (hermes-agent v0.21.7 and later). The URL and every redirect target must be a public address (loopback, private, link local and cloud metadata addresses are refused), redirects are followed by hand up to 5 hops, and the body is streamed and cut off at 25 MiB (a declared larger length is refused before reading). If the helpers cannot be imported, remote fetches are refused (fail closed); local and `file://` paths are unchanged and keep their 100 MiB cap and sensitive path check.
- The Approve and Cancel buttons on the `/reload-mcp` prompt did nothing: the adapter imported `gateway.slash_confirm` (which does not exist) and called the resolver without `await`. It now imports `tools.slash_confirm.resolve` and awaits it.
- A revoked pairing no longer keeps polling. The backend answers a revoked token with a plain 401, so the adapter counts consecutive 401s across every authenticated call (30 in a row over at least 60 seconds, as in the Claude Code plugin). It then logs one line, stops the 5 second poll, and sends one probe every 5 minutes; the first answer that is not a refusal resumes polling and sends a heartbeat. A single 401 followed by a good answer never pauses. The 6 hour heartbeat feeds the same count and is skipped while paused. The WebSocket `pairing_revoked` event pauses at once.

## 0.32.0 (2026-10-15)

Per agent poll cursors (Channels and Organizations phase 0, PR 0.3e).

- The inbound cursor is now one per agent (assistant id) in `$HERMES_HOME/bgos_last_ids.json`, not one per gateway. A channel message addressed to two agents on one gateway reaches both: before, the first agent's row moved the shared cursor past the id and was the only one the duplicate guard let through, so the second agent lost it (in the same poll, or after a restart between the two turns).
- The poll starts from the lowest known agent cursor and keeps each row only past its own agent's cursor; the duplicate guard is per (agent, message); an idle agent is carried up to what the poll covered so it cannot pin the window.
- Migration: the old single `bgos_last_id` seeds every agent known at the first poll, so nothing is replayed and nothing is skipped. An agent exposed later starts at the newest consumed id. The old file is left in place and no longer written.
- A WS reconnect backfills from the lowest agent cursor.
- Live WS pushes batch text per (chat, agent), so the two events of a message addressed to two agents are not merged into one dispatch.
- An agent removed from the pairing and exposed again while the gateway runs rejoins at the newest consumed id instead of replaying what arrived while it was away.
- A message dropped because its assistant is not known yet is kept in memory (200 messages, one hour) and delivered once the agent is exposed; each poll asks the pairing again while one waits. Its id is also persisted in `$HERMES_HOME/bgos_pending_unknown.json` (50 live records held one hour; an expired one is remembered for a day so the same row does not start a new hour): the poll window reaches back to it, an agent bound later starts below it and the idle carry stops below it, so a restart before the agent is bound fetches the row again instead of losing it. The memory copy now lasts as long as the record (one hour), and while this process holds a row the poll window is not anchored at it. Held rows do not count toward `BGOS_BACKFILL_STORM_LIMIT`, and a record is released when its row is consumed.

## 0.31.0 (2026-10-11)

App control and the Work Stream for Hermes agents (board row 019fc652-952b).

- New `[[BGOS_UI]]action[ | target][[/BGOS_UI]]` marker: during a live HOAI voice call, POSTs one whitelisted in-call UI action to `/api/v1/integrations/voice-sessions/:sessionId/ui-action` (pairing auth). The session id is learned from the backend's consult and dispatch frames for the chat. At most 3 per reply.
- New `[[BGOS_TASK]]{"taskId","result","failed"}[[/BGOS_TASK]]` marker: POSTs the voice task result to `/api/v1/integrations/voice-tasks/:taskId/result`, the body `complete_voice_task` sends. The dispatch runner skips its automatic report for a task the marker settled.
- Both markers run on the final text, whether it arrives as a send() or as a streamed reply's finalize edit, and each block runs once per chat within 120 seconds even when a preview send and the final edit both carry it. Interim streaming edits only hide them (including a block still streaming); the standalone send path only strips complete blocks. Blocks inside fenced or inline code are documentation and never run. Malformed blocks are logged and not executed.
- A voice session learned from a frame is forgotten after 3 hours.
- The `[voice dispatch]` turn now tells the agent how to mark a task failed.
- The bundled fallback platform hint documents both markers.
