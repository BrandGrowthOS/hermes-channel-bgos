"""Socket.IO client for the BGOS backend.

Connects with `?pairingToken=<raw>` as a query parameter, joins its
`pairing:<id>` + `assistant:<id>` rooms, and dispatches the two events the
adapter cares about (`inbound_message`, `callback_result`) to caller-supplied
callbacks. Supports sync or async callbacks.

Reconnect behavior is delegated to python-socketio's built-in exponential
backoff. After each successful (re)connection beyond the first, the optional
`on_reconnect` callback fires with the highest `message_id` seen so far -
the caller uses it to drive a REST backfill via
`GET /api/v1/integrations/inbound?since_message_id=...`.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import socketio

from .config import BgosConfig
from .skills_bridge import SkillsBridge

log = logging.getLogger(__name__)


_Handler = Callable[[dict], None] | Callable[[dict], Awaitable[None]]
_ReconnectHandler = Callable[[int], None] | Callable[[int], Awaitable[None]]


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


class BgosWs:
    def __init__(
        self,
        config: BgosConfig,
        *,
        on_inbound_message: _Handler,
        on_callback_result: _Handler,
        on_reconnect: _ReconnectHandler | None = None,
        on_inbound_click: _Handler | None = None,
        on_voice_rpc: _Handler | None = None,
        reconnection_delay: float = 1.0,
        reconnection_delay_max: float = 30.0,
    ) -> None:
        self._config = config
        self._on_inbound = on_inbound_message
        self._on_callback = on_callback_result
        self._on_reconnect = on_reconnect
        self._on_inbound_click = on_inbound_click
        self._on_voice_rpc = on_voice_rpc
        self._skills_bridge: SkillsBridge | None = None

        self._assistants: set[int] = set()
        self._pairing_id: int | None = None
        self._last_message_id: int = 0
        self._was_connected = False

        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_delay=reconnection_delay,
            reconnection_delay_max=reconnection_delay_max,
        )
        self._register_handlers()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    @property
    def last_message_id(self) -> int:
        return self._last_message_id

    def bind_assistants(self, ids: list[int]) -> None:
        """Set the list of assistant IDs to subscribe to on next (re)connect.

        If already connected, the newly-added assistants are joined immediately.
        """
        new_set = set(ids)
        to_join = new_set - self._assistants
        to_leave = self._assistants - new_set
        self._assistants = new_set
        if self._sio.connected:
            asyncio.create_task(self._apply_room_deltas(to_join, to_leave))

    def unbind_assistant(self, assistant_id: int) -> None:
        if assistant_id not in self._assistants:
            return
        self._assistants.discard(assistant_id)
        if self._sio.connected:
            asyncio.create_task(
                self._apply_room_deltas(set(), {assistant_id}),
            )

    def bind_pairing(self, pairing_id: int) -> None:
        self._pairing_id = pairing_id

    async def start(self) -> None:
        url = self._build_connect_url()
        await self._sio.connect(url, transports=["websocket", "polling"])

    async def stop(self) -> None:
        if self._sio.connected:
            await self._sio.disconnect()

    async def emit_typing(self, *, chat_id: int, assistant_id: int) -> None:
        """Emit a `typing` Socket.IO event so the backend can forward an
        ephemeral typing indicator to clients viewing this chat.

        Best-effort: if the WS isn't connected, returns silently. The
        backend may not handle this event yet - Socket.IO drops unknown
        events server-side, so this is forward-safe.
        """
        if not self._sio.connected:
            return
        try:
            await self._sio.emit("typing", {
                "chatId": chat_id,
                "assistantId": assistant_id,
            })
        except Exception:
            log.debug("emit_typing failed (non-fatal)", exc_info=True)

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _build_connect_url(self) -> str:
        """Attach `pairingToken` as a query string on the base URL.

        python-socketio's connect() wants a scheme+host URL plus optional query.
        We parse the configured base_url so we don't double-append a question mark
        if one exists.
        """
        if not self._config.pairing_token:
            raise RuntimeError("pairing_token required for Socket.IO connect")
        parts = urlsplit(self._config.base_url)
        existing = parts.query
        new_qs = (existing + "&" if existing else "") + f"pairingToken={self._config.pairing_token}"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_qs, parts.fragment))

    def _register_handlers(self) -> None:
        @self._sio.event  # type: ignore[misc]
        async def connect() -> None:
            log.info("bgos_ws.connected pairing_id=%s assistants=%s",
                     self._pairing_id, sorted(self._assistants))
            await self._join_current_rooms()
            if self._was_connected and self._on_reconnect is not None:
                try:
                    await _maybe_await(self._on_reconnect(self._last_message_id))
                except Exception:
                    log.exception("bgos_ws.on_reconnect callback failed")
            self._was_connected = True

        @self._sio.event  # type: ignore[misc]
        async def disconnect() -> None:
            log.warning("bgos_ws.disconnected")

        @self._sio.on("inbound_message")  # type: ignore[misc]
        async def _inbound(data: dict) -> None:
            # Backend may send snake_case (REST backfill path) or camelCase
            # (live WS - see _INBOUND_CAMEL_ALIASES in bgos_adapter.py).
            # Read both so the DEBUG line is informative and the cursor
            # advances no matter which shape the transport delivered.
            # Without the camelCase fallback this log printed None even
            # though the downstream adapter saw real values - caught live
            # on kc's server 2026-05-13.
            mid = data.get("message_id") or data.get("messageId")
            log.debug(
                "bgos_ws.inbound_message message_id=%s chat_id=%s "
                "assistant_id=%s message_type=%s text_len=%d files=%d",
                mid,
                data.get("chat_id") or data.get("chatId"),
                data.get("assistant_id") or data.get("assistantId"),
                data.get("message_type") or data.get("messageType"),
                len(data.get("text") or ""),
                len(data.get("files") or []),
            )
            if isinstance(mid, int) and mid > self._last_message_id:
                self._last_message_id = mid
            try:
                await _maybe_await(self._on_inbound(data))
            except Exception:
                log.exception("bgos_ws.on_inbound_message callback failed")

        @self._sio.on("callback_result")  # type: ignore[misc]
        async def _callback(data: dict) -> None:
            try:
                await _maybe_await(self._on_callback(data))
            except Exception:
                log.exception("bgos_ws.on_callback_result callback failed")

        @self._sio.on("inbound_click")  # type: ignore[misc]
        async def _inbound_click(data: dict) -> None:
            if self._on_inbound_click is None:
                log.debug("inbound_click received but no handler registered")
                return
            try:
                await _maybe_await(self._on_inbound_click(data))
            except Exception:
                log.exception("bgos_ws.on_inbound_click callback failed")

        @self._sio.on("voice_rpc")  # type: ignore[misc]
        async def _voice_rpc(data: dict) -> None:
            # Native in-app voice control frames (spec §6.2/§6.3) - the
            # backend pushes these into the pairing:<id> room. Validation
            # and op whitelisting happen downstream in
            # voice_rpc.normalize_voice_rpc; this layer only dispatches.
            if self._on_voice_rpc is None:
                log.debug("voice_rpc received but no handler registered")
                return
            try:
                await _maybe_await(self._on_voice_rpc(data))
            except Exception:
                log.exception("bgos_ws.on_voice_rpc callback failed")

        @self._sio.on("skills_rpc")  # type: ignore[misc]
        async def _skills_rpc(data: dict) -> None:
            try:
                await self._skills_bridge_dispatch(data)
            except Exception:
                log.exception("bgos_ws.skills_rpc handler failed")

    async def _skills_bridge_dispatch(self, data: dict) -> None:
        if self._skills_bridge is None:
            self._skills_bridge = SkillsBridge(self._config)
        await self._skills_bridge.handle_frame(data)

    async def _join_current_rooms(self) -> None:
        if self._pairing_id is not None:
            await self._sio.emit("join", {"room": f"pairing:{self._pairing_id}"})
        for aid in sorted(self._assistants):
            await self._sio.emit("join", {"room": f"assistant:{aid}"})

    async def _apply_room_deltas(self, to_join: set[int], to_leave: set[int]) -> None:
        for aid in sorted(to_leave):
            await self._sio.emit("leave", {"room": f"assistant:{aid}"})
        for aid in sorted(to_join):
            await self._sio.emit("join", {"room": f"assistant:{aid}"})
