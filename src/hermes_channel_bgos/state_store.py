"""In-process state for the BGOS adapter.

Tracks assistant→route mapping, retry cache (last user text per chat),
conversation bindings per chat (used by /new slash command in Task 9),
the last assistant-message id per chat (used by streaming edits), and
the last user_id seen per chat (passed on PATCH /api/v1/messages so
the backend's DTO validation that requires userId on edits is happy —
caught live 2026-05-13).

Not persisted. On adapter restart, state is rebuilt from `whoami()` +
REST inbound backfill. Losing the retry cache is acceptable.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StateStore:
    assistant_route: dict[int, str] = field(default_factory=dict)
    last_user_text_by_chat: dict[int, str] = field(default_factory=dict)
    conversation_by_chat: dict[int, str] = field(default_factory=dict)
    last_assistant_message_by_chat: dict[int, int] = field(default_factory=dict)
    last_user_id_by_chat: dict[int, str] = field(default_factory=dict)
    # The assistant addressed by the inbound event. This can differ from the
    # chat owner for a2a side threads, so style lookup must keep it separate.
    addressed_assistant_id_by_chat: dict[int, int] = field(default_factory=dict)
    assistant_id_by_chat: dict[int, int] = field(default_factory=dict)
    chat_kind_by_chat: dict[int, str] = field(default_factory=dict)
    # Server-authoritative chat addressing (2026-05-30 hardening). The
    # adapter must NEVER originate a chat_id the agent invented — every
    # outbound send/edit/get_chat_info MUST target a chat the adapter has
    # actually received on a prior inbound MessageEvent. `received_chat_ids`
    # is the allow-set of chats seen inbound; agent-supplied ids absent from
    # it are rejected before dispatch. See
    # docs/bgos-agent-capabilities.md §"Chat addressing".
    received_chat_ids: set[int] = field(default_factory=set)
    # Opaque, HMAC-signed `sessionHandle` carried on every inbound event,
    # keyed by chat_id. When present the adapter sends the handle back in
    # the outbound POST body (`sessionHandle` field, which the backend
    # prioritizes over a raw `chatId`) instead of a raw chat id. Latest
    # handle wins — the backend re-issues per inbound event.
    session_handle_by_chat: dict[int, str] = field(default_factory=dict)
    # Replies already returned synchronously by /peers/:id/send waitForReply.
    # If the same side-thread message later arrives by WS or REST backfill, the
    # adapter advances the cursor but suppresses dispatch to avoid ping-pong.
    consumed_peer_wait_replies: list[dict] = field(default_factory=list)

    def record_inbound_chat(
        self, chat_id: int, session_handle: str | None = None
    ) -> None:
        """Remember that `chat_id` was received on an inbound event, and
        stash its `sessionHandle` if the event carried one.

        Called from the inbound path so subsequent outbound send/edit calls
        can (a) validate the agent isn't targeting a chat the adapter never
        saw and (b) round-trip the opaque handle back to the server.
        """
        self.received_chat_ids.add(chat_id)
        if session_handle:
            self.session_handle_by_chat[chat_id] = session_handle

    def has_received_chat(self, chat_id: int) -> bool:
        return chat_id in self.received_chat_ids

    def session_handle_for_chat(self, chat_id: int) -> str | None:
        return self.session_handle_by_chat.get(chat_id)

    def set_route(self, assistant_id: int, route: str) -> None:
        self.assistant_route[assistant_id] = route

    def get_route(self, assistant_id: int) -> str | None:
        return self.assistant_route.get(assistant_id)

    def remove_assistant(self, assistant_id: int) -> None:
        self.assistant_route.pop(assistant_id, None)

    def reset_conversation(self, chat_id: int) -> None:
        self.conversation_by_chat.pop(chat_id, None)
