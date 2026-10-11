# Changelog

Older releases are described in the README sections named after their version.

## 0.31.0 (2026-10-11)

App control and the Work Stream for Hermes agents (board row 019fc652-952b).

- New `[[BGOS_UI]]action[ | target][[/BGOS_UI]]` marker: during a live HOAI voice call, POSTs one whitelisted in-call UI action to `/api/v1/integrations/voice-sessions/:sessionId/ui-action` (pairing auth). The session id is learned from the backend's consult and dispatch frames for the chat. At most 3 per reply.
- New `[[BGOS_TASK]]{"taskId","result","failed"}[[/BGOS_TASK]]` marker: POSTs the voice task result to `/api/v1/integrations/voice-tasks/:taskId/result`, the body `complete_voice_task` sends. The dispatch runner skips its automatic report for a task the marker settled.
- Both markers are stripped from visible text on send, hidden in streaming edits (including a block still streaming), and stripped on the standalone send path. Malformed blocks are logged and not executed.
- The `[voice dispatch]` turn now tells the agent how to mark a task failed.
- The bundled fallback platform hint documents both markers.
