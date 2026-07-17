"""Integration coverage for quiet prompt and command surfaces."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.commands_sync import (
    BRIDGE_LOCAL_COMMANDS,
    build_manifest,
)
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.quiet_mode import ChatStyleStore, EVERYTHING, TIDY


pytestmark = pytest.mark.asyncio

CHAT_ID = 42
ASSISTANT_ID = 894
PAIRING_ID = 424242
QUIET_DESCRIPTION = (
    "Show or change how much behind-the-scenes work shows in this chat"
)


@pytest.fixture(autouse=True)
def _tidy_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)


def _adapter(server, *, bind_chat: bool = True) -> BGOSAdapter:
    adapter = BGOSAdapter(
        BgosConfig(base_url=server.url, pairing_token="pair_xyz")
    )
    adapter.pairing_id = PAIRING_ID
    adapter._state.set_route(ASSISTANT_ID, "default")
    if bind_chat:
        adapter._state.assistant_id_by_chat[CHAT_ID] = ASSISTANT_ID
    return adapter


def _message_posts(server) -> list[dict]:
    return [
        request.json_body
        for request in server.requests
        if request.method == "POST"
        and request.path == "/api/v1/messages"
    ]


def _update_options() -> list[dict]:
    return [
        {
            "text": "✓ Yes",
            "callbackData": "update_prompt:y",
            "style": "success",
            "row_index": 0,
        },
        {
            "text": "✗ No",
            "callbackData": "update_prompt:n",
            "style": "default",
            "row_index": 0,
        },
    ]


def _confirm_options(confirm_id: str) -> list[dict]:
    return [
        {
            "text": "✅ Approve Once",
            "callbackData": f"sc:once:{confirm_id}",
            "style": "success",
            "row_index": 0,
        },
        {
            "text": "🔒 Always Approve",
            "callbackData": f"sc:always:{confirm_id}",
            "style": "success",
            "row_index": 0,
        },
        {
            "text": "❌ Cancel",
            "callbackData": f"sc:cancel:{confirm_id}",
            "style": "danger",
            "row_index": 1,
        },
    ]


async def test_update_prompt_tidy_posts_event_with_options_and_full_body(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9001}
    )
    adapter = _adapter(mock_bgos_server)
    first_line = "U" * 320
    prompt = f"\n\n{first_line}\nKeep this second line."
    expected_text = f"⚕ **Update needs your input:**\n\n{prompt}"
    try:
        await adapter.send_update_prompt(CHAT_ID, prompt)

        body = _message_posts(mock_bgos_server)[-1]
        assert body["messageType"] == "event"
        assert body["text"] == expected_text
        assert body["options"] == _update_options()
        assert body["eventMeta"] == {
            "source": "update",
            "title": "Small update ready",
            "peek": first_line[:300],
        }
    finally:
        await adapter.disconnect()


async def test_update_prompt_everything_preserves_legacy_wire_shape(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9002}
    )
    adapter = _adapter(mock_bgos_server)
    adapter.chat_style_store.set_style(ASSISTANT_ID, EVERYTHING)
    prompt = "Restore stashed config?"
    expected_text = (
        "⚕ **Update needs your input:**\n\n"
        f"{prompt}\n\n_default: no_"
    )
    try:
        await adapter.send_update_prompt(CHAT_ID, prompt, default="no")

        assert _message_posts(mock_bgos_server)[-1] == {
            "chatId": CHAT_ID,
            "text": expected_text,
            "sender": "assistant",
            "messageType": "standard",
            "options": _update_options(),
        }
    finally:
        await adapter.disconnect()


async def test_slash_confirm_tidy_posts_event_and_records_state(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9003}
    )
    adapter = _adapter(mock_bgos_server)
    title = "Reload MCP?"
    message = "\n\nThis invalidates the provider prompt cache.\nContinue?"
    confirm_id = "conf-abc"
    try:
        await adapter.send_slash_confirm(
            chat_id=CHAT_ID,
            title=title,
            message=message,
            session_key="sess-1",
            confirm_id=confirm_id,
        )

        body = _message_posts(mock_bgos_server)[-1]
        assert body["messageType"] == "event"
        assert body["text"] == f"**{title}**\n\n{message}"
        assert body["options"] == _confirm_options(confirm_id)
        assert body["eventMeta"] == {
            "source": "confirm",
            "title": title,
            "peek": "This invalidates the provider prompt cache.",
        }
        assert adapter._slash_confirm_state[confirm_id] == "sess-1"
    finally:
        await adapter.disconnect()


async def test_slash_confirm_tidy_callback_leads_with_outcome_and_title(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
):
    message_id = 9011
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": message_id}
    )
    mock_bgos_server.on(
        "PATCH", f"/api/v1/messages/{message_id}",
    ).respond(200, {"id": message_id})
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.resolve_slash_confirm",
        lambda *_args: None,
    )
    adapter = _adapter(mock_bgos_server)
    try:
        await adapter.send_slash_confirm(
            chat_id=CHAT_ID,
            title="Reload MCP?",
            message="This invalidates the provider prompt cache.",
            session_key="sess-1",
            confirm_id="conf-outcome",
        )
        await adapter._handle_callback(
            {
                "callback_data": "sc:once:conf-outcome",
                "user_id": "user_kc",
                "message_id": message_id,
                "chat_id": CHAT_ID,
            }
        )

        patch = mock_bgos_server.last_request(
            "PATCH", f"/api/v1/messages/{message_id}"
        ).json_body
        assert patch == {
            "text": "✅ Approved once by user_kc: Reload MCP?",
            "options": [],
            "userId": "user_kc",
        }
        assert "eventMeta" not in patch
    finally:
        await adapter.disconnect()


async def test_slash_confirm_everything_preserves_legacy_wire_shape(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9004}
    )
    adapter = _adapter(mock_bgos_server)
    adapter.chat_style_store.set_style(ASSISTANT_ID, EVERYTHING)
    confirm_id = "conf-legacy"
    try:
        await adapter.send_slash_confirm(
            chat_id=CHAT_ID,
            title="Reload MCP?",
            message="Continue?",
            session_key="sess-legacy",
            confirm_id=confirm_id,
        )

        assert _message_posts(mock_bgos_server)[-1] == {
            "chatId": CHAT_ID,
            "text": "**Reload MCP?**\n\nContinue?",
            "sender": "assistant",
            "messageType": "slash_confirm",
            "options": _confirm_options(confirm_id),
        }
    finally:
        await adapter.disconnect()


async def test_status_tidy_posts_friendly_event_with_private_payload(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9005}
    )
    adapter = _adapter(mock_bgos_server)
    adapter._approval_state[1] = "sess-1"
    adapter._approval_state[2] = "sess-2"
    try:
        await adapter._handle_bridge_local("status", {"chat_id": CHAT_ID})

        body = _message_posts(mock_bgos_server)[-1]
        assert body["messageType"] == "event"
        assert str(PAIRING_ID) not in body["text"]
        assert "adapter" not in body["text"].lower()
        assert "Waiting on 2 approvals from you." in body["text"]
        assert body["eventMeta"] == {
            "source": "status",
            "title": "Connected and healthy",
            "peek": "1 agent online",
            "payload": {
                "pairingId": PAIRING_ID,
                "assistantsBound": 1,
                "lastMessageId": 0,
                "pendingApprovals": 2,
            },
        }
    finally:
        await adapter.disconnect()


async def test_status_everything_preserves_legacy_technical_text(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9006}
    )
    adapter = _adapter(mock_bgos_server)
    adapter.chat_style_store.set_style(ASSISTANT_ID, EVERYTHING)
    try:
        await adapter._handle_bridge_local("status", {"chat_id": CHAT_ID})

        assert _message_posts(mock_bgos_server)[-1] == {
            "chatId": CHAT_ID,
            "text": "\n".join(
                [
                    "**BGOS adapter status**",
                    f"- Pairing: {PAIRING_ID}",
                    "- Assistants bound: 1",
                    "- Last message id seen: 0",
                    "- Pending approvals: 0",
                ]
            ),
            "sender": "assistant",
            "messageType": "standard",
        }
    finally:
        await adapter.disconnect()


async def test_quiet_off_persists_and_no_arg_reports_raw_mode(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9007}
    )
    adapter = _adapter(mock_bgos_server)
    try:
        await adapter._handle_bridge_local(
            "quiet", {"chat_id": CHAT_ID, "command_args": "off"}
        )
        assert ChatStyleStore().style_for(ASSISTANT_ID) == EVERYTHING

        await adapter._handle_bridge_local(
            "quiet", {"chat_id": CHAT_ID, "command_args": ""}
        )

        posts = _message_posts(mock_bgos_server)
        assert posts[-2]["messageType"] == "standard"
        assert posts[-2]["text"] == (
            "Raw mode is on. You will see every line exactly as it arrives."
        )
        assert posts[-1]["messageType"] == "standard"
        assert posts[-1]["text"] == (
            "This chat shows everything raw. Send /quiet on to fold tool "
            "chatter into tidy cards."
        )
    finally:
        await adapter.disconnect()


async def test_first_inbound_quiet_command_uses_addressed_assistant(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9012}
    )
    adapter = _adapter(mock_bgos_server, bind_chat=False)
    try:
        await adapter._handle_inbound(
            {
                "assistant_id": ASSISTANT_ID,
                "chat_id": CHAT_ID,
                "message_id": 1003,
                "user_id": "user_kc",
                "text": "/quiet off",
                "files": [],
                "message_type": "slash_command",
                "command_name": "quiet",
                "command_args": "off",
            },
            batchable=False,
        )

        assert ChatStyleStore().style_for(ASSISTANT_ID) == EVERYTHING
        assert _message_posts(mock_bgos_server)[-1]["text"] == (
            "Raw mode is on. You will see every line exactly as it arrives."
        )
    finally:
        await adapter.disconnect()


async def test_voice_rpc_records_addressed_assistant(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = BGOSAdapter(
        BgosConfig(base_url="http://invalid", pairing_token="pair_xyz")
    )

    class Handler:
        async def handle(self, _frame) -> None:
            return None

    monkeypatch.setattr(adapter, "_voice_handler", lambda: Handler())
    try:
        await adapter._handle_voice_rpc(
            {
                "rpcId": "rpc-1",
                "op": "consult",
                "assistantId": ASSISTANT_ID,
                "agentRoute": "default",
                "chatId": CHAT_ID,
                "payload": {},
            }
        )
        await asyncio.sleep(0)

        assert adapter._state.addressed_assistant_id_by_chat[CHAT_ID] == (
            ASSISTANT_ID
        )
    finally:
        await adapter.disconnect()


async def test_quiet_on_restores_tidy_and_removes_persisted_override(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9008}
    )
    adapter = _adapter(mock_bgos_server)
    adapter.chat_style_store.set_style(ASSISTANT_ID, EVERYTHING)
    style_path = Path(os.environ["HERMES_HOME"]) / "bgos_chat_style.json"
    try:
        await adapter._handle_bridge_local(
            "quiet", {"chat_id": CHAT_ID, "command_args": "on"}
        )

        assert ChatStyleStore().style_for(ASSISTANT_ID) == TIDY
        assert str(ASSISTANT_ID) not in json.loads(
            style_path.read_text(encoding="utf-8")
        )
        body = _message_posts(mock_bgos_server)[-1]
        assert body["messageType"] == "standard"
        assert body["text"] == (
            "Tidy mode is on. Tool chatter and system noise will fold into "
            "cards."
        )
    finally:
        await adapter.disconnect()


async def test_quiet_with_unknown_args_posts_usage_line(mock_bgos_server):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9009}
    )
    adapter = _adapter(mock_bgos_server)
    try:
        await adapter._handle_bridge_local(
            "quiet", {"chat_id": CHAT_ID, "command_args": "sometimes"}
        )

        assert _message_posts(mock_bgos_server)[-1] == {
            "chatId": CHAT_ID,
            "text": (
                "Use /quiet on for tidy chat or /quiet off to see everything "
                "raw."
            ),
            "sender": "assistant",
            "messageType": "standard",
        }
    finally:
        await adapter.disconnect()


async def test_quiet_without_assistant_binding_posts_unavailable_notice(
    mock_bgos_server,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(
        201, {"id": 9010}
    )
    adapter = _adapter(mock_bgos_server, bind_chat=False)
    try:
        await adapter._handle_bridge_local(
            "quiet", {"chat_id": CHAT_ID, "command_args": "off"}
        )

        body = _message_posts(mock_bgos_server)[-1]
        assert body["messageType"] == "standard"
        assert body["text"] == (
            "The quiet setting is not available yet for this chat."
        )
    finally:
        await adapter.disconnect()


async def test_quiet_command_is_in_bridge_manifest():
    assert "quiet" in BRIDGE_LOCAL_COMMANDS
    quiet_entry = next(
        entry for entry in build_manifest([]) if entry["command"] == "quiet"
    )
    assert quiet_entry == {
        "command": "quiet",
        "description": QUIET_DESCRIPTION,
        "scope": "all",
    }
    assert len(quiet_entry["description"]) <= 100
