"""Tests for the daemon env facts reported on the version heartbeat.

Reduced from the original connect-time report design: main's 6h heartbeat
loop (test_heartbeat.py) already covers the version-reporting cadence, so
the env facts simply ride that existing beat as the backend HeartbeatDto
`env` object ({platform?, python?, hermes?}, values <=64 chars).
"""
from __future__ import annotations

from importlib import metadata
import platform

import pytest

import hermes_channel_bgos.bgos_adapter as adapter_module


def test_daemon_env_reports_platform_and_python() -> None:
    env = adapter_module._daemon_env()
    assert env["platform"] == platform.system().lower()[:64]
    assert env["python"] == platform.python_version()[:64]
    assert all(len(value) <= 64 for value in env.values())


def test_daemon_env_truncates_values_and_tries_metadata_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[str] = []

    def fake_version(distribution: str) -> str:
        looked_up.append(distribution)
        if distribution == "hermes-agent":
            raise metadata.PackageNotFoundError(distribution)
        return "h" * 80

    monkeypatch.setattr(platform, "system", lambda: "P" * 80)
    monkeypatch.setattr(platform, "python_version", lambda: "y" * 80)
    monkeypatch.setattr(metadata, "version", fake_version)

    env = adapter_module._daemon_env()

    assert looked_up == ["hermes-agent", "hermes_agent"]
    assert env == {
        "platform": "p" * 64,
        "python": "y" * 64,
        "hermes": "h" * 64,
    }


def test_daemon_env_omits_hermes_on_metadata_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_version(_distribution: str) -> str:
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(metadata, "version", fail_version)

    env = adapter_module._daemon_env()

    assert "hermes" not in env
