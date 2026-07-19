"""Agent-catalog spec parsing — shared by the pair CLI (`--agents`), the
adapter's catalog push (`BGOS_AGENTS` / `BGOS_AGENTS_JSON`), and the doctor.

Single source of truth for the `route:Display Name` comma format so the CLI,
adapter, and diagnostics never disagree on what a spec string means.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping

log = logging.getLogger(__name__)


def parse_agents_spec(raw: str) -> list[dict]:
    """Parse a comma-separated `route:Display Name` spec into catalog entries.

    - `"hades:Hades,ramy:Ramy"` → two entries.
    - A bare route with no colon uses the route as both route and name.
    - Whitespace around pieces, routes, and names is stripped.
    - Empty pieces (e.g. a trailing comma) are skipped.
    - An empty/blank string yields `[]`.

    Returns entries shaped `{"agent_route": str, "name": str}` — the shape the
    backend's agent-catalog endpoint expects.
    """
    out: list[dict] = []
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            route, name = piece.split(":", 1)
            route = route.strip()
            name = name.strip() or route
        else:
            route = piece
            name = piece
        if route:
            out.append({"agent_route": route, "name": name})
    return out


def enumerate_agents_from_env(env: Mapping[str, str] | None = None) -> list[dict]:
    """Discover configured agents from env. First non-empty source wins:

    1. `BGOS_AGENTS_JSON` — JSON list of `{"agent_route", "name", ...}` dicts.
    2. `BGOS_AGENTS` — comma-separated `route:Display Name` (see parse_agents_spec).

    Returns `[]` when neither is set.

    `env` defaults to `os.environ`; the doctor passes the gateway-effective
    environment (process env overlaid with `$HERMES_HOME/.env`) so its report
    matches what the running gateway actually sees.
    """
    if env is None:
        env = os.environ
    raw_json = env.get("BGOS_AGENTS_JSON", "").strip()
    if raw_json:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            log.warning("BGOS_AGENTS_JSON is not valid JSON — ignoring")
        else:
            if isinstance(data, list):
                out = [e for e in data if isinstance(e, dict) and e.get("agent_route")]
                if out:
                    return out
    return parse_agents_spec(env.get("BGOS_AGENTS", ""))
