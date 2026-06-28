"""Regression test for the bgos:943 home-target resolution fix (2026-06-27).

Server-authoritative chat addressing rejects any outbound to a chat the
adapter never received inbound (``received_chat_ids``). The gateway's
restart/shutdown/startup lifecycle notices target the operator-configured BGOS
home channel (env ``BGOS_HOME_CHANNEL``, e.g. 943). On a fresh process
``received_chat_ids`` is empty, so the first home notice after a restart failed
with ``unknown_chat_target`` and the gateway fell through to the Telegram home.

``BGOSAdapter._seed_home_channel_target()`` seeds the configured home chat as a
trusted outbound target so those notices resolve.
"""
import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter, UnknownChatTarget
from hermes_channel_bgos.state_store import StateStore


def _bare_adapter() -> BGOSAdapter:
    adapter = object.__new__(BGOSAdapter)
    adapter._state = StateStore()
    return adapter


def test_home_channel_seeded_as_trusted_target(monkeypatch):
    monkeypatch.setenv("BGOS_HOME_CHANNEL", "943")
    adapter = _bare_adapter()

    adapter._seed_home_channel_target()

    assert adapter._state.has_received_chat(943)
    # Outbound to the home chat now resolves instead of unknown_chat_target.
    chat_key, handle = adapter._resolve_outbound_target("943")
    assert chat_key == 943
    assert handle is None


def test_home_resolution_fails_without_seeding(monkeypatch):
    monkeypatch.setenv("BGOS_HOME_CHANNEL", "943")
    adapter = _bare_adapter()

    # Without the seed, the home chat is unknown — this is the pre-fix failure.
    with pytest.raises(UnknownChatTarget):
        adapter._resolve_outbound_target("943")


def test_seed_noop_when_home_unset(monkeypatch):
    monkeypatch.delenv("BGOS_HOME_CHANNEL", raising=False)
    adapter = _bare_adapter()

    adapter._seed_home_channel_target()

    assert adapter._state.received_chat_ids == set()


def test_seed_ignores_non_numeric_home(monkeypatch):
    monkeypatch.setenv("BGOS_HOME_CHANNEL", "not-a-number")
    adapter = _bare_adapter()

    adapter._seed_home_channel_target()

    assert adapter._state.received_chat_ids == set()
