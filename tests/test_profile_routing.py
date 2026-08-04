"""Multi-profile routing: BGOS agent_route -> Hermes profile stamping.

Root cause pinned 2026-08-04 (Achilles/Shadow on one machine): the adapter
resolves each assistant's agent_route from whoami but drops it on the floor
when wrapping the gateway MessageEvent - `build_source` receives only
chat_id/user_id, so Hermes's profile scoping (`source.profile` ->
`_resolve_profile_home_for_source` -> `_profile_runtime_scope`) never sees
which agent was addressed and EVERY assistant on the pairing is served by the
gateway's active profile. Message to Shadow, answered by Achilles.

These tests pin the fix: the route resolved for an inbound event is mapped to
the Hermes profile of the same name (when it exists) and stamped on
`source.profile` before `handle_message`, on every dispatch surface (plain
inbound, batched flush, inline-button click, synthetic boards/voice turns).
Also pinned: profile-aware HERMES_HOME resolution, so a secondary-profile
adapter created under Hermes's context-local home override reads ITS OWN
secrets/cursor instead of silently sharing the default profile's pairing.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
import hermes_channel_bgos.hermes_profiles as hermes_profiles
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.hermes_profiles import (
    multi_route_warnings,
    profile_for_route,
    resolve_hermes_home,
)
from tests.mocks.mock_hermes import (
    MessageEvent as GatewayMessageEvent,
    MessageType as GatewayMessageType,
)



# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_helpers(existing: set[str]):
    """Injectable (normalize, exists) pair mimicking hermes_cli.profiles."""
    def normalize(name: str) -> str:
        return name.strip().lower()

    def exists(name: str) -> bool:
        return name in existing

    return normalize, exists


def _patch_profiles(monkeypatch, existing: set[str]) -> None:
    normalize, exists = _fake_helpers(existing)
    monkeypatch.setattr(
        hermes_profiles, "_load_profile_helpers", lambda: (normalize, exists),
    )


def _make_adapter() -> BGOSAdapter:
    return BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))


def _make_gateway_adapter(monkeypatch) -> BGOSAdapter:
    """Adapter wired for the gateway-event dispatch path with a build_source
    that returns an attribute-bearing source (like the real SessionSource)."""
    adapter = _make_adapter()
    monkeypatch.setattr(
        bgos_adapter_module, "_GatewayMessageEvent", GatewayMessageEvent,
    )
    monkeypatch.setattr(
        bgos_adapter_module, "_GatewayMessageType", GatewayMessageType,
    )

    def build_source(*, chat_id: str, user_id: Any = None) -> SimpleNamespace:
        return SimpleNamespace(chat_id=chat_id, user_id=user_id, profile=None)

    monkeypatch.setattr(adapter, "build_source", build_source, raising=False)
    return adapter


def _capture(adapter: BGOSAdapter) -> list[Any]:
    received: list[Any] = []

    async def capture(event: Any) -> None:
        received.append(event)

    adapter.handle_message = capture
    return received


def _inbound(assistant_id: int, chat_id: int, message_id: int, text: str) -> dict:
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "user_id": "user_1",
        "assistant_id": assistant_id,
        "message_type": "standard",
    }


# ---------------------------------------------------------------------------
# profile_for_route
# ---------------------------------------------------------------------------

def test_default_route_maps_to_no_profile(monkeypatch) -> None:
    _patch_profiles(monkeypatch, {"shadow"})
    assert profile_for_route("default") is None
    assert profile_for_route("") is None
    assert profile_for_route(None) is None
    assert profile_for_route("   ") is None


def test_route_with_matching_profile_maps_to_it(monkeypatch) -> None:
    _patch_profiles(monkeypatch, {"shadow"})
    assert profile_for_route("shadow") == "shadow"


def test_route_is_normalized_before_lookup(monkeypatch) -> None:
    _patch_profiles(monkeypatch, {"shadow"})
    assert profile_for_route(" Shadow ") == "shadow"


def test_route_without_matching_profile_maps_to_none(monkeypatch, caplog) -> None:
    _patch_profiles(monkeypatch, {"other"})
    with caplog.at_level("WARNING"):
        assert profile_for_route("shadow") is None
    assert any("shadow" in r.message for r in caplog.records)


def test_missing_profile_warns_once_per_route(monkeypatch, caplog) -> None:
    _patch_profiles(monkeypatch, set())
    hermes_profiles.reset_warned_routes()
    with caplog.at_level("WARNING"):
        profile_for_route("ghost")
        profile_for_route("ghost")
    warnings = [r for r in caplog.records if "ghost" in r.message]
    assert len(warnings) == 1


def test_no_hermes_cli_means_no_stamp(monkeypatch) -> None:
    monkeypatch.setattr(hermes_profiles, "_load_profile_helpers", lambda: None)
    assert profile_for_route("shadow") is None


# ---------------------------------------------------------------------------
# resolve_hermes_home
# ---------------------------------------------------------------------------

def test_resolve_hermes_home_prefers_hermes_constants(monkeypatch, tmp_path) -> None:
    """When running inside Hermes, the context-local profile override
    (hermes_constants.get_hermes_home) must win over os.environ - this is
    what isolates a secondary multiplexed profile's secrets and cursor."""
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: tmp_path / "profiles" / "shadow"
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))
    assert resolve_hermes_home() == tmp_path / "profiles" / "shadow"


def test_resolve_hermes_home_falls_back_to_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "envhome"))
    assert resolve_hermes_home() == tmp_path / "envhome"


def test_resolve_hermes_home_defaults_to_dot_hermes(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert resolve_hermes_home() == Path.home() / ".hermes"


def test_resolve_hermes_home_survives_broken_hermes_constants(
    monkeypatch, tmp_path,
) -> None:
    def boom() -> Path:
        raise RuntimeError("hermes internals unhappy")

    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = boom
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "envhome"))
    assert resolve_hermes_home() == tmp_path / "envhome"


# ---------------------------------------------------------------------------
# adapter: secrets + cursor resolve through the profile-aware home
# ---------------------------------------------------------------------------

def test_resolve_config_reads_secrets_from_profile_home(
    monkeypatch, tmp_path,
) -> None:
    """A secondary-profile adapter constructed under Hermes's home override
    must read THAT profile's secrets file, not the process-env one. This is
    the duplicate-reply half of the Achilles/Shadow bug: both profiles were
    silently sharing ~/.hermes/secrets/bgos.json."""
    env_home = tmp_path / "root"
    profile_home = tmp_path / "profiles" / "shadow"
    for home, token in ((env_home, "pair_root"), (profile_home, "pair_shadow")):
        (home / "secrets").mkdir(parents=True)
        (home / "secrets" / "bgos.json").write_text(
            '{"pairing_token": "%s", "base_url": "http://x"}' % token,
        )
    monkeypatch.setenv("HERMES_HOME", str(env_home))
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: profile_home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)

    config = BGOSAdapter._resolve_config(None)
    assert config.pairing_token == "pair_shadow"


def test_last_id_path_is_captured_at_init_under_profile_home(
    monkeypatch, tmp_path,
) -> None:
    """The cursor path must bind to the adapter's own profile home at
    construction time, so a later context change (or a callback running
    outside the profile scope) cannot cross the cursor between profiles."""
    profile_home = tmp_path / "profiles" / "shadow"
    profile_home.mkdir(parents=True)
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: profile_home
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)

    adapter = _make_adapter()

    # Simulate the scope having been exited after construction.
    monkeypatch.delitem(sys.modules, "hermes_constants")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))

    assert adapter._last_id_path() == profile_home / "bgos_last_id"


# ---------------------------------------------------------------------------
# adapter: inbound dispatch stamps source.profile from the agent route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_stamps_profile_for_non_default_route(monkeypatch) -> None:
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(1005, "shadow")
    received = _capture(adapter)

    await adapter._handle_inbound(
        _inbound(1005, 3038, 200, "Who are you?"), batchable=False,
    )

    assert len(received) == 1
    assert received[0].source.profile == "shadow"


@pytest.mark.asyncio
async def test_inbound_leaves_default_route_unstamped(monkeypatch) -> None:
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(974, "default")
    received = _capture(adapter)

    await adapter._handle_inbound(
        _inbound(974, 1919, 201, "Who are you?"), batchable=False,
    )

    assert len(received) == 1
    assert received[0].source.profile is None


@pytest.mark.asyncio
async def test_inbound_does_not_override_existing_stamp(monkeypatch) -> None:
    """An operator's profile_routes (or a secondary adapter's ownership)
    already stamped the source - the route-derived stamp must yield."""
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(1005, "shadow")

    def build_source(*, chat_id: str, user_id: Any = None) -> SimpleNamespace:
        return SimpleNamespace(
            chat_id=chat_id, user_id=user_id, profile="operator-pick",
        )

    monkeypatch.setattr(adapter, "build_source", build_source, raising=False)
    received = _capture(adapter)

    await adapter._handle_inbound(
        _inbound(1005, 3038, 202, "hello"), batchable=False,
    )

    assert received[0].source.profile == "operator-pick"


@pytest.mark.asyncio
async def test_interleaved_assistants_each_get_their_own_profile(
    monkeypatch,
) -> None:
    """The KC scenario in miniature: Achilles (default) and Shadow (shadow)
    on ONE pairing, interleaved messages, each stamped for its own profile."""
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(974, "default")
    adapter._state.set_route(1005, "shadow")
    received = _capture(adapter)

    await adapter._handle_inbound(_inbound(1005, 3038, 300, "a"), batchable=False)
    await adapter._handle_inbound(_inbound(974, 1919, 301, "b"), batchable=False)
    await adapter._handle_inbound(_inbound(1005, 3038, 302, "c"), batchable=False)

    assert [e.source.profile for e in received] == ["shadow", None, "shadow"]


@pytest.mark.asyncio
async def test_batched_flush_stamps_profile(monkeypatch) -> None:
    """Rapid plain-text messages take the adaptive-batching flush path -
    the merged dispatch must carry the same stamp as the direct path."""
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(1005, "shadow")
    adapter._text_batch_window = 0.05
    received = _capture(adapter)

    await adapter._handle_inbound(_inbound(1005, 3038, 400, "part one"))
    await adapter._handle_inbound(_inbound(1005, 3038, 401, "part two"))
    await asyncio.sleep(0.4)

    assert len(received) == 1
    assert received[0].source.profile == "shadow"
    assert "part one" in received[0].text and "part two" in received[0].text


@pytest.mark.asyncio
async def test_click_dispatch_stamps_profile(monkeypatch) -> None:
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(1005, "shadow")
    received = _capture(adapter)

    await adapter._handle_inbound_click({
        "assistantId": 1005,
        "userId": "user_1",
        "chatId": 3038,
        "messageId": 500,
        "optionId": 1,
        "callbackData": "value_a",
        "buttonText": "Option A",
    })

    assert len(received) == 1
    assert received[0].source.profile == "shadow"


@pytest.mark.asyncio
async def test_boards_result_turn_stamps_addressed_assistants_profile(
    monkeypatch,
) -> None:
    """Synthetic turns must land in the SAME profile-scoped session as the
    inbound turn they answer, or the result would fork into a different
    conversation namespace."""
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(1005, "shadow")
    adapter._state.addressed_assistant_id_by_chat[3038] = 1005
    received = _capture(adapter)

    await adapter._dispatch_boards_result_turn(3038, "[BGOS boards result] ok")

    assert len(received) == 1
    assert received[0].source.profile == "shadow"


@pytest.mark.asyncio
async def test_voice_turn_stamps_addressed_assistants_profile(monkeypatch) -> None:
    _patch_profiles(monkeypatch, {"shadow"})
    adapter = _make_gateway_adapter(monkeypatch)
    adapter._state.set_route(1005, "shadow")
    adapter._state.addressed_assistant_id_by_chat[3038] = 1005
    received = _capture(adapter)

    await adapter._dispatch_voice_turn(3038, "[voice consult] hello")

    assert len(received) == 1
    assert received[0].source.profile == "shadow"


# ---------------------------------------------------------------------------
# multi_route_warnings - operator-facing misconfiguration report
# ---------------------------------------------------------------------------

def test_no_warnings_for_single_default_route(monkeypatch) -> None:
    _, exists = _fake_helpers({"shadow"})
    assert multi_route_warnings(
        {974: "default"}, multiplex_on=True, profile_exists=exists,
    ) == []


def test_warns_when_route_has_no_profile() -> None:
    _, exists = _fake_helpers(set())
    warnings = multi_route_warnings(
        {1005: "shadow"}, multiplex_on=True, profile_exists=exists,
    )
    assert len(warnings) == 1
    assert "shadow" in warnings[0]
    assert "hermes profile create" in warnings[0] or "profile" in warnings[0]


def test_warns_when_multiplex_off_with_non_default_routes() -> None:
    _, exists = _fake_helpers({"shadow"})
    warnings = multi_route_warnings(
        {974: "default", 1005: "shadow"},
        multiplex_on=False,
        profile_exists=exists,
    )
    assert any("multiplex_profiles" in w for w in warnings)


def test_unknown_multiplex_state_skips_the_multiplex_warning() -> None:
    _, exists = _fake_helpers({"shadow"})
    warnings = multi_route_warnings(
        {974: "default", 1005: "shadow"},
        multiplex_on=None,
        profile_exists=exists,
    )
    assert warnings == []
