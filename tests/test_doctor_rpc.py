"""Tests for doctor_rpc WebSocket dispatch, adapter handling, and REST replies."""
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
from hermes_channel_bgos.doctor import CheckResult


pytestmark = pytest.mark.asyncio

RUN_FRAME = {"rpcId": "rpc-doctor-1", "op": "run", "payload": {}}


@dataclass
class FakeApi:
    acks: list[str] = field(default_factory=list)
    results: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ack_error: Exception | None = None
    result_error: Exception | None = None
    result_posted: asyncio.Event = field(default_factory=asyncio.Event)

    async def post_doctor_rpc_ack(self, rpc_id: str) -> None:
        self.acks.append(rpc_id)
        if self.ack_error is not None:
            raise self.ack_error

    async def post_doctor_rpc_result(
        self, rpc_id: str, body: dict[str, Any],
    ) -> None:
        self.results.append((rpc_id, body))
        self.result_posted.set()
        if self.result_error is not None:
            raise self.result_error

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


async def _wait_for_request(mock_bgos_server, method: str, path: str) -> None:
    async def _wait() -> None:
        while not any(
            request.method == method and request.path == path
            for request in mock_bgos_server.requests
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout=1.0)


async def test_ws_doctor_rpc_dispatches_to_injected_callback() -> None:
    frames: list[dict[str, Any]] = []

    async def on_doctor_rpc(frame: dict[str, Any]) -> None:
        frames.append(frame)

    ws = BgosWs(
        BgosConfig(base_url="https://bgos.test", pairing_token="pair_xyz"),
        on_inbound_message=lambda _data: None,
        on_callback_result=lambda _data: None,
        on_doctor_rpc=on_doctor_rpc,
    )

    handler = ws._sio.handlers["/"]["doctor_rpc"]
    await handler(RUN_FRAME)

    assert frames == [RUN_FRAME]


async def test_doctor_rpc_rest_routes_use_pairing_auth(mock_bgos_server) -> None:
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/doctor-rpc/rpc-7/ack"
    ).respond(200, {"ok": True})
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/doctor-rpc/rpc-7/result"
    ).respond(200, {"ok": True})
    api = BgosApi(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz")
    )
    body = {"ok": True, "payload": {"result": "ok", "checks": []}}
    try:
        await api.post_doctor_rpc_ack("rpc-7")
        await api.post_doctor_rpc_result("rpc-7", body)

        ack = mock_bgos_server.last_request(
            "POST", "/api/v1/integrations/doctor-rpc/rpc-7/ack"
        )
        result = mock_bgos_server.last_request(
            "POST", "/api/v1/integrations/doctor-rpc/rpc-7/result"
        )
        assert ack.headers.get("X-BGOS-Pairing") == "pair_xyz"
        assert result.headers.get("X-BGOS-Pairing") == "pair_xyz"
        assert result.json_body == body
    finally:
        await api.close()


async def test_valid_frame_posts_ack_and_serialized_ok_result(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api
    canned = [
        CheckResult("package", "OK", "installed"),
        CheckResult("auth", "WARN", "not restricted", "Set allowed users"),
    ]

    async def fake_run_checks() -> list[CheckResult]:
        return canned

    monkeypatch.setattr(
        bgos_adapter_module, "run_checks", fake_run_checks, raising=False
    )
    await adapter._handle_doctor_rpc(RUN_FRAME)
    await _wait_for_result(fake_api)

    assert fake_api.acks == ["rpc-doctor-1"]
    assert fake_api.results == [
        (
            "rpc-doctor-1",
            {
                "ok": True,
                "payload": {
                    "result": "ok",
                    "checks": [
                        {
                            "name": "package",
                            "status": "OK",
                            "detail": "installed",
                            "fix": "",
                        },
                        {
                            "name": "auth",
                            "status": "WARN",
                            "detail": "not restricted",
                            "fix": "Set allowed users",
                        },
                    ],
                },
            },
        )
    ]


async def test_serialized_checks_are_clamped_to_backend_limits(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api
    canned = [
        CheckResult("n" * 100, "OK", "d" * 500, "f" * 500),
        *[
            CheckResult(f"check-{index}", "OK", "fine")
            for index in range(1, 25)
        ],
    ]

    async def fake_run_checks() -> list[CheckResult]:
        return canned

    monkeypatch.setattr(
        bgos_adapter_module, "run_checks", fake_run_checks, raising=False
    )
    await adapter._handle_doctor_rpc(RUN_FRAME)
    await _wait_for_result(fake_api)

    _, body = fake_api.results[0]
    checks = body["payload"]["checks"]
    assert body["ok"] is True
    assert body["payload"]["result"] == "ok"
    assert len(checks) == 24
    assert checks[0] == {
        "name": "n" * 64,
        "status": "OK",
        "detail": "d" * 400,
        "fix": "f" * 400,
    }
    assert checks[-1]["name"] == "check-23"


async def test_fail_check_sets_payload_result_to_fail(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api

    async def fake_run_checks() -> list[CheckResult]:
        return [CheckResult("pairing_live", "FAIL", "whoami failed", "Re-pair")]

    monkeypatch.setattr(
        bgos_adapter_module, "run_checks", fake_run_checks, raising=False
    )
    await adapter._handle_doctor_rpc(RUN_FRAME)
    await _wait_for_result(fake_api)

    _, body = fake_api.results[0]
    assert body["ok"] is True
    assert body["payload"]["result"] == "fail"


async def test_unknown_op_posts_nothing(adapter_and_api) -> None:
    adapter, fake_api = adapter_and_api

    await adapter._handle_doctor_rpc(
        {"rpcId": "rpc-doctor-1", "op": "status", "payload": {}}
    )

    assert fake_api.acks == []
    assert fake_api.results == []


@pytest.mark.parametrize(
    "frame",
    [
        {"op": "run", "payload": {}},
        {"rpcId": "", "op": "run", "payload": {}},
    ],
)
async def test_missing_or_empty_rpc_id_posts_nothing(
    adapter_and_api, frame,
) -> None:
    adapter, fake_api = adapter_and_api

    await adapter._handle_doctor_rpc(frame)

    assert fake_api.acks == []
    assert fake_api.results == []


async def test_duplicate_rpc_id_is_ignored_while_in_flight(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_run_checks() -> list[CheckResult]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [CheckResult("package", "OK", "installed")]

    monkeypatch.setattr(
        bgos_adapter_module, "run_checks", fake_run_checks, raising=False
    )
    await adapter._handle_doctor_rpc(RUN_FRAME)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await adapter._handle_doctor_rpc(RUN_FRAME)
    release.set()
    await _wait_for_result(fake_api)

    assert calls == 1
    assert fake_api.acks == ["rpc-doctor-1"]
    assert len(fake_api.results) == 1


async def test_run_checks_error_posts_doctor_error(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api

    async def fake_run_checks() -> list[CheckResult]:
        raise RuntimeError("x" * 400)

    monkeypatch.setattr(
        bgos_adapter_module, "run_checks", fake_run_checks, raising=False
    )
    await adapter._handle_doctor_rpc(RUN_FRAME)
    await _wait_for_result(fake_api)

    _, body = fake_api.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "DOCTOR_ERROR"
    assert len(body["error"]["message"]) <= 300


async def test_run_checks_timeout_posts_doctor_timeout(
    adapter_and_api, monkeypatch,
) -> None:
    adapter, fake_api = adapter_and_api
    cancelled = asyncio.Event()

    async def fake_run_checks() -> list[CheckResult]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        bgos_adapter_module, "run_checks", fake_run_checks, raising=False
    )
    monkeypatch.setattr(
        bgos_adapter_module, "_DOCTOR_RPC_TIMEOUT_SECONDS", 0.01, raising=False
    )
    await adapter._handle_doctor_rpc(RUN_FRAME)
    await _wait_for_result(fake_api)

    _, body = fake_api.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "DOCTOR_TIMEOUT"
    assert cancelled.is_set()


async def test_ack_endpoint_500_does_not_prevent_result(
    mock_bgos_server, monkeypatch,
) -> None:
    ack_path = "/api/v1/integrations/doctor-rpc/rpc-doctor-1/ack"
    result_path = "/api/v1/integrations/doctor-rpc/rpc-doctor-1/result"
    mock_bgos_server.on("POST", ack_path).respond(
        500, {"error": "backend_failed"}
    )
    mock_bgos_server.on("POST", result_path).respond(200, {"ok": True})
    adapter = BGOSAdapter(
        BgosConfig(base_url=mock_bgos_server.url, pairing_token="pair_xyz")
    )

    async def fake_run_checks() -> list[CheckResult]:
        return [CheckResult("package", "OK", "installed")]

    monkeypatch.setattr(
        bgos_adapter_module, "run_checks", fake_run_checks, raising=False
    )
    try:
        await adapter._handle_doctor_rpc(RUN_FRAME)
        await _wait_for_request(mock_bgos_server, "POST", result_path)

        ack = mock_bgos_server.last_request("POST", ack_path)
        result = mock_bgos_server.last_request("POST", result_path)
        assert ack.headers.get("X-BGOS-Pairing") == "pair_xyz"
        assert result.json_body["ok"] is True
    finally:
        await adapter.disconnect()
