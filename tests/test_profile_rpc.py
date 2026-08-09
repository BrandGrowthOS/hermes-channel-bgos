"""Tests for profile_rpc (add_profile) WS dispatch, adapter handling, and
REST replies. Mirrors the doctor_rpc harness: the backend emits a
`profile_rpc` frame to the pairing room; the adapter acks over REST, does the
local profile work, and posts the result."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.bgos_api import BgosApi
from hermes_channel_bgos.bgos_ws import BgosWs
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.profile_setup import AddProfileResult


pytestmark = pytest.mark.asyncio

ADD_FRAME = {
    "rpcId": "rpc-profile-1",
    "op": "add_profile",
    "payload": {"assistant_id": 1012, "agent_route": "wolf", "name": "Wolf"},
}


@dataclass
class FakeApi:
    acks: list[str] = field(default_factory=list)
    results: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    result_posted: asyncio.Event = field(default_factory=asyncio.Event)

    async def post_profile_rpc_ack(self, rpc_id: str) -> None:
        self.acks.append(rpc_id)

    async def post_profile_rpc_result(
        self, rpc_id: str, body: dict[str, Any],
    ) -> None:
        self.results.append((rpc_id, body))
        self.result_posted.set()

    async def close(self) -> None:
        pass


@pytest.fixture
async def adapter_and_api() -> tuple[BGOSAdapter, FakeApi]:
    adapter = BGOSAdapter(
        BgosConfig(base_url="https://bgos.test", pairing_token="pair_xyz")
    )
    await adapter._api.close()
    fake_api = FakeApi()
    adapter._api = fake_api  # type: ignore[assignment]
    try:
        yield adapter, fake_api
    finally:
        await adapter.disconnect()


async def _wait_for_result(fake_api: FakeApi) -> None:
    await asyncio.wait_for(fake_api.result_posted.wait(), timeout=1.0)


async def test_ws_profile_rpc_dispatches_to_injected_callback() -> None:
    frames: list[dict[str, Any]] = []

    async def on_profile_rpc(frame: dict[str, Any]) -> None:
        frames.append(frame)

    ws = BgosWs(
        BgosConfig(base_url="https://bgos.test", pairing_token="pair_xyz"),
        on_inbound_message=lambda _data: None,
        on_callback_result=lambda _data: None,
        on_profile_rpc=on_profile_rpc,
    )
    handler = ws._sio.handlers["/"]["profile_rpc"]
    await handler(ADD_FRAME)

    assert frames == [ADD_FRAME]


async def test_profile_rpc_rest_routes_use_pairing_auth(
    mock_bgos_server,
) -> None:
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/profile-rpc/rpc-profile-1/ack",
    ).respond(204, {})
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/profile-rpc/rpc-profile-1/result",
    ).respond(204, {})

    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz")
    )
    try:
        await api.post_profile_rpc_ack("rpc-profile-1")
        await api.post_profile_rpc_result(
            "rpc-profile-1", {"ok": True, "payload": {"profile": "wolf"}},
        )
    finally:
        await api.close()

    ack = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/profile-rpc/rpc-profile-1/ack",
    )
    assert ack.headers.get("X-BGOS-Pairing") == "pair_xyz"
    result = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/profile-rpc/rpc-profile-1/result",
    )
    assert result.json_body == {"ok": True, "payload": {"profile": "wolf"}}


async def test_valid_frame_acks_and_posts_ok_result(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api
    applied: list[dict[str, Any]] = []

    def fake_apply(payload, **kwargs):
        applied.append(dict(payload))
        return AddProfileResult(
            ok=True,
            profile="wolf",
            agents_spec="default:Hermes,wolf:Wolf",
            multiplex=True,
            restart_required=False,
        )

    monkeypatch.setattr(
        bgos_adapter_module, "apply_add_profile", fake_apply,
    )

    await adapter._handle_profile_rpc(ADD_FRAME)
    await _wait_for_result(fake_api)

    assert fake_api.acks == ["rpc-profile-1"]
    assert applied == [ADD_FRAME["payload"]]
    rpc_id, body = fake_api.results[0]
    assert rpc_id == "rpc-profile-1"
    assert body["ok"] is True
    assert body["payload"]["profile"] == "wolf"
    assert body["payload"]["restart_required"] is False


async def test_apply_failure_posts_error_result(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api

    def fake_apply(payload, **kwargs):
        return AddProfileResult(
            ok=False,
            error_code="profile_create_failed",
            error_message="disk full",
        )

    monkeypatch.setattr(
        bgos_adapter_module, "apply_add_profile", fake_apply,
    )

    await adapter._handle_profile_rpc(ADD_FRAME)
    await _wait_for_result(fake_api)

    _rpc_id, body = fake_api.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "profile_create_failed"
    assert "disk full" in body["error"]["message"]


async def test_unknown_op_posts_nothing(adapter_and_api) -> None:
    adapter, fake_api = adapter_and_api

    await adapter._handle_profile_rpc(
        {"rpcId": "rpc-x", "op": "remove_profile", "payload": {}},
    )
    await asyncio.sleep(0.05)

    assert fake_api.acks == []
    assert fake_api.results == []


async def test_missing_rpc_id_posts_nothing(adapter_and_api) -> None:
    adapter, fake_api = adapter_and_api

    await adapter._handle_profile_rpc({"op": "add_profile", "payload": {}})
    await asyncio.sleep(0.05)

    assert fake_api.acks == []
    assert fake_api.results == []


async def test_duplicate_rpc_id_is_ignored_while_in_flight(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api
    release = asyncio.Event()
    calls: list[str] = []

    def slow_apply(payload, **kwargs):
        calls.append(payload["agent_route"])
        # Runs in a worker thread; block until released.
        import time

        while not release.is_set():
            time.sleep(0.01)
        return AddProfileResult(ok=True, profile="wolf", multiplex=True)

    monkeypatch.setattr(bgos_adapter_module, "apply_add_profile", slow_apply)

    await adapter._handle_profile_rpc(ADD_FRAME)
    await adapter._handle_profile_rpc(ADD_FRAME)
    await asyncio.sleep(0.05)
    release.set()
    await _wait_for_result(fake_api)

    assert calls == ["wolf"]
    assert len(fake_api.results) == 1


async def test_ok_result_triggers_an_immediate_scope_refresh(
    adapter_and_api, monkeypatch,
) -> None:
    """Without this the new route only hot-loads on the FIRST inbound for an
    unknown assistant id (the lazy _handle_inbound path); the add_profile
    handler KNOWS the scope just changed, so it refreshes eagerly and the
    agent is routable the moment the app shows success."""
    adapter, fake_api = adapter_and_api
    refreshed = asyncio.Event()

    async def fake_refresh() -> bool:
        refreshed.set()
        return True

    monkeypatch.setattr(adapter, "_refresh_pairing_scope", fake_refresh)
    monkeypatch.setattr(
        bgos_adapter_module,
        "apply_add_profile",
        lambda payload, **kwargs: AddProfileResult(
            ok=True, profile="wolf", multiplex=True,
        ),
    )

    await adapter._handle_profile_rpc(ADD_FRAME)
    await _wait_for_result(fake_api)
    await asyncio.wait_for(refreshed.wait(), timeout=1.0)

    assert fake_api.results[0][1]["ok"] is True


async def test_apply_raising_unexpectedly_still_settles_the_rpc(
    adapter_and_api, monkeypatch,
) -> None:
    """apply_add_profile promises never to raise, but the handler's belt and
    braces must still settle the backend RPC if it ever does."""
    adapter, fake_api = adapter_and_api

    def exploding_apply(payload, **kwargs):
        raise RuntimeError("unexpected explosion")

    monkeypatch.setattr(
        bgos_adapter_module, "apply_add_profile", exploding_apply,
    )

    await adapter._handle_profile_rpc(ADD_FRAME)
    await _wait_for_result(fake_api)

    _rpc_id, body = fake_api.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "ADD_PROFILE_ERROR"
    assert "unexpected explosion" in body["error"]["message"]
