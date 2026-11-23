"""Test stand-in for Hermes's ``tools/slash_confirm.py``.

A faithful copy of NousResearch/hermes-agent ``tools/slash_confirm.py`` at
main 249dc60308 (2026-11-23), so the tests drive the adapter against the
real module shape: module-level pending state, ``register`` and an ASYNC
``resolve(session_key, confirm_id, choice, timeout=...)`` that returns the
handler's text, or None when the confirm is stale, already resolved or
superseded. The one change: ``register`` no longer reads the turn's
authority from ``tools.approval_context`` (not present here), so it
defaults to None.

The tests load this file as ``tools.slash_confirm`` (see
tests/test_slash_confirm_resolver.py); it is not a test module.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# session_key -> {"confirm_id", "command", "handler", "created_at", "authority"}
_pending: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()

DEFAULT_TIMEOUT_SECONDS = 300


def register(session_key: str, confirm_id: str, command: str,
             handler: Callable[[str], Awaitable[Optional[str]]], authority: Optional[dict] = None) -> None:
    """Register a pending confirm, superseding any prior one for the session."""
    with _lock:
        _pending[session_key] = {"confirm_id": confirm_id, "command": command,
                                 "handler": handler, "created_at": time.time(), "authority": authority}


def get_pending(session_key: str) -> Optional[dict[str, Any]]:
    """Return a copy of the pending confirm dict for a session, or None."""
    with _lock:
        entry = _pending.get(session_key)
        return dict(entry) if entry else None


def clear(session_key: str) -> None:
    """Drop the pending confirm for ``session_key`` without running it."""
    with _lock:
        _pending.pop(session_key, None)


def _is_stale(entry: dict[str, Any], timeout: float) -> bool:
    return time.time() - float(entry.get("created_at", 0) or 0) > timeout


def clear_if_stale(session_key: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Drop the pending confirm if older than ``timeout`` seconds; True if dropped."""
    with _lock:
        entry = _pending.get(session_key)
        stale = bool(entry and _is_stale(entry, timeout))
        if stale:
            _pending.pop(session_key, None)
        return stale


async def resolve(session_key: str, confirm_id: str, choice: str,
                  timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Optional[str]:
    """Run the pending handler with ``choice`` ("once" / "always" / "cancel").

    Returns the handler's output string, or None if the confirm was stale, already resolved,
    or the confirm_id doesn't match (superseded prompt).
    """
    with _lock:
        entry = _pending.get(session_key)
        if not entry or entry.get("confirm_id") != confirm_id:
            return None
        # Pop before running so duplicate callbacks (button double-click) cannot run it twice.
        _pending.pop(session_key, None)
        if _is_stale(entry, timeout):
            return None
        handler = entry.get("handler")
        command = entry.get("command", "?")

    if not handler:
        return None
    try:
        result = await handler(choice)
    except Exception as exc:
        logger.error("Slash-confirm handler for /%s raised: %s", command, exc, exc_info=True)
        return f"❌ Error handling confirmation: {exc}"
    return result if isinstance(result, str) else None
