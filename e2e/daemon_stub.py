"""Deterministic Hermes-daemon stand-in for the two-profile routing e2e.

Runs the REAL vendor adapter (config resolution from $HERMES_HOME secrets,
REST + Socket.IO against the local backend, inbound pipeline, outbound
send()) with the LLM/Hermes-core layer replaced by a scripted brain, so the
proof is about ROUTING, deterministically.

Modes (env):
  BGOS_E2E_IDENTITY   - name this daemon answers with ("Achilles"/"Shadow").
  BGOS_E2E_GATEWAY_SIM=1 - simulate the Hermes gateway dispatch layer:
    install a fake `hermes_cli.profiles` (a `shadow` profile exists), a
    stand-in gateway MessageEvent/MessageType and build_source, so the
    adapter's profile stamping runs exactly as it would inside a real
    multiplexed gateway. Replies carry `served-by-profile=<stamp>` which is
    what Hermes's `_resolve_profile_home_for_source` would serve the turn
    from.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any

IDENTITY = os.environ.get("BGOS_E2E_IDENTITY", "Agent")
GATEWAY_SIM = bool(os.environ.get("BGOS_E2E_GATEWAY_SIM"))

if GATEWAY_SIM:
    # Must be installed BEFORE the adapter imports so profile_for_route's
    # `from hermes_cli.profiles import ...` resolves to the fake registry.
    hermes_cli = types.ModuleType("hermes_cli")
    profiles_mod = types.ModuleType("hermes_cli.profiles")

    def normalize_profile_name(name: str) -> str:
        return name.strip().lower()

    def profile_exists(name: str) -> bool:
        return name == "shadow"

    profiles_mod.normalize_profile_name = normalize_profile_name
    profiles_mod.profile_exists = profile_exists
    hermes_cli.profiles = profiles_mod
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.profiles"] = profiles_mod

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module  # noqa: E402
from hermes_channel_bgos.bgos_adapter import BGOSAdapter  # noqa: E402


class SimMessageType(Enum):
    TEXT = "text"
    COMMAND = "command"
    VOICE = "voice"


@dataclass
class SimGatewayEvent:
    text: str
    message_type: SimMessageType
    source: Any
    message_id: str | None = None
    raw_message: Any = None
    internal: bool = False
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)


async def main() -> None:
    adapter = BGOSAdapter(None)  # resolve from $HERMES_HOME/secrets/bgos.json

    if GATEWAY_SIM:
        bgos_adapter_module._GatewayMessageEvent = SimGatewayEvent
        bgos_adapter_module._GatewayMessageType = SimMessageType

        def build_source(*, chat_id: str, user_id: Any = None) -> SimpleNamespace:
            # Mirrors the real SessionSource surface the stamping touches.
            return SimpleNamespace(chat_id=chat_id, user_id=user_id, profile=None)

        adapter.build_source = build_source

        async def handle_message(event: Any) -> None:
            if getattr(event, "internal", False):
                return
            source = getattr(event, "source", None)
            chat_id = int(getattr(source, "chat_id", 0) or 0)
            if not chat_id:
                return
            profile = getattr(source, "profile", None) or "active"
            await adapter.send(
                chat_id,
                f"served-by-profile={profile} (gateway-sim, {IDENTITY})",
            )

    else:

        async def handle_message(event: Any) -> None:
            chat_id = int(getattr(event, "chat_id", 0) or 0)
            if not chat_id:
                return
            route = getattr(event, "agent_route", "")
            # getattr fallback keeps this stub runnable on the pre-fix
            # adapter (no _hermes_home), so the e2e discriminates on
            # ROUTING behavior, not on the stub's own attribute access.
            home = getattr(adapter, "_hermes_home", os.environ.get("HERMES_HOME", ""))
            await adapter.send(
                chat_id,
                f"I am {IDENTITY} (route={route}, home={home})",
            )

    adapter.handle_message = handle_message

    ok = await adapter.connect()
    print(f"daemon {IDENTITY}: connect() -> {ok}", flush=True)
    if not ok:
        sys.exit(2)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
