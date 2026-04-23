"""Tests for hermes_channel_bgos.bgos_api — the async BGOS REST client."""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_api import BgosApi, BgosApiError
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
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 101})

    resp = await api.post_message(
        chat_id=7, text="hi", sender_type="ASSISTANT", message_type="standard",
    )

    assert resp == {"id": 101}
    req = mock_bgos_server.last_request("POST", "/api/v1/messages")
    assert req.headers["X-BGOS-Pairing"] == "pair_xyz"
    assert req.json_body["chatId"] == 7
    assert req.json_body["text"] == "hi"
    assert req.json_body["senderType"] == "ASSISTANT"
    assert req.json_body["message_type"] == "standard"
    # Optional fields absent when not provided
    assert "files" not in req.json_body
    assert "options" not in req.json_body
    assert "approval_meta" not in req.json_body
    await api.close()


async def test_post_message_full_payload(mock_bgos_server):
    """Optional fields (files, options, approval_meta, reply_to) flow through correctly."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 200})

    await api.post_message(
        chat_id=7,
        text="Proceed?",
        sender_type="ASSISTANT",
        message_type="approval_request",
        files=[{"filename": "x.png", "s3_key": "k/1", "mime": "image/png", "size": 1024}],
        options=[{"label": "Yes", "callback_data": "ea:once:1", "style": "success"}],
        approval_meta={"command": "rm -rf", "session_key": "s1", "approval_id": 1},
        reply_to_message_id=100,
    )

    req = mock_bgos_server.last_request("POST", "/api/v1/messages")
    body = req.json_body
    assert body["files"][0]["s3_key"] == "k/1"
    assert body["options"][0]["callback_data"] == "ea:once:1"
    assert body["approval_meta"]["session_key"] == "s1"
    assert body["reply_to_message_id"] == 100
    await api.close()


async def test_patch_message_sends_changes(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("PATCH", "/api/v1/messages/42").respond(200, {"id": 42})

    await api.patch_message(42, text="updated text")

    req = mock_bgos_server.last_request("PATCH", "/api/v1/messages/42")
    assert req.json_body == {"text": "updated text"}
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


async def test_whoami_returns_pairing_context(mock_bgos_server):
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {"pairing_id": 42, "assistants": [{"id": 7, "agent_route": "hades"}]},
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
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/files/upload-url",
    ).respond(200, {"upload_url": "https://s3/..", "s3_key": "k/1",
                    "expires_at": "2099-01-01T00:00:00Z"})

    resp = await api.create_upload_url(filename="big.png", mime="image/png", size=1024000)

    assert resp["s3_key"] == "k/1"
    req = mock_bgos_server.last_request("POST", "/api/v1/integrations/files/upload-url")
    assert req.json_body == {"filename": "big.png", "mime": "image/png", "size": 1024000}
    await api.close()


async def test_500_with_non_json_body_still_raises(mock_bgos_server):
    """Backend crash path: 500 with text body (e.g. stack trace). Should raise BgosApiError
    without crashing on JSON parse."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    mock_bgos_server.on("POST", "/api/v1/messages").respond(500, text="Internal Server Error")

    with pytest.raises(BgosApiError) as excinfo:
        await api.post_message(chat_id=1, text="x", sender_type="ASSISTANT")
    assert excinfo.value.status == 500
    await api.close()


async def test_require_pairing_token_when_missing(mock_bgos_server):
    """Calling an authenticated endpoint without a pairing token should fail fast."""
    api = BgosApi(BgosConfig(base_url=mock_bgos_server.url, pairing_token=None))

    with pytest.raises(RuntimeError, match="pairing token"):
        await api.whoami()
    await api.close()
