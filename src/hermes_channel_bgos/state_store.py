"""In-process state for the BGOS adapter.

Tracks assistant→route mapping, retry cache (last user text per chat),
conversation bindings per chat (used by /new slash command in Task 9),
and the last assistant-message id per chat (used by streaming edits in
Phase 3, if we get there).

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
    # `/peer-complete` needs to know which peer conversation to close. We
    # track the most recent open one per chat as `(peer_assistant_id,
    # conversation_id)`. Cleared by `/peer-complete` and (on backend cron
    # auto-close) by the `peer_conversation_closed` WS event handler.
    peer_conversation_by_chat: dict[int, tuple[int, int]] = field(default_factory=dict)

    def set_route(self, assistant_id: int, route: str) -> None:
        self.assistant_route[assistant_id] = route

    def get_route(self, assistant_id: int) -> str | None:
        return self.assistant_route.get(assistant_id)

    def remove_assistant(self, assistant_id: int) -> None:
        self.assistant_route.pop(assistant_id, None)

    def reset_conversation(self, chat_id: int) -> None:
        self.conversation_by_chat.pop(chat_id, None)
