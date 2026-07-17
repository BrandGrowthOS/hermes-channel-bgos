"""Bridge BGOS memory RPC frames to the local Hermes memory machinery."""
from __future__ import annotations

import asyncio
import importlib
import logging
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from .config import BgosConfig


log = logging.getLogger(__name__)

_RPC_ROOT = "/api/v1/integrations/memory-rpc"
_HTTP_TIMEOUT_SECONDS = 10.0
_SEEN_RPC_CAP = 256
_MAX_TEXT_LENGTH = 4000
_MAX_ERROR_LENGTH = 300
_MAX_QUERY_LENGTH = 200
_DEFAULT_SEARCH_LIMIT = 10
_MAX_SEARCH_LIMIT = 10
_ENTRY_DELIMITER = "\n§\n"
_SUPPORTED_OPS = {"list", "add", "replace", "remove", "search"}
_MACHINERY_UNAVAILABLE = "memory machinery unavailable on this host"
_TARGETS = ("memory", "user")


class _MachineryUnavailable(ImportError):
    pass


def _load_store() -> Any:
    """Import Hermes memory support and return a fresh on-disk store."""
    try:
        memory_module = importlib.import_module("tools.memory_tool")
    except (ImportError, AttributeError) as exc:
        raise _MachineryUnavailable(_MACHINERY_UNAVAILABLE) from exc

    try:
        load_on_disk_store = getattr(memory_module, "load_on_disk_store", None)
    except (ImportError, AttributeError) as exc:
        raise _MachineryUnavailable(_MACHINERY_UNAVAILABLE) from exc
    if load_on_disk_store is not None:
        return load_on_disk_store()

    try:
        store_type = getattr(memory_module, "MemoryStore")
    except (ImportError, AttributeError) as exc:
        raise _MachineryUnavailable(_MACHINERY_UNAVAILABLE) from exc
    store = store_type()
    store.load_from_disk()
    return store


def _load_threat_scanner() -> Callable[..., list[str]] | None:
    try:
        threat_module = importlib.import_module("tools.threat_patterns")
        scanner = getattr(threat_module, "scan_for_threats")
    except (ImportError, AttributeError):
        return None
    return scanner if callable(scanner) else None


def _entry_payloads(
    entries: list[str],
    scanner: Callable[..., list[str]] | None,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for entry in entries:
        patterns = scanner(entry, scope="strict") if scanner is not None else []
        payloads.append(
            {
                "text": entry,
                "flagged": bool(patterns),
                "patterns": patterns,
            }
        )
    return payloads


def _stores_payload(store: Any) -> dict[str, Any]:
    scanner = _load_threat_scanner()
    memory_entries = list(store.memory_entries)
    user_entries = list(store.user_entries)
    return {
        "stores": {
            "memory": {
                "entries": _entry_payloads(memory_entries, scanner),
                "chars": len(_ENTRY_DELIMITER.join(memory_entries)),
                "limit": store.memory_char_limit,
            },
            "user": {
                "entries": _entry_payloads(user_entries, scanner),
                "chars": len(_ENTRY_DELIMITER.join(user_entries)),
                "limit": store.user_char_limit,
            },
        }
    }


def _load_stores_payload() -> dict[str, Any]:
    return _stores_payload(_load_store())


def _load_session_db() -> Any:
    try:
        state_module = importlib.import_module("hermes_state")
        session_db_type = getattr(state_module, "SessionDB")
    except (ImportError, AttributeError) as exc:
        raise _MachineryUnavailable(_MACHINERY_UNAVAILABLE) from exc
    return session_db_type()


def _search_payload(
    query: str,
    limit: int,
    owner_user_id: str,
) -> dict[str, Any]:
    db = _load_session_db()
    rows = db.search_messages(
        query,
        exclude_sources=["subagent", "tool"],
        limit=50,
    )

    seen_session_ids: set[Any] = set()
    non_cron_hits: list[dict[str, Any]] = []
    cron_hits: list[dict[str, Any]] = []
    for row in rows:
        session_id = row.get("session_id")
        if session_id in seen_session_ids:
            continue
        seen_session_ids.add(session_id)

        session = db.get_session(session_id) or {}
        source = row.get("source")
        if source == "bgos":
            session_user_id = session.get("user_id")
            if (
                not owner_user_id
                or session_user_id is None
                or str(session_user_id) != str(owner_user_id)
            ):
                continue

        chat_id = None
        if source == "bgos":
            raw_chat_id = session.get("chat_id")
            if raw_chat_id not in (None, ""):
                chat_id = str(raw_chat_id)
        timestamp = row.get("timestamp")
        hit = {
            "sessionId": str(session_id),
            "title": session.get("title") or None,
            "when": str(timestamp) if timestamp is not None else None,
            "source": source,
            "snippet": row.get("snippet"),
            "chatId": chat_id,
            "openable": bool(chat_id),
        }
        if source == "cron":
            cron_hits.append(hit)
        else:
            non_cron_hits.append(hit)

    hits = (non_cron_hits + cron_hits)[:limit]
    return {"hits": hits, "count": len(hits)}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "payload": payload}


def _validate_text(payload: dict[str, Any], field: str) -> dict[str, Any] | None:
    value = payload.get(field)
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT_LENGTH:
        return _error(
            "bad_request",
            f"{field} must be a non-empty string of at most {_MAX_TEXT_LENGTH} chars",
        )
    return None


def _validate_payload(op: str, payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return _error("bad_request", "payload must be an object")
    if op == "list":
        return None
    if op == "search":
        query = payload.get("query")
        if (
            not isinstance(query, str)
            or len(query.strip()) < 2
            or len(query) > _MAX_QUERY_LENGTH
        ):
            return _error(
                "bad_request",
                f"query must be a string between 2 and {_MAX_QUERY_LENGTH} chars",
            )
        return None
    if payload.get("target") not in _TARGETS:
        return _error("bad_request", "target must be memory or user")
    if op == "add":
        return _validate_text(payload, "content")
    if op == "replace":
        return _validate_text(payload, "oldText") or _validate_text(
            payload,
            "newContent",
        )
    return _validate_text(payload, "oldText")


def _sanitize_error_message(message: str) -> str:
    sanitized = message.replace(chr(0x2014), ", ").replace(chr(0x2013), ", ")
    while "  " in sanitized:
        sanitized = sanitized.replace("  ", " ")
    return sanitized[:_MAX_ERROR_LENGTH]


def _coerce_search_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        limit = _DEFAULT_SEARCH_LIMIT
    return max(1, min(_MAX_SEARCH_LIMIT, limit))


def _write_error(result: dict[str, Any]) -> dict[str, Any]:
    raw_error = result.get("error")
    message = raw_error if isinstance(raw_error, str) else "memory write failed"
    if "Refusing to write" in message:
        code = "store_busy"
    elif "No entry matched" in message:
        code = "no_match"
    elif "Multiple entries matched" in message:
        code = "ambiguous"
    elif any(
        phrase in message
        for phrase in (
            "would exceed the limit",
            "over the limit",
            "would put memory at",
        )
    ):
        code = "over_budget"
    else:
        code = "write_failed"
    return _error(code, _sanitize_error_message(message))


class MemoryBridge:
    """Handle memory RPC frames for one BGOS pairing."""

    def __init__(self, config: BgosConfig) -> None:
        self._config = config
        self._mutation_lock = asyncio.Lock()
        self._inflight_rpc_ids: set[str] = set()
        self._recent_rpc_ids: set[str] = set()
        self._recent_rpc_order: deque[str] = deque()

    async def handle_frame(self, frame: dict) -> None:
        if not isinstance(frame, dict):
            log.warning("memory_bridge dropped non-object frame")
            return
        rpc_id = frame.get("rpcId")
        op = frame.get("op")
        if not isinstance(rpc_id, str) or not rpc_id.strip():
            log.warning("memory_bridge dropped frame without rpcId")
            return
        if not isinstance(op, str):
            log.warning("memory_bridge dropped frame without op rpc=%s", rpc_id)
            return
        if not self._claim_rpc(rpc_id):
            log.info("memory_bridge duplicate frame ignored rpc=%s", rpc_id)
            return

        try:
            try:
                await self._post(f"{_RPC_ROOT}/{rpc_id}/ack", {})
            except Exception:
                log.warning(
                    "memory_bridge ack post failed rpc=%s",
                    rpc_id,
                    exc_info=True,
                )
            try:
                if op not in _SUPPORTED_OPS:
                    result = _error("bad_request", "unsupported op")
                else:
                    payload = frame.get("payload", {})
                    validation_error = _validate_payload(op, payload)
                    if validation_error is not None:
                        result = validation_error
                    else:
                        result = await self._dispatch(op, payload)
            except _MachineryUnavailable:
                result = _error("unavailable", _MACHINERY_UNAVAILABLE)
            except Exception:
                log.exception(
                    "memory_bridge operation failed rpc=%s op=%s",
                    rpc_id,
                    op,
                )
                result = _error(
                    "write_failed",
                    "memory operation failed on the agent host",
                )

            await self._post_result(rpc_id, result)
        finally:
            self._complete_rpc(rpc_id)

    def _claim_rpc(self, rpc_id: str) -> bool:
        if rpc_id in self._inflight_rpc_ids or rpc_id in self._recent_rpc_ids:
            return False
        self._inflight_rpc_ids.add(rpc_id)
        return True

    def _complete_rpc(self, rpc_id: str) -> None:
        self._inflight_rpc_ids.discard(rpc_id)
        if rpc_id in self._recent_rpc_ids:
            return
        if len(self._recent_rpc_order) >= _SEEN_RPC_CAP:
            oldest = self._recent_rpc_order.popleft()
            self._recent_rpc_ids.discard(oldest)
        self._recent_rpc_order.append(rpc_id)
        self._recent_rpc_ids.add(rpc_id)

    async def _dispatch(
        self,
        op: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if op == "list":
            stores = await asyncio.to_thread(_load_stores_payload)
            return _ok(stores)
        if op == "search":
            query = payload["query"]
            limit = _coerce_search_limit(
                payload.get("limit", _DEFAULT_SEARCH_LIMIT)
            )
            raw_owner_user_id = payload.get("ownerUserId", "")
            owner_user_id = (
                raw_owner_user_id if isinstance(raw_owner_user_id, str) else ""
            )
            try:
                search_payload = await asyncio.to_thread(
                    _search_payload,
                    query,
                    limit,
                    owner_user_id,
                )
            except _MachineryUnavailable:
                raise
            except Exception:
                log.exception("memory_bridge search failed")
                return _error(
                    "search_failed",
                    "search failed on the agent host",
                )
            return _ok(search_payload)

        async with self._mutation_lock:
            store = await asyncio.to_thread(_load_store)
            target = payload["target"]
            if op == "add":
                write_result = await asyncio.to_thread(
                    store.add,
                    target,
                    payload["content"],
                )
            elif op == "replace":
                write_result = await asyncio.to_thread(
                    store.replace,
                    target,
                    payload["oldText"],
                    payload["newContent"],
                )
            else:
                write_result = await asyncio.to_thread(
                    store.remove,
                    target,
                    payload["oldText"],
                )

            if not isinstance(write_result, dict):
                return _error("write_failed", "memory write failed")
            if write_result.get("success") is not True:
                return _write_error(write_result)
            stores = await asyncio.to_thread(_load_stores_payload)
            return _ok(stores)

    async def _post_result(self, rpc_id: str, body: dict[str, Any]) -> None:
        path = f"{_RPC_ROOT}/{rpc_id}/result"
        for attempt in range(2):
            try:
                await self._post(path, body)
                return
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if 500 <= status_code < 600 and attempt == 0:
                    log.warning(
                        "memory_bridge result server error, retrying rpc=%s status=%s",
                        rpc_id,
                        status_code,
                    )
                    continue
                if 500 <= status_code < 600:
                    log.exception(
                        "memory_bridge result post failed after retry rpc=%s status=%s",
                        rpc_id,
                        status_code,
                    )
                else:
                    log.exception(
                        "memory_bridge result post rejected rpc=%s status=%s",
                        rpc_id,
                        status_code,
                    )
                return
            except (httpx.TransportError, ConnectionError):
                if attempt == 0:
                    log.warning(
                        "memory_bridge result connection failed, retrying rpc=%s",
                        rpc_id,
                    )
                    continue
                log.exception("memory_bridge result post failed after retry rpc=%s", rpc_id)
                return
            except Exception:
                log.exception("memory_bridge result post failed rpc=%s", rpc_id)
                return

    async def _post(self, path: str, body: dict[str, Any]) -> None:
        if not self._config.pairing_token:
            raise RuntimeError("pairing token required for memory RPC endpoint")
        headers = {
            "Content-Type": "application/json",
            "X-BGOS-Pairing": self._config.pairing_token,
        }
        async with httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=_HTTP_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(path, json=body, headers=headers)
            response.raise_for_status()
