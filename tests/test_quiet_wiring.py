"""Integration coverage for quiet routing in outbound send and edit paths."""
from __future__ import annotations

import asyncio

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio

CHAT_ID = 42
A2A_CHAT_ID = 43
ASSISTANT_ID = 894
OWNER_ASSISTANT_ID = 872
PREVIEW_ID = 500
CARD_ID = 9001
TOOL_JSON = (
    '{"name":"calendar_read","arguments":{"start":"2026-07-18"}}'
)
EMOJI_TOOL = '📖 read_file: "/etc/hostname"'


@pytest.fixture(autouse=True)
def _tidy_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    monkeypatch.setenv("BGOS_POLL_INTERVAL", "60")


async def _connected_adapter(server) -> BGOSAdapter:
    """Connect with one assistant, then prime its chat as received inbound."""
    server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {
            "pairing_id": 9,
            "user_id": "user_kc",
            "assistants": [
                {"assistant_id": ASSISTANT_ID, "agent_route": "default"},
            ],
        },
    )
    server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {"messages": []},
    )
    server.on(
        "PUT",
        f"/api/v1/integrations/assistants/{ASSISTANT_ID}/commands",
    ).respond(200, {})
    server.on(
        "POST", "/api/v1/integrations/pairings/9/agent-catalog",
    ).respond(200, {})
    server.on("GET", f"/api/v1/chats/{CHAT_ID}").respond(
        200, {"id": CHAT_ID, "assistantId": ASSISTANT_ID, "kind": "dm"},
    )
    server.on("GET", f"/api/v1/chats/{A2A_CHAT_ID}").respond(
        200,
        {
            "id": A2A_CHAT_ID,
            "assistantId": OWNER_ASSISTANT_ID,
            "kind": "a2a",
        },
    )
    server.on("POST", "/api/v1/messages").respond(201, {"id": CARD_ID})
    server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 9002}},
    )
    server.on("PATCH", f"/api/v1/messages/{CARD_ID}").respond(
        200, {"id": CARD_ID},
    )
    server.on("PATCH", f"/api/v1/messages/{PREVIEW_ID}").respond(
        200, {"id": PREVIEW_ID},
    )

    adapter = BGOSAdapter(
        BgosConfig(base_url=server.url, pairing_token="pair_xyz")
    )
    await adapter.connect()
    adapter._state.record_inbound_chat(CHAT_ID)
    adapter._state.assistant_id_by_chat[CHAT_ID] = ASSISTANT_ID
    adapter._state.last_user_id_by_chat[CHAT_ID] = "user_kc"
    return adapter


def _tool_progress_bodies(server) -> list[dict]:
    return [
        request.json_body
        for request in server.requests
        if request.method in {"POST", "PATCH"}
        and request.path.startswith("/api/v1/messages")
        and isinstance(request.json_body, dict)
        and "toolProgress" in request.json_body
    ]


def _standard_post_bodies(server) -> list[dict]:
    return [
        request.json_body
        for request in server.requests
        if request.method == "POST"
        and request.path in {
            "/api/v1/messages",
            "/api/v1/send-message",
        }
        and isinstance(request.json_body, dict)
        and request.json_body.get("messageType") == "standard"
    ]


async def test_function_call_json_edit_never_patches_chat_text(
    mock_bgos_server,
):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, TOOL_JSON,
        )

        raw_text_patches = [
            request
            for request in mock_bgos_server.requests
            if request.method == "PATCH"
            and isinstance(request.json_body, dict)
            and request.json_body.get("text") == TOOL_JSON
        ]
        assert raw_text_patches == []
        assert result.message_id == str(PREVIEW_ID)
        assert _tool_progress_bodies(mock_bgos_server)
        tools = _tool_progress_bodies(mock_bgos_server)[-1]["toolProgress"][
            "tools"
        ]
        assert any(tool["name"] == "calendar_read" for tool in tools)
    finally:
        await adapter.disconnect()


async def test_function_call_json_send_never_posts_standard(
    mock_bgos_server,
):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.send(CHAT_ID, TOOL_JSON)

        assert not any(
            body.get("text") == TOOL_JSON
            for body in _standard_post_bodies(mock_bgos_server)
        )
        tools = _tool_progress_bodies(mock_bgos_server)[-1]["toolProgress"][
            "tools"
        ]
        assert any(tool["name"] == "calendar_read" for tool in tools)
        assert adapter._quiet_suppressed_text[CHAT_ID] == TOOL_JSON
        assert result.message_id == str(CARD_ID)
    finally:
        await adapter.disconnect()


async def test_first_reply_uses_addressed_assistant_style(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.assistant_id_by_chat.pop(CHAT_ID)
    adapter.chat_style_store.set_style(ASSISTANT_ID, "everything")

    async def reply(event) -> None:
        await adapter.send(event.chat_id, TOOL_JSON)

    monkeypatch.setattr(adapter, "handle_message", reply)
    try:
        await adapter._handle_inbound(
            {
                "assistant_id": ASSISTANT_ID,
                "chat_id": CHAT_ID,
                "message_id": 1001,
                "user_id": "user_kc",
                "text": "Run it",
                "files": [],
                "message_type": "standard",
            },
            batchable=False,
        )

        assert adapter._state.addressed_assistant_id_by_chat[CHAT_ID] == (
            ASSISTANT_ID
        )
        assert any(
            body.get("text") == TOOL_JSON
            for body in _standard_post_bodies(mock_bgos_server)
        )
        assert _tool_progress_bodies(mock_bgos_server) == []
    finally:
        await adapter.disconnect()


async def test_a2a_style_uses_addressed_assistant_not_chat_owner(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter.chat_style_store.set_style(ASSISTANT_ID, "everything")

    async def reply(event) -> None:
        await adapter.send(event.chat_id, TOOL_JSON)

    monkeypatch.setattr(adapter, "handle_message", reply)
    try:
        await adapter._handle_inbound(
            {
                "assistant_id": ASSISTANT_ID,
                "chat_id": A2A_CHAT_ID,
                "message_id": 1002,
                "user_id": "user_kc",
                "text": "Run it for the peer",
                "files": [],
                "message_type": "standard",
            },
            batchable=False,
        )

        assert adapter._state.addressed_assistant_id_by_chat[A2A_CHAT_ID] == (
            ASSISTANT_ID
        )
        assert adapter._state.assistant_id_by_chat[A2A_CHAT_ID] == (
            OWNER_ASSISTANT_ID
        )
        bridged = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "POST"
            and request.path == "/api/v1/send-message"
            and request.json_body.get("text") == TOOL_JSON
        ]
        assert len(bridged) == 1
        assert bridged[0]["assistantId"] == OWNER_ASSISTANT_ID
        assert _tool_progress_bodies(mock_bgos_server) == []
    finally:
        await adapter.disconnect()


async def test_dump_rows_append_to_existing_emoji_rows(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, '📖 read_file: "/etc/hostname"',
        )
        await adapter.edit_message(CHAT_ID, PREVIEW_ID, TOOL_JSON)

        tools = _tool_progress_bodies(mock_bgos_server)[-1]["toolProgress"][
            "tools"
        ]
        assert [tool["name"] for tool in tools] == [
            "read_file",
            "calendar_read",
        ]
    finally:
        await adapter.disconnect()


async def test_dump_row_survives_next_accumulated_emoji_edit(
    mock_bgos_server,
):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, '📖 read_file: "/etc/hostname"',
        )
        await adapter.edit_message(CHAT_ID, PREVIEW_ID, TOOL_JSON)
        await adapter.edit_message(
            CHAT_ID,
            PREVIEW_ID,
            (
                '📖 read_file: "/etc/hostname"\n'
                '🔎 web_search: "Saturday calendar"'
            ),
        )

        body = _tool_progress_bodies(mock_bgos_server)[-1]
        assert [
            tool["name"] for tool in body["toolProgress"]["tools"]
        ] == ["read_file", "web_search", "calendar_read"]
        assert body["text"].startswith("Using 3 tools")
    finally:
        await adapter.disconnect()


async def test_prose_send_passes_through_and_cancels_failsafe(
    mock_bgos_server,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._quiet_failsafe_seconds = 0.05
    try:
        await adapter.send(CHAT_ID, TOOL_JSON)
        await adapter.send(CHAT_ID, "All done, your Saturday is planned.")
        await asyncio.sleep(0.15)

        standards = _standard_post_bodies(mock_bgos_server)
        assert any(
            body["text"] == "All done, your Saturday is planned."
            for body in standards
        )
        assert not any(body.get("text") == TOOL_JSON for body in standards)
    finally:
        await adapter.disconnect()


async def test_failsafe_promotes_suppressed_text(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(CHAT_ID, "session-normal")
    adapter._quiet_failsafe_seconds = 0.05
    try:
        await adapter.send(CHAT_ID, TOOL_JSON)
        await asyncio.sleep(0.2)

        standards = _standard_post_bodies(mock_bgos_server)
        assert any(TOOL_JSON in body["text"] for body in standards)
        promoted = next(
            body for body in standards if TOOL_JSON in body["text"]
        )
        assert "sessionHandle" not in promoted
        card_bodies = _tool_progress_bodies(mock_bgos_server)
        assert card_bodies[-1]["toolProgress"]["state"] == "done"
    finally:
        await adapter.disconnect()


async def test_a2a_failsafe_uses_bridge_and_session_handle(
    mock_bgos_server,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(A2A_CHAT_ID, "session-a2a")
    adapter._state.addressed_assistant_id_by_chat[A2A_CHAT_ID] = ASSISTANT_ID
    adapter._quiet_failsafe_seconds = 0.05
    suppressed_edit = "❌ Follow-up tool step failed."
    try:
        await adapter.send(A2A_CHAT_ID, EMOJI_TOOL, reply_to=778)
        adapter._state.record_inbound_chat(A2A_CHAT_ID, "session-newer-turn")
        await adapter.edit_message(
            A2A_CHAT_ID, PREVIEW_ID, suppressed_edit,
        )
        await asyncio.sleep(0.2)

        promoted = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "POST"
            and request.path == "/api/v1/send-message"
            and request.json_body.get("messageType") == "standard"
            and suppressed_edit in request.json_body.get("text", "")
        ]
        assert len(promoted) == 1
        assert promoted[0]["assistantId"] == OWNER_ASSISTANT_ID
        assert promoted[0]["sessionHandle"] == "session-a2a"
        assert promoted[0]["replyToId"] == 778
        assert not any(
            request.method == "POST"
            and request.path == "/api/v1/messages"
            and request.json_body.get("messageType") == "standard"
            and suppressed_edit in request.json_body.get("text", "")
            for request in mock_bgos_server.requests
        )
    finally:
        await adapter.disconnect()


async def test_everything_mode_passes_dump_as_chat(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter.chat_style_store.set_style(ASSISTANT_ID, "everything")
    try:
        await adapter.send(CHAT_ID, TOOL_JSON)
        result = await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, TOOL_JSON,
        )

        standards = _standard_post_bodies(mock_bgos_server)
        assert any(body["text"] == TOOL_JSON for body in standards)
        assert _tool_progress_bodies(mock_bgos_server) == []
        assert CHAT_ID not in adapter._quiet_suppressed_text
        assert CHAT_ID not in adapter._quiet_failsafe_tasks
        preview_patches = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "PATCH"
            and request.path == f"/api/v1/messages/{PREVIEW_ID}"
        ]
        assert preview_patches[-1]["text"] == TOOL_JSON
        assert result.message_id == str(PREVIEW_ID)
    finally:
        await adapter.disconnect()


async def test_voice_prefix_stripped_in_tidy(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.send(CHAT_ID, "[voice consult] Kickoff is at 6:45pm.")

        standards = _standard_post_bodies(mock_bgos_server)
        assert standards[-1]["text"].startswith("Kickoff")
    finally:
        await adapter.disconnect()


async def test_voice_prefix_kept_in_everything(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter.chat_style_store.set_style(ASSISTANT_ID, "everything")
    try:
        await adapter.send(CHAT_ID, "[voice consult] Kickoff is at 6:45pm.")

        standards = _standard_post_bodies(mock_bgos_server)
        assert standards[-1]["text"].startswith("[voice consult]")
    finally:
        await adapter.disconnect()


async def test_error_send_posts_agent_error_with_event_meta(
    mock_bgos_server,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(CHAT_ID, "session-normal")
    try:
        await adapter.send(CHAT_ID, "❌ Hermes update failed.")

        errors = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "POST"
            and request.path == "/api/v1/messages"
            and isinstance(request.json_body, dict)
            and request.json_body.get("messageType") == "agent_error"
        ]
        assert len(errors) == 1
        body = errors[0]
        assert body["text"] == "Hermes update failed."
        assert body["sender"] == "assistant"
        assert body["sessionHandle"] == "session-normal"
        assert body["eventMeta"]["source"] == "agent"
        assert body["eventMeta"]["title"] == "Hermes update failed."
        assert "❌ Hermes update failed." in body["eventMeta"]["payload"][
            "details"
        ]
        assert not any(
            request.method == "POST"
            and request.path == "/api/v1/send-message"
            and request.json_body.get("messageType") == "agent_error"
            for request in mock_bgos_server.requests
        )
    finally:
        await adapter.disconnect()


async def test_a2a_error_uses_bridge_with_session_and_event_meta(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(A2A_CHAT_ID, "session-a2a")
    adapter._state.addressed_assistant_id_by_chat[A2A_CHAT_ID] = ASSISTANT_ID
    get_chat = adapter._api.get_chat

    async def rotate_handle_during_lookup(chat_id: int) -> dict:
        adapter._state.record_inbound_chat(
            A2A_CHAT_ID, "session-newer-turn",
        )
        return await get_chat(chat_id)

    monkeypatch.setattr(adapter._api, "get_chat", rotate_handle_during_lookup)
    try:
        await adapter.send(A2A_CHAT_ID, "❌ Hermes update failed.")

        errors = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "POST"
            and request.path == "/api/v1/send-message"
            and request.json_body.get("messageType") == "agent_error"
        ]
        assert len(errors) == 1
        body = errors[0]
        assert body["assistantId"] == OWNER_ASSISTANT_ID
        assert body["sessionHandle"] == "session-a2a"
        assert body["text"] == "Hermes update failed."
        assert body["eventMeta"]["source"] == "agent"
        assert body["eventMeta"]["title"] == "Hermes update failed."
        assert "❌ Hermes update failed." in body["eventMeta"]["payload"][
            "details"
        ]
        assert not any(
            request.method == "POST"
            and request.path == "/api/v1/messages"
            and request.json_body.get("messageType") == "agent_error"
            for request in mock_bgos_server.requests
        )
    finally:
        await adapter.disconnect()


async def test_a2a_error_uses_cached_kind_when_lookup_fails(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(A2A_CHAT_ID, "session-a2a")
    adapter._state.addressed_assistant_id_by_chat[A2A_CHAT_ID] = ASSISTANT_ID
    adapter._state.assistant_id_by_chat[A2A_CHAT_ID] = OWNER_ASSISTANT_ID
    adapter._state.chat_kind_by_chat[A2A_CHAT_ID] = "a2a"

    async def fail_lookup(_chat_id: int) -> dict:
        raise RuntimeError("transient lookup failure")

    monkeypatch.setattr(adapter._api, "get_chat", fail_lookup)
    try:
        await adapter.send(A2A_CHAT_ID, "❌ Hermes update failed.")

        errors = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "POST"
            and request.path == "/api/v1/send-message"
            and request.json_body.get("messageType") == "agent_error"
        ]
        assert len(errors) == 1
        assert errors[0]["assistantId"] == OWNER_ASSISTANT_ID
        assert errors[0]["sessionHandle"] == "session-a2a"
    finally:
        await adapter.disconnect()


async def test_edit_prose_still_streams(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        result = await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, "The plan is ready.",
        )

        preview_patches = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "PATCH"
            and request.path == f"/api/v1/messages/{PREVIEW_ID}"
        ]
        assert preview_patches[-1]["text"] == "The plan is ready."
        assert result.message_id == str(PREVIEW_ID)
    finally:
        await adapter.disconnect()


async def test_deferred_prose_edit_cancels_suppressed_failsafe(
    mock_bgos_server,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._quiet_failsafe_seconds = 0.12
    adapter._edit_throttle_seconds = 0.03
    try:
        await adapter.send(CHAT_ID, TOOL_JSON)
        adapter._last_edit_at[CHAT_ID] = asyncio.get_running_loop().time()
        await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, "The plan is ready.",
        )
        await asyncio.sleep(0.2)

        preview_patches = [
            request.json_body
            for request in mock_bgos_server.requests
            if request.method == "PATCH"
            and request.path == f"/api/v1/messages/{PREVIEW_ID}"
        ]
        assert preview_patches[-1]["text"] == "The plan is ready."
        assert CHAT_ID not in adapter._quiet_suppressed_text
        assert CHAT_ID not in adapter._quiet_failsafe_tasks
        assert not any(
            body.get("text") == TOOL_JSON
            for body in _standard_post_bodies(mock_bgos_server)
        )
    finally:
        await adapter.disconnect()


async def test_prose_edit_does_not_clear_newer_suppression(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.send(CHAT_ID, TOOL_JSON)

        async def patch_and_suppress(
            message_id: int, **_kwargs,
        ) -> dict:
            adapter._quiet_record_suppressed(CHAT_ID, "newer dump")
            return {"id": message_id}

        monkeypatch.setattr(adapter._api, "patch_message", patch_and_suppress)
        await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, "The plan is ready.",
        )

        assert adapter._quiet_suppressed_text[CHAT_ID] == "newer dump"
        assert CHAT_ID in adapter._quiet_failsafe_tasks
    finally:
        await adapter.disconnect()


async def test_slow_visible_edit_pauses_old_failsafe(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
):
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._quiet_failsafe_seconds = 0.03
    try:
        await adapter.send(CHAT_ID, TOOL_JSON)

        async def slow_patch(message_id: int, **_kwargs) -> dict:
            await asyncio.sleep(0.08)
            return {"id": message_id}

        monkeypatch.setattr(adapter._api, "patch_message", slow_patch)
        await adapter.edit_message(
            CHAT_ID, PREVIEW_ID, "The plan is ready.",
        )
        await asyncio.sleep(0.12)

        assert CHAT_ID not in adapter._quiet_suppressed_text
        assert CHAT_ID not in adapter._quiet_failsafe_tasks
        assert not any(
            body.get("text") == TOOL_JSON
            for body in _standard_post_bodies(mock_bgos_server)
        )
    finally:
        await adapter.disconnect()
