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

    def _headers(
        self,
        *,
        require_pairing: bool = True,
        caller_assistant_id: int | None = None,
    ) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.pairing_token:
            headers["X-BGOS-Pairing"] = self._config.pairing_token
        elif require_pairing:
            raise RuntimeError(
                "pairing token required for this endpoint but not configured"
            )
        if caller_assistant_id is not None:
            # Required for the peer (a2a) endpoints. The backend uses this to
            # enforce the introduction matrix and stamp the inbound message
            # with the originator. See bgos-agent-capabilities.md §11.
            headers["X-Caller-Assistant-Id"] = str(caller_assistant_id)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        require_pairing: bool = True,
        caller_assistant_id: int | None = None,
    ) -> Any:
        resp = await self._client.request(
            method,
            path,
            json=json,
            params=params,
            headers=self._headers(
                require_pairing=require_pairing,
                caller_assistant_id=caller_assistant_id,
            ),
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
    ) -> dict:
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if approval_meta is not None:
            body["approvalMeta"] = approval_meta
        return await self._request("PATCH", f"/api/v1/messages/{message_id}", json=body)

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

    # -------------------------------------------------------------------------
    # Peer (agent-to-agent) endpoints — see bgos-agent-capabilities.md §11
    # -------------------------------------------------------------------------
    #
    # Every peer call carries `X-Caller-Assistant-Id: <calling assistant id>`
    # IN ADDITION to the standard `X-BGOS-Pairing` header. The backend uses
    # the caller id to enforce the introduction matrix and stamp the inbound
    # peer message with the originator's identity.

    async def list_peers(self, *, caller_assistant_id: int) -> list[dict]:
        """GET /api/v1/peers — discovery.

        Returns a list of `{assistantId, name, avatarUrl, color, introduced,
        expiresAt}` for every other assistant on the user's account. The
        `introduced` flag is True ONLY if the user has enabled this direction
        in the BGOS Agent Permissions matrix; if False, `send_to_peer` will
        return `status='requires_introduction'`.
        """
        result = await self._request(
            "GET",
            "/api/v1/peers",
            caller_assistant_id=caller_assistant_id,
        )
        if isinstance(result, list):
            return result
        # Some backend versions wrap the list in `{peers: [...]}`.
        if isinstance(result, dict) and isinstance(result.get("peers"), list):
            return result["peers"]
        return []

    async def peer_status(
        self, *, caller_assistant_id: int, peer_assistant_id: int,
    ) -> dict:
        """GET /api/v1/peers/:id/status — presence + open-conversation state.

        Returns `{online, lastSeenAt, hasOpenConversation, conversationId,
        turnHolderId}`. Use BEFORE `send_to_peer` to know if the peer is
        actively connected (immediate delivery) vs. offline (next-reconnect
        pickup).
        """
        return await self._request(
            "GET",
            f"/api/v1/peers/{peer_assistant_id}/status",
            caller_assistant_id=caller_assistant_id,
        )

    async def send_to_peer(
        self,
        *,
        caller_assistant_id: int,
        target_assistant_id: int,
        text: str,
        parent_message_id: int,
        wait_for_reply: bool = False,
        timeout_seconds: int | None = None,
        turn_state: str | None = None,
    ) -> dict:
        """POST /api/v1/peers/:targetAssistantId/send — send to peer.

        `parent_message_id` must reference one of YOUR previous reply messages
        in the user's chat — it anchors the SideConversationCard to that
        message visually. Pattern: send a "Looping in <peer>..." reply first,
        capture its message id, then call this with that id as parent.

        `wait_for_reply=True` blocks until the peer replies (their reply must
        carry `replyToId` set to the message id we returned). Server cap is
        85s. **Do NOT retry on timeout** — the message is already saved.
        Either drop wait_for_reply and poll the side-thread later, or accept
        the timeout.

        `turn_state` lifecycle hint:
        - `expecting_reply` (default) — yields the turn to the peer.
        - `more_coming` — keeps the turn for back-to-back updates.
        - `final` — closes the conversation server-side.

        Returns `{status, sideThreadChatId, messageId, conversationId,
        turnState, reply?}`. `status` is either `'sent'` or
        `'requires_introduction'` (returned 200 so the caller can degrade
        gracefully).
        """
        body: dict[str, Any] = {
            "text": text,
            "parentMessageId": parent_message_id,
            "waitForReply": wait_for_reply,
        }
        if timeout_seconds is not None:
            body["timeoutSeconds"] = timeout_seconds
        if turn_state is not None:
            body["turnState"] = turn_state
        return await self._request(
            "POST",
            f"/api/v1/peers/{target_assistant_id}/send",
            json=body,
            caller_assistant_id=caller_assistant_id,
        )

    async def complete_peer_thread(
        self,
        *,
        caller_assistant_id: int,
        peer_assistant_id: int,
        summary: str | None = None,
    ) -> dict:
        """POST /api/v1/peers/conversations/close — close the active conversation
        between caller and peer. The optional `summary` becomes the
        collapsed-state caption on the SideConversationCard. Strongly
        recommended; without it the UI shows a generic "Conversation
        completed" line.
        """
        body: dict[str, Any] = {"peerAssistantId": peer_assistant_id}
        if summary is not None and summary.strip():
            body["summary"] = summary.strip()
        return await self._request(
            "POST",
            "/api/v1/peers/conversations/close",
            json=body,
            caller_assistant_id=caller_assistant_id,
        )

    async def complete_side_thread(
        self,
        *,
        caller_assistant_id: int,
        parent_message_id: int,
        summary: str,
    ) -> dict:
        """POST /api/v1/peers/threads/:parentMessageId/complete — flip the
        SideConversationCard from live to completed-collapsed with the given
        one-line summary. Only the agent that initiated the side-thread
        (i.e., the chat owner) may call this.
        """
        return await self._request(
            "POST",
            f"/api/v1/peers/threads/{parent_message_id}/complete",
            json={"summary": summary},
            caller_assistant_id=caller_assistant_id,
        )

    async def get_side_thread(
        self,
        *,
        caller_assistant_id: int,
        parent_message_id: int,
    ) -> dict:
        """GET /api/v1/peers/threads/:parentMessageId — fetch a side-thread.

        Used as a fallback when `send_to_peer` with `wait_for_reply` times out
        — poll this endpoint and look for a message whose `replyToId`
        matches the message id you sent.
        """
        return await self._request(
            "GET",
            f"/api/v1/peers/threads/{parent_message_id}",
            caller_assistant_id=caller_assistant_id,
        )

    async def get_peer_inbox(self, *, caller_assistant_id: int) -> dict:
        """GET /api/v1/peers/inbox — list ALL chats (main + a2a side-threads)
        where this assistant is the recipient. Use this for plugin-side
        chat discovery so a2a chats don't get dropped (the main
        `/integrations/inbound` endpoint omits them so they don't pollute
        the user's sidebar).
        """
        return await self._request(
            "GET",
            "/api/v1/peers/inbox",
            caller_assistant_id=caller_assistant_id,
        )
