from __future__ import annotations

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.commands_sync import BRIDGE_LOCAL_COMMANDS
from hermes_channel_bgos.config import BgosConfig
from tests.mocks.mock_hermes import (
    MessageEvent as GatewayMessageEvent,
    MessageType as GatewayMessageType,
)


pytestmark = pytest.mark.asyncio


def _make_gateway_adapter(monkeypatch) -> BGOSAdapter:
    adapter = BGOSAdapter(
        BgosConfig(base_url="http://x", pairing_token="pair_xyz")
    )
    adapter._state.set_route(7, "default")
    monkeypatch.setattr(
        bgos_adapter_module,
        "_GatewayMessageEvent",
        GatewayMessageEvent,
    )
    monkeypatch.setattr(
        bgos_adapter_module,
        "_GatewayMessageType",
        GatewayMessageType,
    )
    monkeypatch.setattr(
        adapter,
        "build_source",
        lambda *, chat_id, user_id: {
            "chat_id": chat_id,
            "user_id": user_id,
        },
        raising=False,
    )
    return adapter


async def _dispatch_voice_command(
    monkeypatch,
    args: str,
) -> GatewayMessageEvent:
    adapter = _make_gateway_adapter(monkeypatch)
    received: list[GatewayMessageEvent] = []

    async def capture(event: GatewayMessageEvent) -> None:
        received.append(event)

    adapter.handle_message = capture
    try:
        await adapter._handle_inbound({
            "assistant_id": 7,
            "user_id": "u",
            "chat_id": 42,
            "message_id": 100,
            "text": f"/voice {args}",
            "files": [],
            "message_type": "slash_command",
            "command_name": "voice",
            "command_args": args,
        })
    finally:
        await adapter.disconnect()

    assert len(received) == 1
    return received[0]


async def _connected_adapter(server) -> BGOSAdapter:
    server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {
            "pairing_id": 42,
            "assistants": [
                {"assistant_id": 7, "agent_route": "hades"},
            ],
        },
    )
    adapter = BGOSAdapter(
        BgosConfig(base_url=server.url, pairing_token="pair_xyz")
    )
    await adapter.connect()
    return adapter


async def test_voice_slash_command_passes_through_to_hermes(monkeypatch):
    assert set(BRIDGE_LOCAL_COMMANDS) == {
        "new",
        "retry",
        "status",
        "quiet",
    }
    event = await _dispatch_voice_command(monkeypatch, "tts")
    assert event.message_type == GatewayMessageType.COMMAND
    assert event.text == "/voice tts"
    assert event.raw_message.command_name == "voice"
    assert event.raw_message.command_args == "tts"


async def test_voice_slash_command_off_passes_through(monkeypatch):
    event = await _dispatch_voice_command(monkeypatch, "off")
    assert event.message_type == GatewayMessageType.COMMAND
    assert event.text == "/voice off"
    assert event.raw_message.command_name == "voice"
    assert event.raw_message.command_args == "off"


async def test_send_voice_posts_caption_text_with_audio(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(
        200,
        {"assistantId": 7},
    )
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201,
        {"message": {"id": 702}},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(11)
    caption = "The complete spoken reply."
    try:
        await adapter.send_voice(
            chat_id=11,
            file_bytes=b"voice-bytes",
            filename="reply.ogg",
            mime="audio/ogg",
            caption=caption,
        )
        posts = [
            request
            for request in mock_bgos_server.requests
            if request.method == "POST"
            and request.path in {
                "/api/v1/send-message",
                "/api/v1/messages",
            }
        ]
        assert len(posts) == 1
        body = posts[0].json_body
        assert body["text"] == caption
        assert body["isAudioMessage"] is True
        assert body["audioData"] == "dm9pY2UtYnl0ZXM="
        assert body["audioFileName"] == "reply.ogg"
    finally:
        await adapter.disconnect()
