"""Tests for hermes_channel_bgos.bgos_api — the async BGOS REST client."""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_api import BgosApi, BgosApiError, NOT_MODIFIED
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


async def test_pair_exchange_no_pairing_header(mock_bgos_server):
    """Pair-exchange is the pre-auth endpoint: X-BGOS-Pairing must NOT be sent."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token=None))
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "pair_xyz", "pairing_id": 42},
    )

    resp = await api.pair_exchange(
        code="BGOS-ABCD-EF", device_label="hades-box", integration="hermes",
    )

    assert resp == {"pairing_token": "pair_xyz", "pairing_id": 42}
    req = mock_bgos_server.last_request("POST", "/api/v1/integrations/pair-exchange")
    assert "X-BGOS-Pairing" not in req.headers
    # Backend PairExchangeDto is camelCase — we translate at the wire.
    # The Python surface stays snake_case for idiomatic use.
    assert req.json_body == {
        "code": "BGOS-ABCD-EF",
        "deviceLabel": "hades-box",
        "integration": "hermes",
        "agentCatalog": [],
    }
    await api.close()


async def test_post_message_sends_pairing_header(mock_bgos_server):
    """Wire format matches backend CreateMessageDto: camelCase chatId + messageType,
    `sender` is lowercase 'assistant'."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 101})

    resp = await api.post_message(
        chat_id=7, text="hi", sender="assistant", message_type="standard",
    )

    assert resp == {"id": 101}
    req = mock_bgos_server.last_request("POST", "/api/v1/messages")
    assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
    assert req.json_body["chatId"] == 7
    assert req.json_body["text"] == "hi"
    assert req.json_body["sender"] == "assistant"
    assert req.json_body["messageType"] == "standard"
    # Optional fields absent when not provided
    assert "files" not in req.json_body
    assert "options" not in req.json_body
    assert "approvalMeta" not in req.json_body
    await api.close()


async def test_post_message_defaults_sender_to_assistant(mock_bgos_server):
    """`sender` defaults to 'assistant' — adapter-side `send()` doesn't have
    to pass it explicitly."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 110})

    await api.post_message(chat_id=7, text="hi")

    req = mock_bgos_server.last_request("POST", "/api/v1/messages")
    assert req.json_body["sender"] == "assistant"
    assert req.json_body["messageType"] == "standard"
    await api.close()


async def test_post_message_full_payload(mock_bgos_server):
    """Optional fields (files, options, approvalMeta) flow through with the
    backend-expected key names."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 200})

    await api.post_message(
        chat_id=7,
        text="Proceed?",
        sender="assistant",
        message_type="approval_request",
        files=[{"fileName": "x.png", "s3Key": "k/1", "fileMimeType": "image/png", "size": 1024}],
        options=[{"text": "Yes", "callbackData": "ea:once:1", "style": "success"}],
        approval_meta={"command": "rm -rf", "session_key": "s1", "approval_id": 1},
    )

    req = mock_bgos_server.last_request("POST", "/api/v1/messages")
    body = req.json_body
    assert body["files"][0]["s3Key"] == "k/1"
    assert body["options"][0]["callbackData"] == "ea:once:1"
    # approvalMeta is camelCase on the wire; its contents keep their DB shape
    # (snake_case), since they're stored as JSONB and the adapter controls them.
    assert body["approvalMeta"]["session_key"] == "s1"
    await api.close()


async def test_patch_message_sends_changes(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("PATCH", "/api/v1/messages/42").respond(200, {"id": 42})

    await api.patch_message(42, text="updated text")

    req = mock_bgos_server.last_request("PATCH", "/api/v1/messages/42")
    assert req.json_body == {"text": "updated text"}
    await api.close()


async def test_delete_message_sends_delete_request(mock_bgos_server):
    """DELETE /api/v1/messages/{id} carries the pairing header and uses
    the DELETE verb. Backend may return 204 No Content — _request handles
    empty bodies and returns None."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("DELETE", "/api/v1/messages/42").respond(204)

    result = await api.delete_message(42)

    assert result is None
    req = mock_bgos_server.last_request("DELETE", "/api/v1/messages/42")
    assert req.method == "DELETE"
    assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
    await api.close()


async def test_delete_message_raises_on_404(mock_bgos_server):
    """A 404 (already-deleted or never-existed) bubbles up as BgosApiError
    so callers can decide whether to swallow. Adapter-side delete_message
    swallows 404; lower-level callers may not want to."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("DELETE", "/api/v1/messages/99").respond(
        404, {"error": "MESSAGE_NOT_FOUND"},
    )

    with pytest.raises(BgosApiError) as excinfo:
        await api.delete_message(99)
    assert excinfo.value.status == 404
    await api.close()


async def test_fetch_inbound_since_passes_cursor(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on(
        "GET", "/api/v1/integrations/inbound",
    ).respond(200, {"messages": []})

    await api.fetch_inbound_since(1234)

    req = mock_bgos_server.last_request("GET", "/api/v1/integrations/inbound")
    assert req.query["since_message_id"] == "1234"
    assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
    await api.close()


async def test_fetch_inbound_no_etag_behaves_like_before(mock_bgos_server):
    """Egress fix backward-compat: when the backend omits ETag (Stage-2 and
    older), the conditional GET still returns the full body and never sends
    If-None-Match — identical to the prior behavior."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {"messages": [{"id": 5}]},
    )

    resp = await api.fetch_inbound_since(1)
    assert resp == {"messages": [{"id": 5}]}
    req = mock_bgos_server.last_request("GET", "/api/v1/integrations/inbound")
    assert "If-None-Match" not in req.headers
    await api.close()


async def test_fetch_inbound_sends_if_none_match_after_etag(mock_bgos_server):
    """When the backend returns an ETag, the NEXT poll for the same cursor
    echoes it back via If-None-Match (egress fix Stage-3 conditional GET)."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {"messages": []}, headers={"ETag": '"abc123"'},
    )

    # First call: no prior validator, records the ETag.
    first = await api.fetch_inbound_since(42)
    assert first == {"messages": []}
    req1 = mock_bgos_server.last_request("GET", "/api/v1/integrations/inbound")
    assert "If-None-Match" not in req1.headers

    # Second call, same cursor: must replay the validator.
    await api.fetch_inbound_since(42)
    req2 = mock_bgos_server.last_request("GET", "/api/v1/integrations/inbound")
    assert req2.headers["If-None-Match"] == '"abc123"'
    await api.close()


async def test_fetch_inbound_304_returns_not_modified(mock_bgos_server):
    """A 304 surfaces as the NOT_MODIFIED sentinel so the poll loop can skip
    work entirely (the egress win)."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        304, headers={"ETag": '"abc123"'},
    )

    resp = await api.fetch_inbound_since(42)
    assert resp is NOT_MODIFIED
    await api.close()


async def test_whoami_returns_pairing_context(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    resp = await api.whoami()

    assert resp["pairing_id"] == 42
    assert resp["assistants"][0]["agent_route"] == "hades"
    await api.close()


async def test_401_raises_pairing_revoked_error(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_dead"))
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        401, {"error": "PAIRING_REVOKED"},
    )

    with pytest.raises(BgosApiError) as excinfo:
        await api.whoami()

    assert excinfo.value.status == 401
    assert excinfo.value.code == "PAIRING_REVOKED"
    await api.close()


async def test_put_commands_sends_manifest(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on(
        "PUT", "/api/v1/integrations/assistants/7/commands",
    ).respond(200, {})

    await api.put_commands(
        assistant_id=7,
        commands=[{"command": "help", "description": "Show help", "scope": "all"}],
    )

    req = mock_bgos_server.last_request("PUT", "/api/v1/integrations/assistants/7/commands")
    assert req.json_body == {
        "commands": [{"command": "help", "description": "Show help", "scope": "all"}],
    }
    await api.close()


async def test_push_agent_catalog(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/42/agent-catalog",
    ).respond(200, {})

    entries = [{"agent_route": "hades", "name": "Hades", "description": "Ops agent"}]
    await api.push_agent_catalog(pairing_id=42, entries=entries)

    req = mock_bgos_server.last_request("POST", "/api/v1/integrations/pairings/42/agent-catalog")
    # Backend AgentCatalogPushDto uses `agents` as the array field.
    assert req.json_body == {"agents": entries}
    await api.close()


async def test_create_upload_url_returns_presigned(mock_bgos_server):
    """The route is `/api/v1/files/upload-url` (FileController) — NOT under
    `/integrations/` — and speaks camelCase `{fileName, contentType, size}` in,
    `{uploadUrl, key}` out. `create_upload_url` sends the right keys and
    normalizes the response to the snake_case `{upload_url, s3_key}` shape its
    callers consume. The old contract 404'd, breaking every ≥500 KB media send."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on(
        "POST", "/api/v1/files/upload-url",
    ).respond(200, {"uploadUrl": "https://s3/..", "key": "k/1"})

    resp = await api.create_upload_url(filename="big.png", mime="image/png", size=1024000)

    assert resp["s3_key"] == "k/1"
    assert resp["upload_url"] == "https://s3/.."
    req = mock_bgos_server.last_request("POST", "/api/v1/files/upload-url")
    assert req.json_body == {
        "fileName": "big.png", "contentType": "image/png", "size": 1024000,
    }
    await api.close()


async def test_500_with_non_json_body_still_raises(mock_bgos_server):
    """Backend crash path: 500 with text body (e.g. stack trace). Should raise BgosApiError
    without crashing on JSON parse."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(500, text="Internal Server Error")

    with pytest.raises(BgosApiError) as excinfo:
        await api.post_message(chat_id=1, text="x", sender="assistant")
    assert excinfo.value.status == 500
    await api.close()


async def test_require_pairing_token_when_missing(mock_bgos_server):
    """Calling an authenticated endpoint without a pairing token should fail fast."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token=None))

    with pytest.raises(RuntimeError, match="pairing token"):
        await api.whoami()
    await api.close()


async def test_list_peers_uses_caller_assistant_header(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("GET", "/api/v1/peers").respond(
        200, [{"assistantId": 894, "introduced": True}],
    )

    resp = await api.list_peers(caller_assistant_id=885)

    assert resp == [{"assistantId": 894, "introduced": True}]
    req = mock_bgos_server.last_request("GET", "/api/v1/peers")
    assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
    assert req.headers["X-Caller-Assistant-Id"] == "885"
    await api.close()


async def test_send_peer_clamps_timeout_and_records_blocking_reply(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/peers/894/send").respond(
        200,
        {
            "status": "sent",
            "conversationId": 73,
            "sideThreadChatId": 953,
            "messageId": 8868,
            "reply": {"messageId": 8870, "text": "hello", "fromAssistantId": 894},
        },
    )
    recorded: list[dict] = []

    resp = await api.send_peer(
        caller_assistant_id=885,
        target_assistant_id=894,
        text="hello Athena",
        parent_message_id=8800,
        wait_for_reply=True,
        timeout_seconds=85,
        turn_state="expecting_reply",
        on_wait_reply_consumed=recorded.append,
    )

    assert resp["reply"]["messageId"] == 8870
    req = mock_bgos_server.last_request("POST", "/api/v1/peers/894/send")
    assert req.headers["X-Caller-Assistant-Id"] == "885"
    assert req.json_body == {
        "text": "hello Athena",
        "parentMessageId": 8800,
        "waitForReply": True,
        "timeoutSeconds": 50,
        "turnState": "expecting_reply",
    }
    assert recorded == [{
        "pending": True,
        "callerAssistantId": 885,
        "targetAssistantId": 894,
        "parentMessageId": 8800,
    }, {
        "conversationId": 73,
        "sideThreadChatId": 953,
        "sentMessageId": 8868,
        "returnedReplyMessageId": 8870,
        "targetAssistantId": 894,
    }]
    await api.close()


async def test_send_peer_surfaces_requires_introduction_without_auto_intro(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/peers/894/send").respond(
        200,
        {"status": "requires_introduction", "conversationId": None},
    )

    resp = await api.send_peer(
        caller_assistant_id=885,
        target_assistant_id=894,
        text="hello Athena",
        parent_message_id=8800,
        wait_for_reply=True,
    )

    assert resp["status"] == "requires_introduction"
    assert all("introductions" not in req.path for req in mock_bgos_server.requests)
    await api.close()
