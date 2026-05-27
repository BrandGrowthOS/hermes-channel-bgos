"""Tests for the Hermes-plugin registration hooks."""
from __future__ import annotations

import json

import pytest

from hermes_channel_bgos.plugin import env_enablement, resolve_pairing


def test_resolve_pairing_from_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    monkeypatch.delenv("BGOS_BACKEND_URL", raising=False)
    secrets = tmp_path / "secrets" / "bgos.json"
    secrets.parent.mkdir(parents=True)
    secrets.write_text(json.dumps({"pairing_token": "tok", "base_url": "http://x"}))
    token, base_url = resolve_pairing()
    assert token == "tok"
    assert base_url == "http://x"


def test_resolve_pairing_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "envtok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://env")
    token, base_url = resolve_pairing()
    assert token == "envtok"
    assert base_url == "http://env"


def test_resolve_pairing_none_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    token, base_url = resolve_pairing()
    assert token is None
    assert base_url == "https://api.brandgrowthos.ai"  # prod default


def test_env_enablement_seeds_home_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_HOME_CHANNEL", "830")
    monkeypatch.setenv("BGOS_HOME_CHANNEL_NAME", "Ops")
    seed = env_enablement()
    assert seed is not None
    assert seed["home_channel"] == {"chat_id": "830", "name": "Ops"}


def test_env_enablement_none_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    assert env_enablement() is None
