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
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        pass

    async def handle_message(self, event: Any) -> None:  # noqa: D401
        """Called by the adapter to feed inbound user messages into Hermes."""
        pass

    async def handle_button_press(self, data: dict) -> None:  # noqa: D401
        """Called by the adapter for non-approval button callbacks."""
        pass


@dataclass
class SendResult:
    """Stand-in for `gateway.platforms.base.SendResult`.

    Mirrors the fork's field names: `success` (bool) and `message_id` (str).
    Earlier drafts used `ok` and an int here — but the real fork uses
    success + str, and bypassing that caused TypeError at runtime (caught
    during live testing on kc's server 2026-04-24).
    """

    message_id: str | None = None
    success: bool = True
