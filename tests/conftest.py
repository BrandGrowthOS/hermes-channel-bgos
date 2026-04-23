"""Pytest fixtures shared across the hermes-channel-bgos test suite."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.mocks.mock_bgos_server import MockBgosServer


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
    """Monkeypatch HERMES_HOME to a tmp dir and return the secrets/ subdir."""
    hermes_home = tmp_path / "hermes_home"
    secrets_dir = hermes_home / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return secrets_dir
