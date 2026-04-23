"""Tests for BGOSAdapter's inbound pipeline (Task 5).

`inbound_message` WS events → MessageEvent → self.handle_message(event).
Also verifies the reconnect backfill path: on WS reconnect, fetch_inbound_since
is called with the last seen message_id, and each returned message is fed
through the same translation path.
"""
from __future__ import annotations

import asyncio

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter, MessageEvent
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


async def test_inbound_message_translates_and_handles(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # Wait for WS connection + room join to settle
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        await mock_bgos_server.emit_to_room(
            "assistant:7",
            "inbound_message",
            {
                "chat_id": 11, "message_id": 200, "text": "hello world",
                "user_id": "user_1", "assistant_id": 7, "message_type": "standard",
            },
        )
        await asyncio.sleep(0.2)

        assert len(handled) == 1
        event = handled[0]
        assert event.platform == "bgos"
        assert event.chat_id == 11
        assert event.message_id == 200
        assert event.user_id == "user_1"
        assert event.assistant_id == 7
        assert event.agent_route == "hades"
        assert event.text == "hello world"
        assert event.files == []
        assert event.message_type == "standard"
        assert event.command_name is None
        assert event.command_args is None

        # Retry cache populated
        assert adapter._state.last_user_text_by_chat[11] == "hello world"
    finally:
        await adapter.disconnect()


async def test_inbound_for_unknown_assistant_is_dropped(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # Deliver inbound directly (bypass room routing) with unknown assistant
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 201, "text": "ignored",
            "user_id": "u", "assistant_id": 999, "message_type": "standard",
        })
        assert handled == []
    finally:
        await adapter.disconnect()


async def test_inbound_carries_command_metadata_for_slash_commands(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 500, "text": "/help",
            "user_id": "u", "assistant_id": 7,
            "message_type": "slash_command", "command_name": "help", "command_args": "",
        })
        assert len(handled) == 1
        ev = handled[0]
        assert ev.message_type == "slash_command"
        assert ev.command_name == "help"
        assert ev.command_args == ""
    finally:
        await adapter.disconnect()


async def test_reconnect_triggers_backfill(mock_bgos_server, monkeypatch):
    """After reconnect the adapter calls fetch_inbound_since(last_id) and
    feeds each returned message through the translation pipeline."""
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {
            "messages": [
                {"chat_id": 11, "message_id": 300, "text": "while-you-were-out-1",
                 "user_id": "u", "assistant_id": 7, "message_type": "standard"},
                {"chat_id": 11, "message_id": 301, "text": "while-you-were-out-2",
                 "user_id": "u", "assistant_id": 7, "message_type": "standard"},
            ],
        },
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # connect() schedules a first-connect backfill(0); drain and reset
        # so we're only asserting against the explicit reconnect call.
        await asyncio.sleep(0.15)
        handled.clear()

        # Simulate reconnect: fire the _on_reconnect hook directly (in
        # production the WS client invokes it after a successful re-handshake)
        await adapter._run_backfill(250)

        assert [ev.message_id for ev in handled] == [300, 301]
        assert [ev.text for ev in handled] == [
            "while-you-were-out-1",
            "while-you-were-out-2",
        ]

        last_inbound = [
            r for r in mock_bgos_server.requests
            if r.method == "GET" and r.path == "/api/v1/integrations/inbound"
        ][-1]
        assert last_inbound.query["since_message_id"] == "250"
    finally:
        await adapter.disconnect()


async def test_reconnect_with_empty_backfill_noop(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {"messages": []},
    )

    handled: list[MessageEvent] = []
    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    await adapter.connect()
    try:
        # connect() fires a first-connect backfill using the cursor from
        # $HERMES_HOME/bgos_last_id. The autouse _isolate_hermes_home
        # fixture ensures that file starts absent → cursor=0. Mock returns
        # empty → handled stays []. Drain any scheduled task before
        # the explicit call.
        await asyncio.sleep(0.1)
        await adapter._run_backfill(100)
        assert handled == []
    finally:
        await adapter.disconnect()


async def test_last_id_persists_across_connects(mock_bgos_server, monkeypatch, tmp_path):
    """bgos_last_id file advances as messages are processed; subsequent
    connect() uses the saved cursor, not 0 — preventing history replay
    on every Hermes restart."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/inbound").respond(
        200, {"messages": []},
    )

    adapter = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    async def _noop(event): return None
    monkeypatch.setattr(adapter, "handle_message", _noop)

    await adapter.connect()
    try:
        await asyncio.sleep(0.1)
        # Simulate processing of a high-id message (as would happen on WS)
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 9999, "text": "hi",
            "user_id": "u", "assistant_id": 7, "message_type": "standard",
        })
        # File should now contain 9999
        last_id_file = tmp_path / "hermes_home" / "bgos_last_id"
        assert last_id_file.read_text().strip() == "9999"
    finally:
        await adapter.disconnect()

    # A fresh adapter with the same HERMES_HOME should load cursor=9999
    adapter2 = BGOSAdapter(BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"))
    assert adapter2._load_last_id() == 9999

    # Older message ids don't regress the cursor
    adapter2._save_last_id(100)
    assert adapter2._load_last_id() == 9999
