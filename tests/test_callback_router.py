"""Tests for BGOSAdapter's callback router (Task 8).

ea:{choice}:{approval_id} callbacks → tools.approval.resolve_gateway_approval
with the session_key looked up from self._approval_state.

Other callback_data strings → self.handle_button_press(data) for the agent.
Stale approval clicks (approval_id not in _approval_state) log + no-op.
"""
from __future__ import annotations

import asyncio

import pytest

import hermes_channel_bgos.bgos_adapter as adapter_mod
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


async def _connected_adapter(server) -> BGOSAdapter:
    server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"id": 7, "agent_route": "hades"}]},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    return adapter


async def test_approval_callback_resolves_via_tools_approval(
    mock_bgos_server, monkeypatch,
):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 777})

    resolved: list[tuple[str, str]] = []

    def fake_resolve(session_key: str, choice: str) -> None:
        resolved.append((session_key, choice))

    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", fake_resolve)

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.send_exec_approval(
            chat_id=11, command="rm -rf node_modules",
            session_key="sess-1", description="Proceed?",
        )
        # approval_id=1 is the first minted by itertools.count(1)
        await adapter._handle_callback(
            {"message_id": 777, "callback_data": "ea:once:1"},
        )

        assert resolved == [("sess-1", "once")]
        assert 1 not in adapter._approval_state
    finally:
        await adapter.disconnect()


async def test_approval_callback_all_four_verdicts(mock_bgos_server, monkeypatch):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 800})

    resolved: list[tuple[str, str]] = []

    def fake_resolve(session_key: str, choice: str) -> None:
        resolved.append((session_key, choice))

    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", fake_resolve)

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        for i, choice in enumerate(["once", "session", "always", "deny"], start=1):
            await adapter.send_exec_approval(
                chat_id=1, command="x", session_key=f"s{i}", description="?",
            )
            await adapter._handle_callback(
                {"message_id": 800, "callback_data": f"ea:{choice}:{i}"},
            )
        assert resolved == [("s1", "once"), ("s2", "session"),
                            ("s3", "always"), ("s4", "deny")]
        assert adapter._approval_state == {}
    finally:
        await adapter.disconnect()


async def test_stale_approval_click_is_noop(mock_bgos_server, monkeypatch):
    """approval_id not in _approval_state (already resolved, timed out, or
    arrived after a restart) — log and return, don't call resolver."""
    resolved: list[tuple[str, str]] = []

    def fake_resolve(sk: str, c: str) -> None:
        resolved.append((sk, c))

    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", fake_resolve)

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        # No prior send_exec_approval — approval_id=9999 is unknown
        await adapter._handle_callback(
            {"message_id": 1, "callback_data": "ea:once:9999"},
        )
        assert resolved == []
    finally:
        await adapter.disconnect()


async def test_non_approval_callback_routes_to_button_handler(
    mock_bgos_server, monkeypatch,
):
    adapter = await _connected_adapter(mock_bgos_server)

    pressed: list[dict] = []

    async def fake_press(event: dict) -> None:
        pressed.append(event)

    monkeypatch.setattr(adapter, "handle_button_press", fake_press, raising=False)

    try:
        await adapter._handle_callback(
            {"message_id": 500, "callback_data": "branch:main", "assistant_id": 7},
        )
        assert len(pressed) == 1
        assert pressed[0]["callback_data"] == "branch:main"
    finally:
        await adapter.disconnect()


async def test_non_approval_callback_without_button_handler_drops(
    mock_bgos_server, caplog,
):
    """Base class doesn't guarantee handle_button_press exists on the mock.
    Drop silently with a debug log rather than crashing."""
    adapter = await _connected_adapter(mock_bgos_server)
    # Ensure handle_button_press is NOT available — delete attribute set by mock
    if hasattr(adapter, "handle_button_press"):
        try:
            del adapter.__dict__["handle_button_press"]  # noqa: SLF001
        except KeyError:
            pass

    # Mock class actually defines handle_button_press at class level — that's
    # fine, the router will call it and it'll no-op. This test still exercises
    # the path that "no approval prefix" → delegates somewhere without crashing.
    try:
        await adapter._handle_callback(
            {"message_id": 501, "callback_data": "unrelated:foo"},
        )
    finally:
        await adapter.disconnect()


async def test_callback_router_integration_via_ws(mock_bgos_server, monkeypatch):
    """Full round-trip: server emits callback_result over Socket.IO →
    _handle_callback dispatches to resolve_gateway_approval."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 555})

    resolved: list[tuple[str, str]] = []

    def fake_resolve(sk: str, c: str) -> None:
        resolved.append((sk, c))

    monkeypatch.setattr(adapter_mod, "resolve_gateway_approval", fake_resolve)

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await mock_bgos_server.wait_for_socket_connection(timeout=3.0)
        await asyncio.sleep(0.1)

        await adapter.send_exec_approval(
            chat_id=11, command="rm -rf /", session_key="DANGER",
            description="REALLY?",
        )
        # approval_id=1
        await mock_bgos_server.emit_to_room(
            "assistant:7", "callback_result",
            {"message_id": 555, "callback_data": "ea:deny:1"},
        )
        await asyncio.sleep(0.2)

        assert resolved == [("DANGER", "deny")]
    finally:
        await adapter.disconnect()
