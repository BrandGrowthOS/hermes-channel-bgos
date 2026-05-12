"""Minimal stand-in for Hermes's `gateway.platforms.base` module.

In production, `BGOSAdapter` imports `BasePlatformAdapter` (and friends) from
the real Hermes install via `from gateway.platforms.base import ...`. In the
test environment we don't install Hermes — instead we expose the same symbol
names here, and `bgos_adapter.py` falls back to this module when the real
import fails (see the `try/except ImportError` at the top of bgos_adapter.py).

Keep this surface intentionally thin — only the abstract-contract methods and
data types that BGOSAdapter actually interacts with. If the real Hermes ever
changes its surface, we revisit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class BasePlatformAdapter:
    """Stand-in for `gateway.platforms.base.BasePlatformAdapter`.

    Real class has 4 abstract methods (connect, disconnect, send, get_chat_info)
    plus optional class-level hooks (`send_image`, `send_exec_approval`, etc.)
    that the gateway duck-types. The mock just provides a pass-through __init__
    and async stubs for `handle_message` + `handle_button_press` that the
    adapter calls internally on inbound events.

    The optional `edit_message` / `delete_message` / `send_typing` defaults
    are present on the real `BasePlatformAdapter` (see Hermes
    `gateway/platforms/base.py`) — the gateway probes whether an adapter
    overrides them via `type(adapter).method is BasePlatformAdapter.method`
    to decide whether to drive the tool-progress / streaming / typing UI.
    We mirror them here so that identity check works in tests too.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        pass

    async def handle_message(self, event: Any) -> None:  # noqa: D401
        """Called by the adapter to feed inbound user messages into Hermes."""
        pass

    async def handle_button_press(self, data: dict) -> None:  # noqa: D401
        """Called by the adapter for non-approval button callbacks."""
        pass

    async def edit_message(  # noqa: D401
        self,
        chat_id: Any,
        message_id: Any,
        content: str,
        *,
        finalize: bool = False,
    ) -> "SendResult":
        """Base default — returns failure so the gateway falls back to send().
        Concrete adapters override this to unlock streaming/tool-progress UX."""
        return SendResult(success=False, error="not_implemented")

    async def delete_message(  # noqa: D401
        self,
        chat_id: Any,
        message_id: Any,
    ) -> bool:
        """Base default — returns False (no-op) so the gateway leaves the
        message visible. Concrete adapters override to clean up streaming
        previews."""
        return False

    async def send_typing(  # noqa: D401
        self,
        chat_id: Any,
        metadata: dict | None = None,
    ) -> None:
        """Base default — does nothing. Concrete adapters override to emit a
        typing indicator during long-running operations."""
        return None

    async def send_slash_confirm(  # noqa: D401
        self,
        chat_id: Any,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: dict | None = None,
    ) -> "SendResult":
        """Base default — returns failure so the gateway falls back to plain
        text. Concrete adapters override to render a proper 3-button UI.
        Mirrors the real fork's `BasePlatformAdapter.send_slash_confirm` at
        gateway/platforms/base.py line ~1711."""
        return SendResult(success=False, error="Not supported")


@dataclass
class SendResult:
    """Stand-in for `gateway.platforms.base.SendResult`.

    Mirrors the fork's field names: `success` (bool) and `message_id` (str).
    Earlier drafts used `ok` and an int here — but the real fork uses
    success + str, and bypassing that caused TypeError at runtime (caught
    during live testing on kc's server 2026-04-24).

    `error` is an optional machine-readable failure code used by the
    adapter's `edit_message` to signal a not-editable response so the
    gateway can fall back to a fresh `send()` cleanly.
    """

    message_id: str | None = None
    success: bool = True
    error: str | None = None
