# Changelog

Older releases are described in the README sections named after their version.

## 0.32.0 (2026-10-15)

Per agent poll cursors (Channels and Organizations phase 0, PR 0.3e).

- The inbound cursor is now one per agent (assistant id) in `$HERMES_HOME/bgos_last_ids.json`, not one per gateway. A channel message addressed to two agents on one gateway reaches both: before, the first agent's row moved the shared cursor past the id and was the only one the duplicate guard let through, so the second agent lost it (in the same poll, or after a restart between the two turns).
- The poll starts from the lowest known agent cursor and keeps each row only past its own agent's cursor; the duplicate guard is per (agent, message); an idle agent is carried up to what the poll covered so it cannot pin the window.
- Migration: the old single `bgos_last_id` seeds every agent known at the first poll, so nothing is replayed and nothing is skipped. An agent exposed later starts at the newest consumed id. The old file is left in place and no longer written.
- A WS reconnect backfills from the lowest agent cursor.

## 0.31.0 (2026-10-11)

App control and the Work Stream for Hermes agents (board row 019fc652-952b).

- New `[[BGOS_UI]]action[ | target][[/BGOS_UI]]` marker: during a live HOAI voice call, POSTs one whitelisted in-call UI action to `/api/v1/integrations/voice-sessions/:sessionId/ui-action` (pairing auth). The session id is learned from the backend's consult and dispatch frames for the chat. At most 3 per reply.
- New `[[BGOS_TASK]]{"taskId","result","failed"}[[/BGOS_TASK]]` marker: POSTs the voice task result to `/api/v1/integrations/voice-tasks/:taskId/result`, the body `complete_voice_task` sends. The dispatch runner skips its automatic report for a task the marker settled.
- Both markers run on the final text, whether it arrives as a send() or as a streamed reply's finalize edit, and each block runs once per chat within 120 seconds even when a preview send and the final edit both carry it. Interim streaming edits only hide them (including a block still streaming); the standalone send path only strips complete blocks. Blocks inside fenced or inline code are documentation and never run. Malformed blocks are logged and not executed.
- A voice session learned from a frame is forgotten after 3 hours.
- The `[voice dispatch]` turn now tells the agent how to mark a task failed.
- The bundled fallback platform hint documents both markers.
