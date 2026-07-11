"""Capability bootstrap: BgosApi.get_capabilities + resolve_platform_hint.

resolve_platform_hint uses a SYNCHRONOUS httpx.get (it runs at plugin
registration, before the event loop). Against the async aiohttp mock server we
run it in a thread executor so the loop stays free to serve the request.
"""
from __future__ import annotations

import asyncio

import pytest

from hermes_channel_bgos.bgos_api import BgosApi
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.plugin import BGOS_PLATFORM_HINT, resolve_platform_hint

SERVED = (
    "# BGOS Channel Agent Capabilities\n"
    "(channel: hermes, canon v2026.07.11)\n\n"
    "You are talking with a human through BGOS. Reply concisely."
)


async def test_get_capabilities_sends_channel_and_pairing_header(mock_bgos_server):
    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz")
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/capabilities").respond(
        200,
        {
            "channel": "hermes",
            "version": "2026.07.11",
            "text": SERVED,
            "core": "core",
            "channelSyntax": "syntax",
        },
    )

    resp = await api.get_capabilities("hermes")

    assert resp["version"] == "2026.07.11"
    assert "BGOS Channel Agent Capabilities" in resp["text"]
    req = mock_bgos_server.last_request("GET", "/api/v1/integrations/capabilities")
    assert req.query["channel"] == "hermes"
    assert req.headers.get("X-BGOS-Pairing") == "pair_xyz"


async def test_resolve_platform_hint_prefers_served_canon(
    mock_bgos_server, monkeypatch
):
    monkeypatch.setenv("BGOS_BACKEND_URL", mock_bgos_server.url)
    monkeypatch.setenv("BGOS_API_KEY", "pair_xyz")
    monkeypatch.delenv("BGOS_DISABLE_CAPABILITIES_FETCH", raising=False)
    mock_bgos_server.on("GET", "/api/v1/integrations/capabilities").respond(
        200, {"version": "2026.07.11", "text": SERVED}
    )

    # Run the sync fetch off the loop so the async mock server can answer it.
    hint = await asyncio.get_event_loop().run_in_executor(None, resolve_platform_hint)

    assert hint == SERVED
    req = mock_bgos_server.last_request("GET", "/api/v1/integrations/capabilities")
    assert req.query["channel"] == "hermes"


async def test_resolve_platform_hint_falls_back_on_server_error(
    mock_bgos_server, monkeypatch
):
    monkeypatch.setenv("BGOS_BACKEND_URL", mock_bgos_server.url)
    monkeypatch.setenv("BGOS_API_KEY", "pair_xyz")
    monkeypatch.delenv("BGOS_DISABLE_CAPABILITIES_FETCH", raising=False)
    mock_bgos_server.on("GET", "/api/v1/integrations/capabilities").respond(
        500, {"error": "boom"}
    )

    hint = await asyncio.get_event_loop().run_in_executor(None, resolve_platform_hint)

    assert hint == BGOS_PLATFORM_HINT


def test_resolve_platform_hint_falls_back_when_unpaired(monkeypatch):
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    monkeypatch.delenv("BGOS_DISABLE_CAPABILITIES_FETCH", raising=False)
    # No token: no credential to fetch with, no network attempted.
    assert resolve_platform_hint() == BGOS_PLATFORM_HINT


def test_resolve_platform_hint_disabled_by_env(monkeypatch):
    monkeypatch.setenv("BGOS_API_KEY", "pair_xyz")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://127.0.0.1:9")  # would fail if hit
    monkeypatch.setenv("BGOS_DISABLE_CAPABILITIES_FETCH", "1")
    assert resolve_platform_hint() == BGOS_PLATFORM_HINT
