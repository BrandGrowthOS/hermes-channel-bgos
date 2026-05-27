"""Tests for hermes-bgos-doctor."""
from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.doctor import (
    FAIL, OK, WARN, CheckResult, check_catalog, check_config, check_env,
    check_package, check_whoami, main as doctor_main, render_json,
)


def test_check_package_reports_version():
    r = check_package()
    assert r.status == OK
    assert "hermes_channel_bgos" in r.detail


def test_check_catalog_warns_when_unset(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS", raising=False)
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    r = check_catalog()
    assert r.status == WARN
    assert r.fix


def test_check_catalog_ok(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.setenv("BGOS_AGENTS", "default:David")
    r = check_catalog()
    assert r.status == OK
    assert "default:David" in r.detail


def test_check_env_warns_without_auth(monkeypatch):
    monkeypatch.delenv("BGOS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("BGOS_ALLOWED_USERS", raising=False)
    assert check_env().status == WARN


def test_check_env_ok_with_allow_all(monkeypatch):
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    assert check_env().status == OK


def test_check_config_not_paired(monkeypatch):
    # autouse fixture points HERMES_HOME at an empty tmp dir → no secrets file
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    cfg, r = check_config()
    assert cfg is None
    assert r.status == FAIL
    assert "hermes-pair-bgos" in r.fix


async def test_check_whoami_reports_exposed_assistants(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": [
            {"assistant_id": 892, "agent_route": "default", "name": "David"}]},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok"))
    assert r.status == OK
    assert "892" in r.detail


async def test_check_whoami_401_is_fail(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        401, {"error": "PAIRING_REVOKED"},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="bad"))
    assert r.status == FAIL
    assert "re-pair" in r.fix.lower()


async def test_check_whoami_warns_when_no_assistants(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": []},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok"))
    assert r.status == WARN


def test_render_json_marks_fail():
    data = json.loads(render_json([
        CheckResult("a", OK, "x"),
        CheckResult("b", FAIL, "y", "fixit"),
    ]))
    assert data["result"] == "fail"
    assert data["checks"][1]["fix"] == "fixit"


def test_render_json_ok_when_no_fail():
    data = json.loads(render_json([CheckResult("a", OK, "x"), CheckResult("b", WARN, "y")]))
    assert data["result"] == "ok"


async def test_doctor_main_exits_1_when_unconfigured(monkeypatch):
    # No Hermes gateway in the test env → fork_patch FAILs → exit 1.
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    runner = CliRunner()
    result = await asyncio.to_thread(runner.invoke, doctor_main, ["--offline"])
    assert result.exit_code == 1
    assert "fork_patch" in result.output
