"""Pytest fixtures shared across the hermes-channel-bgos test suite."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_channel_bgos import self_update
from tests.mocks.mock_bgos_server import MockBgosServer


# The package test suite asserts the vendor MessageEvent compatibility layer.
# When tests run inside a full Hermes checkout, importing gateway.platforms.base
# would switch the adapter to Hermes's runtime MessageEvent shape and make those
# package-level assertions environment-dependent. Force the isolated mock path in
# tests; production imports keep using Hermes when this env var is unset.
os.environ.setdefault("HERMES_CHANNEL_BGOS_FORCE_MOCK_HERMES", "1")


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-used: redirect HERMES_HOME to a per-test tmp dir so nothing
    the adapter writes (bgos_last_id, secrets/bgos.json, …) leaks between
    tests or pollutes the developer's real ~/.hermes.

    Tests that need to read/write a specific subdirectory (e.g. the
    pairing CLI's `secrets/bgos.json`) can additionally request the
    explicit `tmp_secrets_dir` fixture below — it points at the secrets/
    folder inside this same tmp HERMES_HOME.
    """
    hermes_home = tmp_path_factory.mktemp("hermes_home")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # The doctor's gateway_env() falls back to `<hermes-install>/.env` when
    # `$HERMES_HOME/.env` is absent, and a real dev machine may have a live
    # hermes-agent checkout at one of the well-known paths. Pin the install
    # search to the same tmp dir so tests never read the developer's env.
    monkeypatch.setenv("HERMES_INSTALL", str(hermes_home))


@pytest.fixture(autouse=True)
def _no_network_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-used: pre-seed the self-update daily version cache so the
    heartbeat loop of any connected adapter under test never reaches out to
    GitHub. Tests exercising the check itself reset `_latest_check`."""
    monkeypatch.setattr(
        self_update,
        "_latest_check",
        self_update._LatestCheck(None, time.monotonic()),
    )


@pytest.fixture
async def mock_bgos_server():
    """Start a fresh MockBgosServer on an ephemeral port for each test."""
    server = MockBgosServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def tmp_secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Explicit `secrets/` dir inside HERMES_HOME for tests that need to
    write a `bgos.json` file (primarily the pair CLI suite). Resets
    HERMES_HOME to its own tmp_path rather than inheriting from the
    autouse fixture, so the pair CLI writes land in a predictable
    location the caller controls."""
    hermes_home = tmp_path / "hermes_home"
    secrets_dir = hermes_home / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return secrets_dir
