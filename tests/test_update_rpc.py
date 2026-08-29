"""Tests for update_rpc dispatch, the adapter's update_now decision table,
and the one-click heartbeat extension (wire contract v1, sections 1 and 3).

The security invariants under test: the frame carries nothing beyond
{rpcId, op}; the daemon acks then fails closed and visibly (kill switch,
brake reasons); it exits for relaunch ONLY under a verified supervisor and
otherwise stages to disk; progress stages ride the pairing-authed REST
routes in contract order.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
from hermes_channel_bgos import __version__, self_update
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.bgos_api import BgosApi
from hermes_channel_bgos.bgos_ws import BgosWs
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.self_update import AppliedUpdate, SelfUpdateError


pytestmark = pytest.mark.asyncio

UPDATE_FRAME = {"rpcId": "rpc-update-1", "op": "update_now"}
HEARTBEAT_PATH = "/api/v1/integrations/heartbeat"


@dataclass
class FakeApi:
    acks: list[str] = field(default_factory=list)
    progresses: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ack_error: Exception | None = None

    async def post_update_rpc_ack(self, rpc_id: str) -> None:
        self.acks.append(rpc_id)
        if self.ack_error is not None:
            raise self.ack_error

    async def post_update_rpc_progress(
        self,
        rpc_id: str,
        *,
        stage: str,
        target_version: str | None = None,
        message: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"stage": stage}
        if target_version is not None:
            body["targetVersion"] = target_version
        if message is not None:
            body["message"] = message
        self.progresses.append((rpc_id, body))

    async def close(self) -> None:
        pass


@pytest.fixture
async def adapter_and_api(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BGOSAdapter, FakeApi]:
    adapter = BGOSAdapter(
        BgosConfig(base_url="https://bgos.test", pairing_token="pair_xyz")
    )
    await adapter._api.close()
    fake_api = FakeApi()
    adapter._api = fake_api  # type: ignore[assignment]
    # Deterministic environment defaults; individual tests override.
    monkeypatch.delenv("BGOS_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(
        self_update, "systemd_user_unit", lambda: "hermes-gateway.service",
    )
    monkeypatch.setattr(
        self_update, "pending_restart_version", lambda clone_dir=None: None,
    )
    monkeypatch.setattr(
        self_update,
        "apply_update",
        lambda clone_dir=None: AppliedUpdate("0.28.0", "0.28.1"),
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        self_update,
        "schedule_unit_restart",
        lambda unit: restarts.append(unit) or True,
    )
    adapter._test_restarts = restarts  # type: ignore[attr-defined]
    try:
        yield adapter, fake_api
    finally:
        await adapter.disconnect()


async def _settle_update_tasks(adapter: BGOSAdapter) -> None:
    tasks = list(adapter._update_tasks)
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=2.0,
        )


def _stages(fake_api: FakeApi) -> list[str]:
    return [body["stage"] for _rpc, body in fake_api.progresses]


# -----------------------------------------------------------------------------
# WS routing + REST routes
# -----------------------------------------------------------------------------


async def test_ws_update_rpc_dispatches_to_injected_callback() -> None:
    frames: list[dict[str, Any]] = []

    async def on_update_rpc(frame: dict[str, Any]) -> None:
        frames.append(frame)

    ws = BgosWs(
        BgosConfig(base_url="https://bgos.test", pairing_token="pair_xyz"),
        on_inbound_message=lambda _data: None,
        on_callback_result=lambda _data: None,
        on_update_rpc=on_update_rpc,
    )

    handler = ws._sio.handlers["/"]["update_rpc"]
    await handler(UPDATE_FRAME)

    assert frames == [UPDATE_FRAME]


async def test_update_rpc_rest_routes_use_pairing_auth(mock_bgos_server) -> None:
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/update-rpc/rpc-9/ack"
    ).respond(200, {"ok": True})
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/update-rpc/rpc-9/progress"
    ).respond(200, {"ok": True})
    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz")
    )
    try:
        await api.post_update_rpc_ack("rpc-9")
        await api.post_update_rpc_progress("rpc-9", stage="draining")
        await api.post_update_rpc_progress(
            "rpc-9", stage="restarting",
            target_version="0.28.1", message="ok",
        )

        ack = mock_bgos_server.last_request(
            "POST", "/api/v1/integrations/update-rpc/rpc-9/ack"
        )
        assert ack.headers.get("X-BGOS-Pairing") == "pair_xyz"
        progresses = [
            r for r in mock_bgos_server.requests
            if r.path == "/api/v1/integrations/update-rpc/rpc-9/progress"
        ]
        assert [r.json_body for r in progresses] == [
            {"stage": "draining"},
            {
                "stage": "restarting",
                "targetVersion": "0.28.1",
                "message": "ok",
            },
        ]
        assert all(
            r.headers.get("X-BGOS-Pairing") == "pair_xyz" for r in progresses
        )
    finally:
        await api.close()


# -----------------------------------------------------------------------------
# Frame validation ladder
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        "not-a-dict",
        {"op": "update_now"},
        {"rpcId": "  ", "op": "update_now"},
        {"rpcId": "rpc-x", "op": "something_else"},
        {"rpcId": "rpc-x"},
    ],
)
async def test_invalid_frames_are_dropped_without_side_effects(
    adapter_and_api, frame,
) -> None:
    adapter, fake_api = adapter_and_api
    await adapter._handle_update_rpc(frame)
    await _settle_update_tasks(adapter)
    assert fake_api.acks == []
    assert fake_api.progresses == []


async def test_duplicate_in_flight_frame_is_dropped(adapter_and_api) -> None:
    adapter, fake_api = adapter_and_api
    adapter._update_rpc_in_flight.add("rpc-update-1")
    await adapter._handle_update_rpc(UPDATE_FRAME)
    assert adapter._update_tasks == set()
    assert fake_api.acks == []


# -----------------------------------------------------------------------------
# update_now decision table
# -----------------------------------------------------------------------------


async def test_supervised_update_acks_then_walks_the_full_stage_ladder(
    adapter_and_api,
) -> None:
    adapter, fake_api = adapter_and_api
    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert fake_api.acks == ["rpc-update-1"]
    assert _stages(fake_api) == ["draining", "installing", "restarting"]
    restarting = fake_api.progresses[-1][1]
    assert restarting["targetVersion"] == "0.28.1"
    assert adapter._test_restarts == ["hermes-gateway.service"]
    assert adapter._update_rpc_in_flight == set()


async def test_kill_switch_acks_then_reports_updates_disabled(
    adapter_and_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake_api = adapter_and_api
    monkeypatch.setenv("BGOS_AUTO_UPDATE", "0")
    applied: list[bool] = []
    monkeypatch.setattr(
        self_update,
        "apply_update",
        lambda clone_dir=None: applied.append(True),
    )

    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert fake_api.acks == ["rpc-update-1"]
    assert fake_api.progresses == [
        ("rpc-update-1", {"stage": "error", "message": "updates_disabled"}),
    ]
    assert applied == []
    assert adapter._test_restarts == []


async def test_unsupervised_install_stages_to_disk_and_never_restarts(
    adapter_and_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake_api = adapter_and_api
    monkeypatch.setattr(self_update, "systemd_user_unit", lambda: None)

    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert _stages(fake_api) == ["draining", "installing", "staged"]
    staged = fake_api.progresses[-1][1]
    assert staged["targetVersion"] == "0.28.1"
    assert adapter._test_restarts == []


@pytest.mark.parametrize(
    "reason", ["dirty_tree", "fetch_failed", "not_a_git_checkout"],
)
async def test_apply_failures_surface_the_short_reason(
    adapter_and_api, monkeypatch: pytest.MonkeyPatch, reason,
) -> None:
    adapter, fake_api = adapter_and_api

    def failing_apply(clone_dir=None):
        raise SelfUpdateError(reason)

    monkeypatch.setattr(self_update, "apply_update", failing_apply)

    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert _stages(fake_api) == ["draining", "installing", "error"]
    assert fake_api.progresses[-1][1]["message"] == reason
    assert adapter._test_restarts == []


async def test_no_update_and_no_pending_reports_no_update_available(
    adapter_and_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake_api = adapter_and_api

    def no_update(clone_dir=None):
        raise SelfUpdateError("no_update_available")

    monkeypatch.setattr(self_update, "apply_update", no_update)

    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert _stages(fake_api) == ["draining", "installing", "error"]
    assert fake_api.progresses[-1][1]["message"] == "no_update_available"


async def test_pending_staged_install_completes_via_restart(
    adapter_and_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clone already fast-forwarded by an earlier staged run only needs
    the relaunch: update_now restarts with the on-disk target instead of
    erroring on no_update_available."""
    adapter, fake_api = adapter_and_api

    def no_update(clone_dir=None):
        raise SelfUpdateError("no_update_available")

    monkeypatch.setattr(self_update, "apply_update", no_update)
    monkeypatch.setattr(
        self_update, "pending_restart_version", lambda clone_dir=None: "0.28.1",
    )

    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert _stages(fake_api) == ["draining", "installing", "restarting"]
    assert fake_api.progresses[-1][1]["targetVersion"] == "0.28.1"
    assert adapter._test_restarts == ["hermes-gateway.service"]


async def test_restart_spawn_failure_reports_error(
    adapter_and_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake_api = adapter_and_api
    monkeypatch.setattr(
        self_update, "schedule_unit_restart", lambda unit: False,
    )

    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert _stages(fake_api) == [
        "draining", "installing", "restarting", "error",
    ]
    assert fake_api.progresses[-1][1]["message"] == "restart_spawn_failed"


async def test_ack_failure_does_not_abort_the_update(
    adapter_and_api,
) -> None:
    adapter, fake_api = adapter_and_api
    fake_api.ack_error = RuntimeError("backend hiccup")

    await adapter._handle_update_rpc(UPDATE_FRAME)
    await _settle_update_tasks(adapter)

    assert _stages(fake_api) == ["draining", "installing", "restarting"]


async def test_drain_waits_for_in_flight_work_without_cancelling(
    adapter_and_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, fake_api = adapter_and_api
    monkeypatch.setattr(bgos_adapter_module, "_UPDATE_DRAIN_SECONDS", 0.2)

    hung = asyncio.create_task(asyncio.sleep(30))
    adapter._voice_tasks.add(hung)
    try:
        await adapter._handle_update_rpc(UPDATE_FRAME)
        await _settle_update_tasks(adapter)
        # The drain timed out and moved on; the in-flight task was NOT
        # cancelled by the drain itself.
        assert _stages(fake_api) == ["draining", "installing", "restarting"]
        assert not hung.cancelled()
    finally:
        adapter._voice_tasks.discard(hung)
        hung.cancel()
        await asyncio.gather(hung, return_exceptions=True)


# -----------------------------------------------------------------------------
# Heartbeat extension (contract section 1)
# -----------------------------------------------------------------------------


async def test_post_heartbeat_carries_update_fields(mock_bgos_server) -> None:
    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(204)
    readiness = {
        "supervised": "systemd",
        "autoUpdateEnabled": True,
        "rollbackLatched": False,
        "pendingRestartVersion": None,
    }

    await api.post_heartbeat(
        daemon_version="1.2.3",
        latest_known_version="1.3.0",
        update_readiness=readiness,
    )

    req = mock_bgos_server.last_request("POST", HEARTBEAT_PATH)
    assert req.json_body == {
        "daemonVersion": "1.2.3",
        "latestKnownVersion": "1.3.0",
        "updateReadiness": readiness,
    }
    await api.close()


async def test_post_heartbeat_sends_explicit_null_latest_version(
    mock_bgos_server,
) -> None:
    """A failed daily check sends latestKnownVersion: null (clears a stale
    persisted value server-side); the UNSET default omits the key."""
    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(204)

    await api.post_heartbeat(
        daemon_version="1.2.3", latest_known_version=None,
    )
    req = mock_bgos_server.last_request("POST", HEARTBEAT_PATH)
    assert req.json_body == {
        "daemonVersion": "1.2.3",
        "latestKnownVersion": None,
    }

    await api.post_heartbeat(daemon_version="1.2.3")
    req = mock_bgos_server.last_request("POST", HEARTBEAT_PATH)
    assert req.json_body == {"daemonVersion": "1.2.3"}
    await api.close()


async def test_boot_heartbeat_reports_readiness_and_latest_version(
    mock_bgos_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        self_update, "latest_known_version", lambda: "0.29.0",
    )
    monkeypatch.setattr(
        self_update, "systemd_user_unit", lambda: "hermes-gateway.service",
    )
    monkeypatch.setattr(
        self_update, "pending_restart_version", lambda clone_dir=None: None,
    )
    monkeypatch.delenv("BGOS_AUTO_UPDATE", raising=False)
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": []},
    )
    mock_bgos_server.on("POST", HEARTBEAT_PATH).respond(204)

    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz"),
    )
    await adapter.connect()
    try:
        async def _wait() -> None:
            while not any(
                r.method == "POST" and r.path == HEARTBEAT_PATH
                for r in mock_bgos_server.requests
            ):
                await asyncio.sleep(0.05)
        await asyncio.wait_for(_wait(), timeout=3.0)

        req = mock_bgos_server.last_request("POST", HEARTBEAT_PATH)
        assert req.json_body["daemonVersion"] == __version__
        assert req.json_body["latestKnownVersion"] == "0.29.0"
        assert req.json_body["updateReadiness"] == {
            "supervised": "systemd",
            "autoUpdateEnabled": True,
            "rollbackLatched": False,
            "pendingRestartVersion": None,
        }
    finally:
        await adapter.disconnect()
