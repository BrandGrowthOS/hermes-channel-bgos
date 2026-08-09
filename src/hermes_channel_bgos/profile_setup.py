"""Local work for a server-dispatched `add_profile` frame (the one-click
new-Hermes-agent flow, existing-host path).

Division of labor: the BGOS backend has already created the assistant and
bound it to THIS pairing under a fresh agent_route; what is missing is the
host-side topology for that route, exactly the pieces the topology guard
(`topology.py`) checks:

1. a Hermes profile named after the route (persona home: SOUL, config,
   memory), and
2. the route in `BGOS_AGENTS` in `$HERMES_HOME/.env`, so a gateway restart
   rebuilds the same catalog instead of shrinking it back.

Deliberately NOT done here: writing any per-profile `secrets/bgos.json`.
The one-pairing topology serves every route through the default pairing;
a per-profile pairing file is the stray-secrets FAIL class (double answers).

`multiplex_active` is the RUNNING gateway's `gateway.multiplex_profiles`
flag, passed in by the adapter. When it is off, the route stamp is ignored
by the runner until a restart, so the new agent would answer with the
ACTIVE profile's persona: the result says so honestly via
`restart_required` instead of letting the wrong-persona state ship silently
(the 2026-08-04 Achilles/Shadow class).

Everything Hermes-specific is injectable so the module tests run without a
Hermes checkout, matching the package's standalone contract.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .agents import parse_agents_spec
from .hermes_profiles import DEFAULT_ROUTE

log = logging.getLogger(__name__)

# Mirrors hermes_cli.profiles._PROFILE_ID_RE: the route doubles as the
# profile directory name, so it must be a valid Hermes profile id.
_ROUTE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class AddProfileResult:
    ok: bool
    profile: str | None = None
    agents_spec: str | None = None
    multiplex: bool | None = None
    restart_required: bool = False
    error_code: str | None = None
    error_message: str | None = None

    def to_wire(self) -> dict:
        """The REST result body for POST profile-rpc/:rpcId/result."""
        if not self.ok:
            return {
                "ok": False,
                "error": {
                    "code": self.error_code or "add_profile_failed",
                    "message": (self.error_message or "")[:300],
                },
            }
        return {
            "ok": True,
            "payload": {
                "profile": self.profile,
                "agents_spec": self.agents_spec,
                "multiplex": self.multiplex,
                "restart_required": self.restart_required,
            },
        }


def _default_create_profile(name: str) -> Path:
    """Create the profile through Hermes's own bootstrapper (dirs, wrapper,
    skill seeding). Only reachable inside a Hermes install."""
    from hermes_cli.profiles import create_profile  # type: ignore

    return Path(create_profile(name, no_alias=True))


def _seed_soul(profile_dir: Path, name: str, *, created_now: bool) -> None:
    """Make sure the profile answers AS the named agent.

    - No SOUL.md: write a minimal persona.
    - SOUL.md exists and we created the profile IN THIS CALL: it is the
      bootstrapper's generic template (hermes_cli.create_profile seeds one),
      so append the identity paragraph. Without it the new agent introduces
      itself as "Hermes Agent" (found live 2026-08-09).
    - SOUL.md exists on a PRE-EXISTING profile: an operator's persona,
      never touched.
    """
    soul = profile_dir / "SOUL.md"
    identity = (
        f"\n\n# {name}\n\n"
        f"You are {name}, an agent on this Hermes gateway, reachable "
        f"through the BGOS (Home of Agents) app. Answer as {name}; your "
        f"owner can refine this persona any time by editing this file.\n"
    )
    if not soul.exists():
        soul.write_text(identity.lstrip(), encoding="utf-8")
        return
    if created_now:
        with soul.open("a", encoding="utf-8") as fh:
            fh.write(identity)


def _merge_agents_env(env_file: Path, route: str, name: str) -> str:
    """Append `route:name` to BGOS_AGENTS in `env_file`, preserving every
    other line. Returns the merged spec. Idempotent for a known route."""
    lines: list[str] = []
    current = ""
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("BGOS_AGENTS="):
                current = line.split("=", 1)[1]
            else:
                lines.append(line)
    entries = parse_agents_spec(current)
    if not any(e["agent_route"] == route for e in entries):
        entries.append({"agent_route": route, "name": name})
    spec = ",".join(f"{e['agent_route']}:{e['name']}" for e in entries)
    lines.append(f"BGOS_AGENTS={spec}")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return spec


def apply_add_profile(
    payload: dict,
    *,
    hermes_home: Path,
    create_profile: Callable[[str], Path] | None = None,
    multiplex_active: bool | None = None,
) -> AddProfileResult:
    """Perform the local half of add_profile. Synchronous disk work; the
    adapter runs it in a worker thread. Never raises: every failure is an
    error result so the backend RPC always settles."""
    route = str(payload.get("agent_route") or "").strip()
    name = str(payload.get("name") or "").strip() or route
    if not _ROUTE_RE.match(route) or route == DEFAULT_ROUTE:
        return AddProfileResult(
            ok=False,
            error_code="invalid_route",
            error_message=(
                f"agent_route {route!r} is not a valid Hermes profile id "
                f"(lowercase alphanumeric, hyphens, underscores; not "
                f"'{DEFAULT_ROUTE}')"
            ),
        )

    create = create_profile or _default_create_profile
    profile_dir = hermes_home / "profiles" / route
    created_now = False
    try:
        if not profile_dir.is_dir():
            created_dir = create(route)
            created_now = True
            if created_dir is not None:
                profile_dir = Path(created_dir)
        _seed_soul(profile_dir, name, created_now=created_now)
    except Exception as exc:
        log.exception("add_profile: creating profile %r failed", route)
        return AddProfileResult(
            ok=False,
            error_code="profile_create_failed",
            error_message=str(exc),
        )

    try:
        spec = _merge_agents_env(hermes_home / ".env", route, name)
    except Exception as exc:
        log.exception("add_profile: updating BGOS_AGENTS failed")
        return AddProfileResult(
            ok=False,
            error_code="env_update_failed",
            error_message=str(exc),
        )

    multiplex = bool(multiplex_active)
    return AddProfileResult(
        ok=True,
        profile=route,
        agents_spec=spec,
        multiplex=multiplex,
        restart_required=not multiplex,
    )
