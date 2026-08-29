"""Async HTTP client for the BGOS backend's integration endpoints.

Every authenticated call sends `X-BGOS-Pairing: <raw token>`. `pair_exchange`
is the one pre-auth endpoint (used to exchange a pair code for that token).

Route coverage matches what the adapter + pair CLI need across Phase 1 tasks 2–13:
pair-exchange, whoami, post/patch messages, inbound backfill, PUT commands,
push agent catalog, create upload URL, plus peer discovery/status/send helpers
for one-shot a2a smoke tests.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import __version__
from .config import BgosConfig

log = logging.getLogger(__name__)


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


# Sentinel returned by conditional GETs when the server replies 304 Not
# Modified. Distinct from `None` (which `_request` returns for an empty 2xx
# body) so callers can tell "nothing changed, skip work" apart from "ok, no
# content". Egress fix (Stage 4): conditional-GET on the inbound poll lets the
# Stage-3 backend answer the tight poll loop with a 0-byte 304 instead of
# re-serializing the message list every few seconds.
NOT_MODIFIED = object()

# Sentinel for "caller did not supply this field" on endpoints where an
# explicit JSON null is itself meaningful (heartbeat latestKnownVersion:
# null clears a stale persisted value server-side, absent leaves it alone).
UNSET: Any = object()


class BgosApi:
    def __init__(self, config: BgosConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
        )
        # Last ETag seen per conditional-GET path, so the next poll can send
        # `If-None-Match`. In-process only — on restart we simply re-fetch the
        # full payload once (the backend still returns 200 + body for a missing
        # / non-matching ETag), so losing this is harmless and backward-safe.
        self._etag_by_path: dict[str, str] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BgosApi":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _headers(self, *, require_pairing: bool = True, assistant_id: int | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.pairing_token:
            headers["X-BGOS-Pairing"] = self._config.pairing_token
        elif require_pairing:
            raise RuntimeError(
                "pairing token required for this endpoint but not configured"
            )
        if assistant_id is not None:
            headers["X-Caller-Assistant-Id"] = str(assistant_id)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        require_pairing: bool = True,
        assistant_id: int | None = None,
    ) -> Any:
        # Diagnostic visibility (enabled via BGOS_DEBUG=1): one DEBUG line
        # per outbound HTTP call so operators can correlate streaming
        # behavior with adapter wire activity. Body length only — never
        # the body itself, which can contain user-visible content.
        # Caught live 2026-05-13 when the streaming-preview cleanup path
        # produced duplicate visible messages on BGOS and there was no
        # way to tell from logs which adapter requests had fired.
        if log.isEnabledFor(logging.DEBUG):
            body_len = -1
            if isinstance(json, dict):
                try:
                    import json as _json
                    body_len = len(_json.dumps(json))
                except Exception:
                    body_len = -1
            log.debug(
                "bgos_api.request method=%s path=%s body_len=%d",
                method, path, body_len,
            )
        resp = await self._client.request(
            method,
            path,
            json=json,
            params=params,
            headers=self._headers(require_pairing=require_pairing, assistant_id=assistant_id),
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "bgos_api.response method=%s path=%s status=%d resp_len=%d",
                method, path, resp.status_code, len(resp.content or b""),
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
            # Diagnostic visibility for the 4xx path specifically — caught
            # live 2026-05-13 when streaming PATCH returned 400 and we had
            # no idea what payload field the backend was rejecting. Logs
            # the backend's error body (truncated 500 chars) and the
            # request body's top-level KEYS only (values omitted so user-
            # visible content stays out of logs).
            if log.isEnabledFor(logging.DEBUG):
                req_keys = (
                    sorted(json.keys()) if isinstance(json, dict) else None
                )
                log.debug(
                    "bgos_api.error method=%s path=%s status=%d "
                    "code=%s req_keys=%s body=%s",
                    method, path, resp.status_code, code, req_keys,
                    str(body)[:500],
                )
            raise BgosApiError(resp.status_code, code, body)
        if not resp.content:
            return None
        return resp.json()

    async def _conditional_get(
        self,
        path: str,
        *,
        params: Any = None,
        cache_key: str | None = None,
    ) -> Any:
        """GET `path` with `If-None-Match` when we hold an ETag for it.

        Returns the parsed JSON body on a 200 (and records the response's
        ETag for next time). Returns the `NOT_MODIFIED` sentinel on a 304 so
        the caller can skip work entirely. Falls back to plain behavior when
        the server omits ETags — i.e. an old/Stage-2 backend that never sends
        `ETag` simply keeps answering 200 + full body, exactly as before, so
        this is safe to ship ahead of the Stage-3 backend.

        `cache_key` defaults to `path`; pass an explicit key when the same path
        is polled with cursor params that shouldn't share an ETag bucket.
        """
        key = cache_key or path
        headers = self._headers()
        prev_etag = self._etag_by_path.get(key)
        if prev_etag:
            headers["If-None-Match"] = prev_etag
        resp = await self._client.request(
            "GET", path, params=params, headers=headers,
        )
        if resp.status_code == 304:
            # Server confirms nothing changed since `prev_etag`. Keep the
            # cached validator and tell the caller to do nothing.
            return NOT_MODIFIED
        if resp.status_code >= 400:
            content_type = resp.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                code = body.get("error") if isinstance(body, dict) else None
            else:
                body = resp.text
                code = None
            raise BgosApiError(resp.status_code, code, body)
        # Record the new validator (drop it if the server stopped sending one).
        etag = resp.headers.get("ETag")
        if etag:
            self._etag_by_path[key] = etag
        else:
            self._etag_by_path.pop(key, None)
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
        intended_assistant_id: int | None = None,
    ) -> dict:
        """Exchange a pair code for a pairing token. Pre-auth — no X-BGOS-Pairing header.

        BGOS backend DTOs are camelCase (`deviceLabel`, `agentCatalog`) while
        this function's Python signature is snake_case for idiomatic use. We
        translate at the wire. `integration` is a lowercase-string literal on
        both sides — matches the `integration_pairings.integration` column.

        `intended_assistant_id` pins the exchange to one assistant: the
        backend's overlap guard then resolves overlap by IDENTITY (the
        pairing serving that assistant) instead of by the shared catalog
        label. Snake_case on the wire on purpose: the field is part of the
        cross-repo exchange contract (backend PairExchangeDto), pinned
        identically by the claude plugin. Omitted entirely when None so the
        unpinned body stays byte-identical.
        """
        body: dict = {
            "code": code,
            "deviceLabel": device_label,
            "integration": integration,
            "agentCatalog": agent_catalog or [],
        }
        if intended_assistant_id is not None:
            body["intended_assistant_id"] = intended_assistant_id
        return await self._request(
            "POST",
            "/api/v1/integrations/pair-exchange",
            json=body,
            require_pairing=False,
        )

    async def whoami(self) -> dict:
        """Introspect the pairing scope: pairing_id + assistants[] + metadata."""
        return await self._request("GET", "/api/v1/integrations/me")

    async def list_pairings(self) -> list[dict]:
        """All ACTIVE pairings of the user who owns this pairing token.

        Backend: GET /api/v1/integrations/pairings resolves the user from any
        auth mode including X-BGOS-Pairing (clerk-auth.guard sets
        resolvedUserId from the pairing row), and each entry carries
        `agent_catalog`. Used by the install-time topology guard to detect a
        re-pair leftover: a second active pairing serving the same agent
        routes answers everything twice.
        """
        resp = await self._request("GET", "/api/v1/integrations/pairings")
        return resp if isinstance(resp, list) else []

    async def post_heartbeat(
        self,
        *,
        daemon_version: str,
        env: dict | None = None,
        last_error: dict | None = None,
        latest_known_version: str | None | Any = UNSET,
        update_readiness: dict | None = None,
    ) -> Any:
        """POST /api/v1/integrations/heartbeat (pairing-authed).

        Reports the running plugin version so the pairing row's
        `daemon_version` is populated — the BGOS app's update prompt keys off
        it (a NULL means "unknown, can never prompt"). Body is camelCase to
        match the backend HeartbeatDto: `{daemonVersion, env?, lastError?}`
        where `env` is an object of short strings (`{platform?, python?,
        hermes?}`, each <=64 chars) and `lastError` is an object
        `{code, message, at}` (the backend rejects bare strings here).
        Callers on the daemon cycle must treat this as best-effort and
        swallow failures — a heartbeat must never block or crash the daemon.

        One-click update extension (wire contract section 1):
        `latest_known_version` is the newest version this daemon found at
        its own pinned source; an EXPLICIT null is sent when the daily
        check failed (clears a stale persisted value; the UNSET default
        omits the key entirely), and `update_readiness` is the
        `{supervised, autoUpdateEnabled, rollbackLatched,
        pendingRestartVersion}` object from self_update.update_readiness().
        Backend-side validation is lenient (invalid semver ignored, never a
        400), so sending is always safe.
        """
        body: dict[str, Any] = {"daemonVersion": daemon_version}
        if env is not None:
            body["env"] = env
        if last_error is not None:
            body["lastError"] = last_error
        if latest_known_version is not UNSET:
            body["latestKnownVersion"] = latest_known_version
        if update_readiness is not None:
            body["updateReadiness"] = update_readiness
        return await self._request(
            "POST", "/api/v1/integrations/heartbeat", json=body,
        )

    async def get_capabilities(
        self,
        channel: str = "hermes",
        daemon_version: str = __version__,
    ) -> dict:
        """GET /api/v1/integrations/capabilities, the backend-served agent
        capability canon for this channel (capability bootstrap).

        Returns {channel, version, text, core, channelSyntax}; `text` is the
        ready-to-inject PLATFORM_HINTS body. Callers fetch this at connect and
        fall back to the bundled BGOS_PLATFORM_HINT when it is unavailable, so a
        fetch failure never hard-fails the gateway. Mirrors whoami()."""
        return await self._request(
            "GET",
            "/api/v1/integrations/capabilities",
            params={"channel": channel, "daemonVersion": daemon_version},
        )

    # -------------------------------------------------------------------------
    # Messages
    # -------------------------------------------------------------------------


    async def get_chat(self, chat_id: int) -> dict:
        """GET /api/v1/chats/{chat_id}. Returns chat metadata including assistantId and kind.

        Used by standalone sends and adapter cache warmup to route A2A replies
        through /send-message with the owning assistant id.
        """
        return await self._request("GET", f"/api/v1/chats/{chat_id}")

    async def post_send_message(
        self,
        *,
        chat_id: int,
        assistant_id: int,
        text: str,
        sender: str = "assistant",
        message_type: str = "standard",
        has_attachment: bool | None = None,
        is_audio_message: bool | None = None,
        audio_data: str | None = None,
        audio_file_name: str | None = None,
        audio_mime_type: str | None = None,
        audio_duration: float | int | None = None,
        files: list[dict] | None = None,
        options: list[dict] | None = None,
        approval_meta: dict | None = None,
        event_meta: dict | None = None,
        render_mode: str | None = None,
        reply_to_id: int | None = None,
        turn_state: str | None = None,
        session_handle: str | None = None,
    ) -> dict:
        """POST to /api/v1/send-message.

        Unlike /api/v1/messages, this endpoint runs BGOS's peer-conversation
        bridge for kind='a2a' chats, retro-tags replies with peerConversationId,
        rotates turns, and emits inbound_message to the other assistant. This is
        the path used by the working Claude Code plugin.

        `session_handle` is the opaque, server-minted `sessionHandle` carried on
        the inbound event this message replies to. When present the backend
        prioritizes it over `chatId` to resolve the target chat
        (server-authoritative addressing, 2026-05-30 hardening) — we still send
        `chatId` for the rollout window where raw ids remain accepted.
        """
        body: dict[str, Any] = {
            "chatId": chat_id,
            "assistantId": assistant_id,
            "text": text,
            "sender": sender,
            "messageType": message_type,
        }
        if session_handle:
            body["sessionHandle"] = session_handle
        if has_attachment is not None:
            body["hasAttachment"] = has_attachment
        if is_audio_message is not None:
            body["isAudioMessage"] = is_audio_message
        if audio_data is not None:
            body["audioData"] = audio_data
        if audio_file_name is not None:
            body["audioFileName"] = audio_file_name
        if audio_mime_type is not None:
            body["audioMimeType"] = audio_mime_type
        if audio_duration is not None:
            body["audioDuration"] = audio_duration
        if files:
            body["files"] = files
        if options:
            body["options"] = options
        if approval_meta is not None:
            body["approvalMeta"] = approval_meta
        if event_meta is not None:
            body["eventMeta"] = event_meta
        if render_mode is not None:
            body["renderMode"] = render_mode
        if reply_to_id is not None:
            body["replyToId"] = reply_to_id
        if turn_state is not None:
            body["turnState"] = turn_state
        return await self._request("POST", "/api/v1/send-message", json=body)

    async def peer_status(self, *, caller_assistant_id: int, peer_assistant_id: int) -> dict:
        return await self._request(
            "GET", f"/api/v1/peers/{peer_assistant_id}/status",
            assistant_id=caller_assistant_id,
        )

    async def list_peers(self, *, caller_assistant_id: int) -> list[dict]:
        """GET /api/v1/peers with X-Caller-Assistant-Id.

        The returned rows include `introduced`. Callers must not attempt to
        create introductions themselves when it is false; user consent lives in
        BGOS Agent Permissions.
        """
        resp = await self._request(
            "GET", "/api/v1/peers", assistant_id=caller_assistant_id,
        )
        return resp if isinstance(resp, list) else []

    async def send_peer(
        self,
        *,
        caller_assistant_id: int,
        target_assistant_id: int,
        text: str,
        parent_message_id: int,
        wait_for_reply: bool = False,
        timeout_seconds: int | None = None,
        turn_state: str | None = None,
        on_wait_reply_consumed: Any | None = None,
    ) -> dict:
        """POST /api/v1/peers/{targetAssistantId}/send.

        `parent_message_id` is required by BGOS and should point to one of the
        caller agent's visible messages in the parent chat. `timeout_seconds`
        is client-side clamped to the backend's current max of 50. If
        `wait_for_reply=True` returns a reply, `on_wait_reply_consumed` is
        called with the IDs needed to suppress the same reply if it later
        arrives over WS or REST reconciliation.
        """
        body: dict[str, Any] = {
            "text": text,
            "parentMessageId": parent_message_id,
            "waitForReply": bool(wait_for_reply),
        }
        if timeout_seconds is not None:
            body["timeoutSeconds"] = max(1, min(int(timeout_seconds), 50))
        if turn_state is not None:
            body["turnState"] = turn_state
        if wait_for_reply and on_wait_reply_consumed is not None:
            on_wait_reply_consumed({
                "pending": True,
                "callerAssistantId": caller_assistant_id,
                "targetAssistantId": target_assistant_id,
                "parentMessageId": parent_message_id,
            })
        resp = await self._request(
            "POST", f"/api/v1/peers/{target_assistant_id}/send",
            json=body, assistant_id=caller_assistant_id,
        )
        if (
            wait_for_reply
            and isinstance(resp, dict)
            and resp.get("status") == "sent"
            and isinstance(resp.get("reply"), dict)
            and resp["reply"].get("messageId") is not None
            and on_wait_reply_consumed is not None
        ):
            on_wait_reply_consumed({
                "conversationId": resp.get("conversationId"),
                "sideThreadChatId": resp.get("sideThreadChatId"),
                "sentMessageId": resp.get("messageId"),
                "returnedReplyMessageId": resp["reply"].get("messageId"),
                "targetAssistantId": target_assistant_id,
            })
        return resp

    async def close_peer_conversation(
        self, *, caller_assistant_id: int, peer_assistant_id: int, summary: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"peerAssistantId": peer_assistant_id}
        if summary:
            body["summary"] = summary
        return await self._request(
            "POST", "/api/v1/peers/conversations/close",
            json=body, assistant_id=caller_assistant_id,
        )

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
        event_meta: dict | None = None,
        render_mode: str | None = None,
        reply_to_id: int | None = None,
        tool_progress: dict | None = None,
        session_handle: str | None = None,
    ) -> dict:
        """POST to /api/v1/messages. Wire format matches backend CreateMessageDto
        (camelCase: chatId, messageType, approvalMeta, eventMeta, renderMode,
        replyToId).
        `sender` is `"assistant"` (lowercase — matches enum) or `"user"`.

        `options` entries should be shaped {text, callbackData, style?} —
        these are passed through as-is to match backend CreateMessageOptionDto.
        `files` entries should be shaped
        {fileName, fileMimeType, fileData? | s3Key?, size?}. `render_mode` is
        `"inline"` (default when options present) or `"modal"`. `reply_to_id`
        tags this message as a reply to a prior message — required in
        agent-to-agent (a2a) side-thread chats so the originator's
        pollForReply() can correlate this assistant message with the inbound
        peer message that prompted it. `session_handle` is the opaque,
        server-minted `sessionHandle` from the inbound event being replied to;
        the backend prioritizes it over `chatId` for chat resolution
        (server-authoritative addressing, 2026-05-30 hardening). Fields not in
        the backend DTO are silently dropped by the whitelist — we still send
        them for forward compatibility when the backend extends.
        """
        body: dict[str, Any] = {
            "chatId": chat_id,
            "text": text,
            "sender": sender,
            "messageType": message_type,
        }
        if session_handle:
            body["sessionHandle"] = session_handle
        if files:
            body["files"] = files
        if options:
            body["options"] = options
        if approval_meta is not None:
            body["approvalMeta"] = approval_meta
        if event_meta is not None:
            body["eventMeta"] = event_meta
        if render_mode is not None:
            body["renderMode"] = render_mode
        if reply_to_id is not None:
            body["replyToId"] = reply_to_id
        if tool_progress is not None:
            body["toolProgress"] = tool_progress
        return await self._request("POST", "/api/v1/messages", json=body)

    async def patch_message(
        self,
        message_id: int,
        *,
        text: str | None = None,
        approval_meta: dict | None = None,
        event_meta: dict | None = None,
        options: list[dict] | None = None,
        render_mode: str | None = None,
        user_id: str | None = None,
        tool_progress: dict | None = None,
    ) -> dict:
        """PATCH /api/v1/messages/{id}. Mutates only the fields the caller
        supplies — backend's UpdateMessageDto whitelists each. Sending
        `options=[]` is meaningful (clears any prior keyboard) and is
        distinct from omitting the field; the adapter's edit_message uses
        that semantic for streaming edits that drop the inline chips.
        `event_meta` edits drive the voice_setup progress card.

        `user_id` is REQUIRED by the backend's DTO validation (caught
        live 2026-05-13: PATCH without it returns 400 with messages
        ['userId should not be empty', 'userId must be a string']).
        Callers should pass the Clerk user id this edit is attributed
        to — for streaming/tool-progress that's the user who sent the
        original prompt (tracked per-chat via
        StateStore.last_user_id_by_chat); for callback in-place edits
        it's the user who clicked the button (from the callback
        payload). Omitting it will fail the PATCH and the adapter's
        edit_message override will return SendResult(success=False)
        so the gateway falls back to a fresh send.
        """
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if approval_meta is not None:
            body["approvalMeta"] = approval_meta
        if event_meta is not None:
            body["eventMeta"] = event_meta
        if options is not None:
            body["options"] = options
        if render_mode is not None:
            body["renderMode"] = render_mode
        if user_id is not None:
            body["userId"] = user_id
        if tool_progress is not None:
            body["toolProgress"] = tool_progress
        return await self._request("PATCH", f"/api/v1/messages/{message_id}", json=body)

    async def delete_message(self, message_id: int) -> None:
        """DELETE /api/v1/messages/{id}. Raises BgosApiError on 4xx/5xx
        (including 404 — callers decide whether to swallow). The adapter's
        delete_message override DOES swallow 404/501 and returns False so
        the gateway can fall back to leaving the message in place."""
        await self._request("DELETE", f"/api/v1/messages/{message_id}")

    # -------------------------------------------------------------------------
    # Derived missions
    # -------------------------------------------------------------------------

    async def create_mission(
        self,
        *,
        assistant_id: int,
        title: str,
        origin: str = "derived",
        mini_goals: list[dict] | None = None,
        progress: dict | None = None,
        effort: dict | None = None,
        first_feed_text: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"title": title, "origin": origin}
        if mini_goals is not None:
            body["miniGoals"] = mini_goals
        if progress is not None:
            body["progress"] = progress
        if effort is not None:
            body["effort"] = effort
        if first_feed_text is not None:
            body["firstFeedText"] = first_feed_text
        return await self._request(
            "POST",
            f"/api/v1/integrations/assistants/{assistant_id}/missions",
            json=body,
        )

    async def get_active_mission(self, *, assistant_id: int) -> dict:
        return await self._request(
            "GET",
            f"/api/v1/integrations/assistants/{assistant_id}/missions/active",
        )

    async def patch_mission_progress(
        self,
        *,
        assistant_id: int,
        mission_id: int | str,
        progress: dict | None = None,
        effort: dict | None = None,
        feed_entry: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if progress is not None:
            body["progress"] = progress
        if effort is not None:
            body["effort"] = effort
        if feed_entry is not None:
            body["feedEntry"] = feed_entry
        return await self._request(
            "PATCH",
            f"/api/v1/integrations/assistants/{assistant_id}/missions/"
            f"{mission_id}/progress",
            json=body,
        )

    async def pause_mission(
        self,
        *,
        assistant_id: int,
        mission_id: int | str,
        reason: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if reason is not None:
            body["reason"] = reason
        return await self._request(
            "PATCH",
            f"/api/v1/integrations/assistants/{assistant_id}/missions/"
            f"{mission_id}/pause",
            json=body,
        )

    async def resume_mission(
        self,
        *,
        assistant_id: int,
        mission_id: int | str,
    ) -> dict:
        return await self._request(
            "PATCH",
            f"/api/v1/integrations/assistants/{assistant_id}/missions/"
            f"{mission_id}/resume",
            json={},
        )

    async def complete_mission(
        self,
        *,
        assistant_id: int,
        mission_id: int | str,
        summary: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        return await self._request(
            "PATCH",
            f"/api/v1/integrations/assistants/{assistant_id}/missions/"
            f"{mission_id}/complete",
            json=body,
        )

    async def fail_mission(
        self,
        *,
        assistant_id: int,
        mission_id: int | str,
        summary: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        return await self._request(
            "PATCH",
            f"/api/v1/integrations/assistants/{assistant_id}/missions/"
            f"{mission_id}/fail",
            json=body,
        )

    async def abandon_mission(
        self,
        *,
        assistant_id: int,
        mission_id: int | str,
    ) -> dict:
        return await self._request(
            "PATCH",
            f"/api/v1/integrations/assistants/{assistant_id}/missions/"
            f"{mission_id}/abandon",
            json={},
        )

    # -------------------------------------------------------------------------
    # Inbound backfill (reconnect)
    # -------------------------------------------------------------------------

    async def fetch_inbound_since(self, since_message_id: int) -> Any:
        """GET /api/v1/integrations/inbound?since_message_id=<cursor>.

        Conditional-GET aware: sends `If-None-Match` when we hold an ETag for
        this cursor and returns the `NOT_MODIFIED` sentinel on a 304 so the
        poll loop can skip the no-op replay. Against an older backend that
        doesn't emit ETags this is identical to the prior behavior (always a
        200 + full body). The ETag bucket is keyed by cursor so a validator
        minted for one cursor is never replayed against a different one.
        """
        return await self._conditional_get(
            "/api/v1/integrations/inbound",
            params={"since_message_id": since_message_id},
            cache_key=f"inbound:{since_message_id}",
        )

    # -------------------------------------------------------------------------
    # Native in-app voice (voice_rpc / voice-tasks)
    # -------------------------------------------------------------------------

    async def post_voice_rpc_ack(self, rpc_id: str) -> Any:
        """POST /api/v1/integrations/voice-rpc/{rpcId}/ack (pairing-authed).

        Cancels the backend's 1.5 s voice_rpc retry re-emit. Best-effort at
        the call site: a failed ACK just costs one duplicate frame, which
        the handler's rpcId dedupe absorbs.
        """
        return await self._request(
            "POST", f"/api/v1/integrations/voice-rpc/{rpc_id}/ack",
        )

    async def post_voice_rpc_result(self, rpc_id: str, body: dict) -> Any:
        """POST /api/v1/integrations/voice-rpc/{rpcId}/result (pairing-authed).

        `body` is `{ok:true, payload}` or `{ok:false, error:{code, message}}`
        — settles the app's held-open mint/consult request (and the
        dispatch-accept). The backend drops results arriving after its own
        deadline, hence the inner<outer cap discipline in voice_rpc.py.
        """
        return await self._request(
            "POST", f"/api/v1/integrations/voice-rpc/{rpc_id}/result", json=body,
        )

    async def post_doctor_rpc_ack(self, rpc_id: str) -> Any:
        """POST the pairing-authenticated doctor_rpc acknowledgement."""
        return await self._request(
            "POST", f"/api/v1/integrations/doctor-rpc/{rpc_id}/ack",
        )

    async def post_doctor_rpc_result(self, rpc_id: str, body: dict) -> Any:
        """POST a doctor_rpc success or failure result."""
        return await self._request(
            "POST", f"/api/v1/integrations/doctor-rpc/{rpc_id}/result", json=body,
        )

    async def post_update_rpc_ack(self, rpc_id: str) -> Any:
        """POST /api/v1/integrations/update-rpc/{rpcId}/ack (pairing-authed).

        Cancels the backend's 1.5 s update_rpc retry re-emit (10 s of no
        ack goes terminal 'unreachable' server-side). Doctor-style
        cross-check on the backend: the responding pairing must be the
        targeted one.
        """
        return await self._request(
            "POST", f"/api/v1/integrations/update-rpc/{rpc_id}/ack",
        )

    async def post_update_rpc_progress(
        self,
        rpc_id: str,
        *,
        stage: str,
        target_version: str | None = None,
        message: str | None = None,
    ) -> Any:
        """POST /api/v1/integrations/update-rpc/{rpcId}/progress.

        `stage` is one of draining|installing|restarting|staged|error
        (staged and error are daemon-terminal; restarting arms the
        backend's heartbeat-based completion detection: a comeback beat
        with daemonVersion >= targetVersion settles 'done', silence for
        5 minutes settles 'unknown').
        """
        body: dict[str, Any] = {"stage": stage}
        if target_version is not None:
            body["targetVersion"] = target_version
        if message is not None:
            body["message"] = message
        return await self._request(
            "POST",
            f"/api/v1/integrations/update-rpc/{rpc_id}/progress",
            json=body,
        )

    async def post_profile_rpc_ack(self, rpc_id: str) -> Any:
        """POST the pairing-authenticated profile_rpc (add_profile) ack."""
        return await self._request(
            "POST", f"/api/v1/integrations/profile-rpc/{rpc_id}/ack",
        )

    async def post_profile_rpc_result(self, rpc_id: str, body: dict) -> Any:
        """POST an add_profile success or failure result."""
        return await self._request(
            "POST", f"/api/v1/integrations/profile-rpc/{rpc_id}/result", json=body,
        )

    async def post_voice_task_result(self, task_id: str, body: dict) -> Any:
        """POST /api/v1/integrations/voice-tasks/{taskId}/result.

        Flips the durable voice_tasks row for a detached dispatch run and
        fans a `voice_task_update` to the user's devices. Dual-auth on the
        backend; this client uses the pairing lane.
        """
        return await self._request(
            "POST", f"/api/v1/integrations/voice-tasks/{task_id}/result", json=body,
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

    async def patch_status(
        self,
        *,
        assistant_id: int,
        status_text: str | None,
        status_emoji: str | None = None,
    ) -> None:
        """Update (or clear) the agent's status line visible under its name in
        the BGOS mobile app.

        Wire: PATCH /api/v1/integrations/assistants/{id}/status
        Body: { "statusText": str|null, "statusEmoji"?: str }

        `statusText` is always included (null clears the displayed status text;
        empty string is also treated as clear by the backend). `statusEmoji` is
        only included when the caller supplies a non-None value — omitting it
        entirely tells the backend to leave the existing emoji unchanged, whereas
        sending `"statusEmoji": null` would clear it. Body keys are camelCase to
        match the backend DTO convention (same as `post_message`'s snake→camel
        translation).
        """
        body: dict = {"statusText": status_text}
        if status_emoji is not None:
            body["statusEmoji"] = status_emoji
        await self._request(
            "PATCH",
            f"/api/v1/integrations/assistants/{assistant_id}/status",
            json=body,
        )

    async def call_owner(
        self,
        *,
        assistant_id: int,
        reason: str | None = None,
        chat_id: int | None = None,
    ) -> dict:
        """POST /api/v1/voice/outbound-call, ring the owner in the app now.

        The other three channel plugins have had a first-class call as a tool
        since 2026-07-08 (Claude Code `call_owner`, Gobot `replyHandle.callOwner`,
        OpenClaw `POST /v1/call-owner`); Hermes did not, so an agent told it
        could ring its owner had to invent a way and shelled out to curl,
        tripping its HOST's terminal approval layer instead of ringing anyone.

        Two shapes that are easy to get wrong, so they are fixed here rather
        than left to the caller:

        - `assistantId` goes in the BODY. This endpoint reads no
          X-Caller-Assistant-Id header, unlike the scheduled-task endpoints,
          so sending one changes nothing.
        - Pairing auth alone proves ownership.

        A 201 means it is ringing. A 409 with code "busy" means the owner is
        already on a call and NOTHING rang: do not retry in a loop, say so in
        chat instead. If voice is not set up the backend returns structured
        setup guidance, which the caller should relay verbatim.
        """
        body: dict = {"assistantId": assistant_id}
        if reason is not None:
            body["reason"] = reason
        if chat_id is not None:
            body["chatId"] = chat_id
        return await self._request("POST", "/api/v1/voice/outbound-call", json=body)

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
    # Agent Boards (the [[BGOS_BOARDS]] round trip's REST lane)
    # -------------------------------------------------------------------------

    async def boards_call(
        self,
        *,
        assistant_id: int,
        method: str,
        path: str,
        json: Any = None,
        params: Any = None,
    ) -> Any:
        """One call on the agent-family boards routes.

        `path` is RELATIVE to the boards root ("" for the collection, or
        "/<board>/rows/query" and friends); this method owns the prefix
        `/api/v1/integrations/assistants/{assistant_id}/boards` so the marker
        layer's RestPlan never re-derives it. Pairing auth like every other
        integration route; the PairingScopedAssistant guard on the backend
        proves the assistant belongs to this pairing.

        Raises BgosApiError on any 4xx/5xx with `.status` and `.body` intact:
        the boards denial bodies ({error, message}) are a leak-proof contract
        the adapter passes to the agent VERBATIM, so nothing here may unwrap,
        reword, or enrich them.
        """
        return await self._request(
            method,
            f"/api/v1/integrations/assistants/{assistant_id}/boards{path}",
            json=json,
            params=params,
        )

    async def put_bytes(self, url: str, data: bytes, content_type: str) -> None:
        """PUT raw bytes to an absolute (presigned S3) URL. No BGOS auth
        headers: the signature in the URL is the credential. Used by the
        boards attach flow for files above the inline threshold. Raises
        BgosApiError on a non-2xx answer."""
        async with httpx.AsyncClient(
            timeout=self._config.request_timeout_seconds,
        ) as client:
            resp = await client.put(
                url, content=data, headers={"Content-Type": content_type},
            )
        if resp.status_code >= 400:
            raise BgosApiError(resp.status_code, None, resp.text)

    # -------------------------------------------------------------------------
    # Files
    # -------------------------------------------------------------------------

    async def create_upload_url(
        self, *, filename: str, mime: str, size: int,
    ) -> dict:
        """Request a presigned S3 PUT URL for an outbound file ≥ S3_THRESHOLD.

        The backend route is `POST /api/v1/files/upload-url` (FileController),
        NOT under `/integrations/` — the earlier `/api/v1/integrations/files/
        upload-url` path 404'd, silently breaking every ≥500 KB media send
        (caught live 2026-05-31: agent's 1.2 MB PNG never delivered). The
        request DTO (`GetUploadUrlDto`) is camelCase `{fileName, contentType,
        size}` and the response (`UploadUrlResponseDto`) is `{uploadUrl, key}`
        — both diverged from the snake_case shape this client used. We send the
        correct keys and normalize the response back to the `{upload_url,
        s3_key}` shape the adapter's `_upload_and_attach` consumes, so callers
        are unaffected.
        """
        resp = await self._request(
            "POST",
            "/api/v1/files/upload-url",
            json={"fileName": filename, "contentType": mime, "size": size},
        )
        if not isinstance(resp, dict):
            raise BgosApiError(502, "bad_upload_url_response", resp)
        return {
            "upload_url": resp.get("uploadUrl"),
            "s3_key": resp.get("key"),
        }
