"""Async HTTP client for the BGOS backend's integration endpoints.

Every authenticated call sends `X-BGOS-Pairing: <raw token>`. `pair_exchange`
is the one pre-auth endpoint (used to exchange a pair code for that token).

Route coverage matches what the adapter + pair CLI need across Phase 1 tasks 2–13:
pair-exchange, whoami, post/patch messages, inbound backfill, PUT commands,
push agent catalog, create upload URL.
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import BgosConfig


class BgosApiError(Exception):
    """Raised for any non-2xx response from the BGOS backend.

    `status` is the HTTP status code. `code` is the error code from the JSON
    body when the backend follows the `{ "error": "<CODE>" }` convention
    (e.g. `PAIRING_REVOKED` on 401). `body` is the full decoded body for
    debugging — JSON if the server returned JSON, a `str` otherwise.
    """

    def __init__(self, status: int, code: str | None, body: Any) -> None:
        super().__init__(f"BGOS API {status}{' ' + code if code else ''}")
        self.status = status
        self.code = code
        self.body = body


class BgosApi:
    def __init__(self, config: BgosConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BgosApi":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _headers(self, *, require_pairing: bool = True) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.pairing_token:
            headers["X-BGOS-Pairing"] = self._config.pairing_token
        elif require_pairing:
            raise RuntimeError(
                "pairing token required for this endpoint but not configured"
            )
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        require_pairing: bool = True,
    ) -> Any:
        resp = await self._client.request(
            method,
            path,
            json=json,
            params=params,
            headers=self._headers(require_pairing=require_pairing),
        )
        if resp.status_code >= 400:
            body: Any
            code: str | None = None
            content_type = resp.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                else:
                    if isinstance(body, dict):
                        code = body.get("error")
            else:
                body = resp.text
            raise BgosApiError(resp.status_code, code, body)
        if not resp.content:
            return None
        return resp.json()

    # -------------------------------------------------------------------------
    # Pairing
    # -------------------------------------------------------------------------

    async def pair_exchange(
        self,
        *,
        code: str,
        device_label: str,
        integration: str,
        agent_catalog: list[dict] | None = None,
    ) -> dict:
        """Exchange a pair code for a pairing token. Pre-auth — no X-BGOS-Pairing header.

        BGOS backend DTOs are camelCase (`deviceLabel`, `agentCatalog`) while
        this function's Python signature is snake_case for idiomatic use. We
        translate at the wire. `integration` is a lowercase-string literal on
        both sides — matches the `integration_pairings.integration` column.
        """
        return await self._request(
            "POST",
            "/api/v1/integrations/pair-exchange",
            json={
                "code": code,
                "deviceLabel": device_label,
                "integration": integration,
                "agentCatalog": agent_catalog or [],
            },
            require_pairing=False,
        )

    async def whoami(self) -> dict:
        """Introspect the pairing scope: pairing_id + assistants[] + metadata."""
        return await self._request("GET", "/api/v1/integrations/me")

    # -------------------------------------------------------------------------
    # Messages
    # -------------------------------------------------------------------------

    async def post_message(
        self,
        *,
        chat_id: int,
        text: str,
        sender: str = "assistant",
        message_type: str = "standard",
        files: list[dict] | None = None,
        options: list[dict] | None = None,
        approval_meta: dict | None = None,
        render_mode: str | None = None,
        reply_to_id: int | None = None,
    ) -> dict:
        """POST to /api/v1/messages. Wire format matches backend CreateMessageDto
        (camelCase: chatId, messageType, approvalMeta, renderMode, replyToId).
        `sender` is `"assistant"` (lowercase — matches enum) or `"user"`.

        `options` entries should be shaped {text, callbackData, style?} —
        these are passed through as-is to match backend CreateMessageOptionDto.
        `files` entries should be shaped
        {fileName, fileMimeType, fileData? | s3Key?, size?}. `render_mode` is
        `"inline"` (default when options present) or `"modal"`. `reply_to_id`
        tags this message as a reply to a prior message — required in
        agent-to-agent (a2a) side-thread chats so the originator's
        pollForReply() can correlate this assistant message with the inbound
        peer message that prompted it. Fields not in the backend DTO are
        silently dropped by the whitelist — we still send them for forward
        compatibility when the backend extends.
        """
        body: dict[str, Any] = {
            "chatId": chat_id,
            "text": text,
            "sender": sender,
            "messageType": message_type,
        }
        if files:
            body["files"] = files
        if options:
            body["options"] = options
        if approval_meta is not None:
            body["approvalMeta"] = approval_meta
        if render_mode is not None:
            body["renderMode"] = render_mode
        if reply_to_id is not None:
            body["replyToId"] = reply_to_id
        return await self._request("POST", "/api/v1/messages", json=body)

    async def patch_message(
        self,
        message_id: int,
        *,
        text: str | None = None,
        approval_meta: dict | None = None,
        options: list[dict] | None = None,
        render_mode: str | None = None,
    ) -> dict:
        """PATCH /api/v1/messages/{id}. Mutates only the fields the caller
        supplies — backend's UpdateMessageDto whitelists each. Sending
        `options=[]` is meaningful (clears any prior keyboard) and is
        distinct from omitting the field; the adapter's edit_message uses
        that semantic for streaming edits that drop the inline chips."""
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if approval_meta is not None:
            body["approvalMeta"] = approval_meta
        if options is not None:
            body["options"] = options
        if render_mode is not None:
            body["renderMode"] = render_mode
        return await self._request("PATCH", f"/api/v1/messages/{message_id}", json=body)

    async def delete_message(self, message_id: int) -> None:
        """DELETE /api/v1/messages/{id}. Raises BgosApiError on 4xx/5xx
        (including 404 — callers decide whether to swallow). The adapter's
        delete_message override DOES swallow 404/501 and returns False so
        the gateway can fall back to leaving the message in place."""
        await self._request("DELETE", f"/api/v1/messages/{message_id}")

    # -------------------------------------------------------------------------
    # Inbound backfill (reconnect)
    # -------------------------------------------------------------------------

    async def fetch_inbound_since(self, since_message_id: int) -> dict:
        return await self._request(
            "GET",
            "/api/v1/integrations/inbound",
            params={"since_message_id": since_message_id},
        )

    # -------------------------------------------------------------------------
    # Commands + agent catalog
    # -------------------------------------------------------------------------

    async def put_commands(self, *, assistant_id: int, commands: list[dict]) -> None:
        await self._request(
            "PUT",
            f"/api/v1/integrations/assistants/{assistant_id}/commands",
            json={"commands": commands},
        )

    async def push_agent_catalog(self, *, pairing_id: int, entries: list[dict]) -> None:
        """Push (or update) the agent catalog for this pairing. Backend DTO
        uses the key `agents` (see AgentCatalogPushDto) — we translate from
        Python-side `entries` at the wire."""
        await self._request(
            "POST",
            f"/api/v1/integrations/pairings/{pairing_id}/agent-catalog",
            json={"agents": entries},
        )

    # -------------------------------------------------------------------------
    # Files
    # -------------------------------------------------------------------------

    async def create_upload_url(
        self, *, filename: str, mime: str, size: int,
    ) -> dict:
        return await self._request(
            "POST",
            "/api/v1/integrations/files/upload-url",
            json={"filename": filename, "mime": mime, "size": size},
        )
