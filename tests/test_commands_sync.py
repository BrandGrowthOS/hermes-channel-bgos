"""Tests for commands_sync.build_manifest + BGOSAdapter.sync_commands_for
and the `/new` / `/retry` / `/status` bridge-local intercepts (Task 9).
"""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.commands_sync import (
    BRIDGE_LOCAL_COMMANDS,
    build_manifest,
)
from hermes_channel_bgos.config import BgosConfig


# ---------------------------------------------------------------------------
# build_manifest — pure function, no fixtures needed
# ---------------------------------------------------------------------------


def test_bridge_local_set_is_exactly_three():
    assert set(BRIDGE_LOCAL_COMMANDS.keys()) == {"new", "retry", "status"}


def test_build_manifest_merges_with_bridge_locals_appended_last():
    native = [
        {"command": "help", "description": "Show help"},
        {"command": "stop", "description": "Stop agent"},
    ]
    merged = build_manifest(native)
    names = [c["command"] for c in merged]
    assert names == ["help", "stop", "new", "retry", "status"]


def test_build_manifest_resolves_status_collision_in_bridge_favor():
    native = [
        {"command": "help", "description": "Show help"},
        {"command": "status", "description": "Hermes status"},
    ]
    merged = build_manifest(native)
    status_entries = [c for c in merged if c["command"] == "status"]
    assert len(status_entries) == 1
    assert "bridge" in status_entries[0]["description"].lower()


def test_build_manifest_drops_native_duplicates():
    native = [
        {"command": "help", "description": "first"},
        {"command": "help", "description": "second"},
        {"command": "help", "description": "third"},
    ]
    merged = build_manifest(native)
    helps = [c for c in merged if c["command"] == "help"]
    assert len(helps) == 1
    assert helps[0]["description"] == "first"  # first occurrence wins


def test_build_manifest_truncates_description_to_100_chars():
    native = [{"command": "x", "description": "a" * 200}]
    merged = build_manifest(native)
    x_entry = next(c for c in merged if c["command"] == "x")
    assert len(x_entry["description"]) == 100


def test_build_manifest_skips_bridge_local_names_in_native():
    """If Hermes ships /new or /retry natively, bridge-local version wins.
    Native entry with that name must be filtered out, not appear twice."""
    native = [{"command": "new", "description": "Hermes /new"}]
    merged = build_manifest(native)
    names = [c["command"] for c in merged]
    # "new" appears once (bridge), not twice
    assert names.count("new") == 1
    new_entry = next(c for c in merged if c["command"] == "new")
    assert "bridge" in new_entry["description"].lower()


def test_build_manifest_strips_empty_command_names():
    native = [{"command": "", "description": "blank"},
              {"command": "   ", "description": "whitespace"}]
    merged = build_manifest(native)
    # Only the three bridge-locals
    assert [c["command"] for c in merged] == ["new", "retry", "status"]


def test_build_manifest_lowercases_names():
    native = [{"command": "HELP", "description": "uppercase"}]
    merged = build_manifest(native)
    assert any(c["command"] == "help" for c in merged)


# ---------------------------------------------------------------------------
# Adapter integration — bridge-local intercepts + sync_commands_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def _connected_adapter(server) -> BGOSAdapter:
    server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    return adapter


@pytest.mark.asyncio
async def test_bridge_local_new_resets_conversation_and_posts_ack(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 900})

    forwarded: list = []
    adapter = await _connected_adapter(mock_bgos_server)

    async def fake_handle(event):
        forwarded.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    try:
        # Prime a conversation binding so we can see it get cleared
        adapter._state.conversation_by_chat[11] = "hermes-conv-xxx"
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 501, "user_id": "u", "assistant_id": 7,
            "text": "/new", "message_type": "slash_command",
            "command_name": "new", "command_args": "",
        })

        # Conversation cleared
        assert 11 not in adapter._state.conversation_by_chat
        # Not forwarded to Hermes agent
        assert forwarded == []
        # Ack message posted
        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        assert any("reset" in r.json_body.get("text", "").lower() for r in posts)
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_bridge_local_retry_replays_last_user_message(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 901})

    handled: list = []
    adapter = await _connected_adapter(mock_bgos_server)

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    try:
        # Prime the retry cache
        adapter._state.last_user_text_by_chat[11] = "what's the weather?"
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 502, "user_id": "u", "assistant_id": 7,
            "text": "/retry", "message_type": "slash_command",
            "command_name": "retry", "command_args": "",
        })
        # Replay produces a handle_message call with the original text
        assert len(handled) == 1
        assert handled[0].text == "what's the weather?"
        assert handled[0].message_type == "standard"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_bridge_local_retry_with_nothing_to_replay_posts_notice(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 902})

    handled: list = []
    adapter = await _connected_adapter(mock_bgos_server)

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    try:
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 503, "user_id": "u", "assistant_id": 7,
            "text": "/retry", "message_type": "slash_command",
            "command_name": "retry", "command_args": "",
        })
        assert handled == []
        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        assert any("nothing to retry" in r.json_body.get("text", "").lower()
                   for r in posts)
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_bridge_local_status_posts_summary(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 903})

    handled: list = []
    adapter = await _connected_adapter(mock_bgos_server)

    async def fake_handle(event):
        handled.append(event)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    try:
        await adapter._handle_inbound({
            "chat_id": 11, "message_id": 504, "user_id": "u", "assistant_id": 7,
            "text": "/status", "message_type": "slash_command",
            "command_name": "status", "command_args": "",
        })
        assert handled == []
        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        status_body = posts[-1].json_body["text"].lower()
        assert "pairing" in status_body
        assert "assistants bound" in status_body
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_sync_commands_for_puts_merged_manifest(mock_bgos_server, monkeypatch):
    mock_bgos_server.on(
        "PUT", "/api/v1/integrations/assistants/7/commands",
    ).respond(200, {})

    # Stub fetch_hermes_native_commands via module-level monkeypatch
    import hermes_channel_bgos.bgos_adapter as adapter_mod

    def fake_fetch(route: str) -> list[dict]:
        assert route == "hades"
        return [{"command": "help", "description": "Show help"}]

    monkeypatch.setattr(adapter_mod, "fetch_hermes_native_commands", fake_fetch)

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.sync_commands_for(7)
        req = mock_bgos_server.last_request(
            "PUT", "/api/v1/integrations/assistants/7/commands",
        )
        names = [c["command"] for c in req.json_body["commands"]]
        assert names == ["help", "new", "retry", "status"]
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_sync_commands_for_unknown_assistant_noop(mock_bgos_server):
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        # assistant 999 isn't in the route map; should silently no-op.
        # (connect() pushes commands for the bound assistant 7, so we assert
        # specifically that NO PUT targeted the unknown 999 path.)
        await adapter.sync_commands_for(999)
        assert not any(
            r.method == "PUT" and r.path.endswith("/assistants/999/commands")
            for r in mock_bgos_server.requests
        )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_connect_pushes_command_catalog(mock_bgos_server, monkeypatch):
    """connect() must push each bound assistant's slash-command manifest so
    the BGOS composer's slash picker is populated.

    Regression: sync_commands_for had NO caller, so a Hermes agent's command
    manifest stayed empty and the picker never popped. Bridge-locals
    (new/retry/status) are always present even with no native commands.
    """
    import hermes_channel_bgos.bgos_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod, "fetch_hermes_native_commands", lambda route: [],
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200,
        {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    mock_bgos_server.on(
        "PUT", "/api/v1/integrations/assistants/7/commands",
    ).respond(200, {})

    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    try:
        req = mock_bgos_server.last_request(
            "PUT", "/api/v1/integrations/assistants/7/commands",
        )
        names = [c["command"] for c in req.json_body["commands"]]
        assert {"new", "retry", "status"}.issubset(set(names))
    finally:
        await adapter.disconnect()
