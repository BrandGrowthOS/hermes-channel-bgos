"""Hermes profile awareness for the BGOS channel.

Two responsibilities, both born from the 2026-08-04 Achilles/Shadow bug
(two agents on one machine, message to Shadow answered by Achilles):

1. `profile_for_route` maps a BGOS `agent_route` to the Hermes profile of
   the same name. The adapter stamps that profile on `source.profile` before
   dispatch, which is the ONLY signal Hermes's multiplexer honors when
   deciding which profile's home (SOUL, config, skills, memory, credentials)
   serves the turn. Before this existed, the route resolved from whoami was
   dropped at the gateway-event boundary and every assistant on a pairing was
   served by the gateway's active profile.

2. `resolve_hermes_home` resolves the Hermes home through
   `hermes_constants.get_hermes_home()` when Hermes is importable, which
   honors the context-local override installed by
   `gateway.run._profile_runtime_scope`. A secondary-profile adapter created
   under that scope previously read `os.environ["HERMES_HOME"]` directly and
   silently loaded the DEFAULT profile's `secrets/bgos.json` - two profiles
   sharing one pairing token, every inbound answered twice. Outside Hermes
   (pair CLI, tests) the env var keeps working unchanged.

The route -> profile contract: routes are opaque strings chosen at pair time
(`BGOS_AGENTS="default:Achilles,shadow:Shadow"`). A route named `default`
(or one with no matching Hermes profile) is served by the gateway's active
profile, exactly as before. Any other route is served by the Hermes profile
of the SAME name when one exists. Serving more than one profile from a
single gateway additionally requires `gateway.multiplex_profiles: true` in
Hermes - `multi_route_warnings` tells the operator when that is missing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_ROUTE = "default"

# Routes already warned about (missing profile). Warn once per route per
# process so a busy chat does not flood the log with the same line.
_warned_routes: set[str] = set()


def reset_warned_routes() -> None:
    """Test hook: clear the warn-once memory."""
    _warned_routes.clear()


def resolve_hermes_home() -> Path:
    """Resolve the Hermes home, honoring Hermes's profile override.

    Order: `hermes_constants.get_hermes_home()` (context-local profile
    override -> HERMES_HOME env -> platform default) when Hermes is
    importable and healthy; else the `HERMES_HOME` env var; else
    `~/.hermes`. The fallback chain means behavior outside a Hermes process
    is byte-identical to the historical `os.environ` read.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore
    except Exception:
        get_hermes_home = None
    if get_hermes_home is not None:
        try:
            return Path(get_hermes_home())
        except Exception:
            log.debug(
                "hermes_constants.get_hermes_home() failed - falling back "
                "to the HERMES_HOME env var",
                exc_info=True,
            )
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def _load_profile_helpers() -> tuple[Callable[[str], str], Callable[[str], bool]] | None:
    """Return (normalize_profile_name, profile_exists) from Hermes, or None
    when Hermes is not importable (tests, standalone tools)."""
    try:
        from hermes_cli.profiles import (  # type: ignore
            normalize_profile_name,
            profile_exists,
        )
    except Exception:
        return None
    return normalize_profile_name, profile_exists


def profile_for_route(route: str | None, *, warn_missing: bool = True) -> str | None:
    """Map a BGOS agent_route to the Hermes profile that should serve it.

    Returns the normalized profile name when a Hermes profile of the same
    name exists, else None (serve from the active profile). The `default`
    route always returns None.

    `warn_missing` controls the once-per-route warning for a non-default
    route with NO matching profile. On a MULTI-route pairing that is the
    exact misconfiguration that makes an agent answer with the wrong
    identity and must be loud. On a single-agent pairing a non-default
    route served by the active profile is the correct, common topology
    (e.g. `hades:Hades` on a one-agent host), so callers pass
    `warn_missing=False` there to avoid crying wolf.
    """
    route = (route or "").strip()
    if not route or route == DEFAULT_ROUTE:
        return None
    helpers = _load_profile_helpers()
    if helpers is None:
        return None
    normalize, exists = helpers
    try:
        name = normalize(route)
    except Exception:
        if warn_missing and route not in _warned_routes:
            _warned_routes.add(route)
            log.warning(
                "BGOS agent_route %r is not a valid Hermes profile name - "
                "this agent will be served by the active profile",
                route,
            )
        return None
    if exists(name):
        return name
    if warn_missing and route not in _warned_routes:
        _warned_routes.add(route)
        log.warning(
            "BGOS agent_route %r has no matching Hermes profile - this agent "
            "will be served by the ACTIVE profile (wrong persona!). Create "
            "it with `hermes profile create %s` or rename the route.",
            route, name,
        )
    return None


def multi_route_warnings(
    routes: dict[int, str],
    *,
    multiplex_on: bool | None,
    profile_exists: Callable[[str], bool],
) -> list[str]:
    """Operator-facing misconfiguration report for a pairing's route map.

    `routes` is the assistant_id -> agent_route map built from whoami.
    `multiplex_on` is the gateway's `multiplex_profiles` flag, or None when
    the adapter cannot see the gateway config (then the multiplex check is
    skipped - never guess; the profile-existence check does not depend on
    gateway config and still runs). Returns human-readable warning lines;
    the caller logs them at WARNING.

    Everything is gated on the pairing exposing MORE THAN ONE distinct
    route: a single agent on a non-default route served by the active
    profile is a correct, common topology and must not be warned about.
    """
    warnings: list[str] = []
    distinct = {
        (r or "").strip() for r in routes.values() if (r or "").strip()
    }
    if len(distinct) < 2:
        return warnings
    non_default = sorted(r for r in distinct if r != DEFAULT_ROUTE)
    if not non_default:
        return warnings
    missing = [r for r in non_default if not profile_exists(r)]
    for route in missing:
        warnings.append(
            f"agent_route '{route}' has no Hermes profile named '{route}' - "
            f"messages to this agent will be answered by the ACTIVE profile "
            f"(wrong persona). Fix: `hermes profile create {route}` and give "
            f"it its own SOUL.md/config."
        )
    if multiplex_on is False:
        warnings.append(
            "this pairing exposes agent route(s) "
            + ", ".join(f"'{r}'" for r in non_default)
            + " but gateway.multiplex_profiles is OFF - Hermes serves every "
            "agent from the active profile regardless of routing. Fix: "
            "`hermes config set gateway.multiplex_profiles true` and restart "
            "the gateway."
        )
    return warnings
