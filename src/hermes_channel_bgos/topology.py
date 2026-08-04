"""Install-time topology guard for multi-agent Hermes hosts.

Born from the second half of the 2026-08-04 Achilles/Shadow incident: the
0.22.0 code fix (route -> profile stamping) shipped, yet the bug survived on
the operator's machine because the TOPOLOGY around the code was broken and
nothing checked it at the moment it was created. Four misconfigurations had
to be repaired by hand, plus two deeper layers found later:

1. `gateway.multiplex_profiles` was off, so Hermes served every turn from
   the active profile regardless of the route stamp.
2. No Hermes profile named after the non-default route existed (or it had
   no SOUL.md), so the route fell back to the active profile: message to
   Shadow, answered by Achilles.
3. A stale `profiles/<route>/secrets/bgos.json` from earlier experiments
   made a second adapter connect with the default profile's pairing:
   every reply arrived twice.
4. A re-pair leftover: TWO active pairings carrying the same agent
   catalog. Also answers everything twice.
5. Hermes's prompt-injection scanner silently replaced a SOUL.md that
   tripped a threat pattern with a `[BLOCKED: ...]` placeholder, so the
   persona was empty even with routing fixed.
6. Hermes reuses a continuing session's STORED system prompt byte for
   byte, so sessions created while any of the above was broken keep
   replaying the bad prompt after the fix ("the fix did not take").

This module holds the pure, injectable checks. `pair_cli` enforces them at
pair time (fail loud, never silently proceed); `doctor` re-runs them any
time. Nothing here imports Hermes at module level - every Hermes touchpoint
is a guarded runtime import or an injected callable, matching the package's
standalone-CLI contract.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .hermes_profiles import DEFAULT_ROUTE

log = logging.getLogger(__name__)

FAIL = "FAIL"
WARN = "WARN"

# Mirrors gateway.config's recognized tokens for the operator env override.
_MULTIPLEX_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MULTIPLEX_FALSY = frozenset({"0", "false", "no", "off"})

# The placeholder Hermes's prompt_builder substitutes for a context file the
# threat scanner rejected (agent/prompt_builder.py in Hermes core).
BLOCKED_MARKER = "[BLOCKED:"


@dataclass
class TopologyFinding:
    """One actionable misconfiguration. `severity` is FAIL (this topology
    WILL misbehave) or WARN (suspicious, may be intentional)."""

    check: str
    severity: str
    detail: str
    fix: str = ""


def distinct_routes(routes: Iterable[str]) -> set[str]:
    """Normalized distinct route set (blank entries dropped)."""
    return {(r or "").strip().lower() for r in routes if (r or "").strip()}


def profile_dir(hermes_home: Path, route: str) -> Path:
    """Disk location of the Hermes profile serving `route`.

    Mirrors hermes_cli.profiles.get_profile_dir: the `default` profile IS
    the Hermes home; named profiles live under `profiles/<name>/`.
    """
    name = (route or "").strip().lower()
    if not name or name == DEFAULT_ROUTE:
        return hermes_home
    return hermes_home / "profiles" / name


def _parse_multiplex_from_yaml_text(text: str) -> bool | None:
    """Minimal, dependency-free read of `multiplex_profiles` from config.yaml.

    Honors both spellings Hermes accepts: top-level `multiplex_profiles:` and
    nested `gateway.multiplex_profiles`. Used only when PyYAML is not
    importable (standalone pair CLI outside Hermes's venv). Returns None when
    the key is absent.
    """
    in_gateway = False
    found: bool | None = None
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip())
        if indent == 0:
            in_gateway = stripped.strip() == "gateway:"
        key_line = stripped.strip()
        if key_line.startswith("multiplex_profiles:") and (
            indent == 0 or in_gateway
        ):
            value = key_line.split(":", 1)[1].strip().strip("'\"").lower()
            found = value in _MULTIPLEX_TRUTHY
    return found


def resolve_multiplex_flag(
    hermes_home: Path, env: Mapping[str, str] | None = None,
) -> bool | None:
    """Resolve `gateway.multiplex_profiles` the way the gateway does.

    Precedence (mirrors gateway.config): the GATEWAY_MULTIPLEX_PROFILES env
    override wins when set to a recognized token; otherwise config.yaml
    (top-level or nested under `gateway:`); otherwise the gateway default,
    which is False. Returns None only when config.yaml exists but cannot be
    read or parsed - unknown, never guessed.
    """
    if env is None:
        env = os.environ
    raw = (env.get("GATEWAY_MULTIPLEX_PROFILES") or "").strip().lower()
    if raw in _MULTIPLEX_TRUTHY:
        return True
    if raw in _MULTIPLEX_FALSY:
        return False
    cfg = hermes_home / "config.yaml"
    if not cfg.is_file():
        return False  # gateway default
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return False
        value = data.get("multiplex_profiles")
        if value is None:
            nested = data.get("gateway")
            if isinstance(nested, dict):
                value = nested.get("multiplex_profiles")
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().lower() in _MULTIPLEX_TRUTHY
        return bool(value)
    except ImportError:
        found = _parse_multiplex_from_yaml_text(text)
        return False if found is None else found
    except Exception:
        return None


def _read_pairing_id(secrets_file: Path) -> int | str | None:
    try:
        data = json.loads(secrets_file.read_text())
    except (OSError, ValueError):
        return None
    pid = data.get("pairing_id") if isinstance(data, dict) else None
    return pid


def find_profile_secrets(hermes_home: Path) -> list[Path]:
    """Every per-profile `secrets/bgos.json` under this Hermes home."""
    root = hermes_home / "profiles"
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    for child in children:
        candidate = child / "secrets" / "bgos.json"
        if candidate.is_file():
            out.append(candidate)
    return out


def local_topology_findings(
    hermes_home: Path,
    routes: Iterable[str],
    env: Mapping[str, str] | None = None,
) -> list[TopologyFinding]:
    """Disk-only topology checks for a pairing that declares `routes`.

    Route/profile/multiplex checks only apply when the pairing declares TWO
    OR MORE distinct routes (a single agent on a non-default route served by
    the active profile is a correct, common topology). The stray-secrets
    check runs for any route set: a per-profile bgos.json that shares the
    default pairing double-answers regardless of route count.
    """
    findings: list[TopologyFinding] = []
    distinct = distinct_routes(routes)
    multi_route = len(distinct) > 1
    non_default = sorted(r for r in distinct if r != DEFAULT_ROUTE)

    if multi_route:
        for route in non_default:
            pdir = profile_dir(hermes_home, route)
            if not pdir.is_dir():
                findings.append(TopologyFinding(
                    check="profile_missing",
                    severity=FAIL,
                    detail=(
                        f"route '{route}' has no Hermes profile at {pdir} - "
                        f"messages to this agent would be answered by the "
                        f"ACTIVE profile (wrong persona)"
                    ),
                    fix=(
                        f"hermes profile create {route}   "
                        f"(then give it its own SOUL.md)"
                    ),
                ))
                continue
            soul = pdir / "SOUL.md"
            try:
                soul_text = soul.read_text(errors="replace") if soul.is_file() else ""
            except OSError:
                soul_text = "unreadable"  # do not warn on a permission quirk
            if not soul_text.strip():
                findings.append(TopologyFinding(
                    check="soul_missing",
                    severity=WARN,
                    detail=(
                        f"profile '{route}' exists but has no SOUL.md - the "
                        f"agent would answer with a default persona"
                    ),
                    fix=f"write {soul}",
                ))
        multiplex = resolve_multiplex_flag(hermes_home, env)
        if multiplex is False:
            findings.append(TopologyFinding(
                check="multiplex_off",
                severity=FAIL,
                detail=(
                    "this pairing declares routes "
                    + ", ".join(f"'{r}'" for r in sorted(distinct))
                    + " but gateway.multiplex_profiles is OFF - Hermes would "
                    "serve every agent from the active profile regardless of "
                    "routing"
                ),
                fix=(
                    "hermes config set gateway.multiplex_profiles true   "
                    "(then restart the gateway)"
                ),
            ))
        elif multiplex is None:
            findings.append(TopologyFinding(
                check="multiplex_unknown",
                severity=WARN,
                detail=(
                    f"could not read {hermes_home / 'config.yaml'} to verify "
                    f"gateway.multiplex_profiles"
                ),
                fix="hermes config get gateway.multiplex_profiles",
            ))

    default_pairing_id = _read_pairing_id(hermes_home / "secrets" / "bgos.json")
    for secrets_file in find_profile_secrets(hermes_home):
        profile_name = secrets_file.parent.parent.name
        pid = _read_pairing_id(secrets_file)
        same_pairing = (
            pid is not None
            and default_pairing_id is not None
            and pid == default_pairing_id
        )
        routed = profile_name.lower() in distinct
        if same_pairing:
            findings.append(TopologyFinding(
                check="stray_secrets",
                severity=FAIL,
                detail=(
                    f"{secrets_file} holds the SAME pairing as "
                    f"{hermes_home / 'secrets' / 'bgos.json'} "
                    f"(pairing_id={pid}) - a second adapter would connect "
                    f"with it and every reply would arrive twice"
                ),
                fix=f"rm {secrets_file}",
            ))
        elif routed:
            findings.append(TopologyFinding(
                check="stray_secrets",
                severity=FAIL,
                detail=(
                    f"profile '{profile_name}' is a route on THIS pairing's "
                    f"catalog but carries its own pairing file "
                    f"{secrets_file} (pairing_id={pid}) - in the one-pairing "
                    f"topology this profile is served through the default "
                    f"pairing, and its own file makes a second adapter "
                    f"connect and answer the same chats"
                ),
                fix=(
                    f"rm {secrets_file}   (keep it ONLY when profile "
                    f"'{profile_name}' runs its own separate gateway that is "
                    f"NOT a route on this pairing)"
                ),
            ))
        else:
            findings.append(TopologyFinding(
                check="stray_secrets",
                severity=WARN,
                detail=(
                    f"profile '{profile_name}' carries its own pairing file "
                    f"{secrets_file} (pairing_id={pid}). Correct only when "
                    f"that profile runs its own separate gateway (topology "
                    f"B); a leftover from experiments double-answers"
                ),
                fix=f"if unintentional: rm {secrets_file}",
            ))
    return findings


def overlapping_pairing_findings(
    pairings: Iterable[Mapping],
    *,
    self_pairing_id: int | None,
    routes: Iterable[str],
    device_label: str | None = None,
) -> list[TopologyFinding]:
    """Report OTHER active pairings that would answer the same agents.

    `pairings` is the backend's GET /api/v1/integrations/pairings payload
    (active pairings only). A route overlap with another pairing means every
    inbound for that agent is dispatched to BOTH daemons: every reply
    arrives twice. A same-device-label pairing with no visible overlap is
    reported as a probable re-pair leftover.
    """
    findings: list[TopologyFinding] = []
    ours = distinct_routes(routes)
    for pairing in pairings:
        pid = pairing.get("id")
        if self_pairing_id is not None and pid == self_pairing_id:
            continue
        catalog = pairing.get("agent_catalog") or []
        theirs = distinct_routes(
            entry.get("agent_route", "") for entry in catalog
            if isinstance(entry, Mapping)
        )
        label = pairing.get("device_label") or "?"
        overlap = sorted(ours & theirs)
        if overlap:
            findings.append(TopologyFinding(
                check="duplicate_pairing",
                severity=FAIL,
                detail=(
                    f"another ACTIVE pairing (id={pid}, device "
                    f"'{label}') already serves route(s) "
                    + ", ".join(f"'{r}'" for r in overlap)
                    + " - every reply to these agents arrives twice while "
                    "both daemons run"
                ),
                fix=(
                    f"revoke the stale one in BGOS: Integrations -> Hermes "
                    f"-> Paired devices -> revoke '{label}' (pairing "
                    f"id={pid}) - or stop that daemon"
                ),
            ))
        elif device_label and label == device_label:
            findings.append(TopologyFinding(
                check="duplicate_pairing",
                severity=WARN,
                detail=(
                    f"another ACTIVE pairing (id={pid}) uses the same device "
                    f"label '{label}' - probably a re-pair leftover"
                ),
                fix=(
                    f"if the old daemon is gone, revoke pairing id={pid} in "
                    f"BGOS: Integrations -> Hermes -> Paired devices"
                ),
            ))
    return findings


def _default_soul_scanner() -> Callable[[str], list[str]] | None:
    """Hermes core's prompt-injection scanner, when importable.

    `tools.threat_patterns.scan_for_threats(content, scope="context")` is the
    exact scanner Hermes's prompt_builder runs over context files (SOUL.md
    included). Unimportable outside Hermes's venv - then the SOUL scan is
    skipped, never faked.
    """
    try:
        from tools.threat_patterns import scan_for_threats  # type: ignore
    except Exception:
        return None
    return lambda content: scan_for_threats(content, scope="context")


def blocked_soul_findings(
    hermes_home: Path,
    routes: Iterable[str],
    scanner: Callable[[str], list[str]] | None = None,
) -> list[TopologyFinding]:
    """Scan the active profile's and each routed profile's SOUL.md through
    Hermes's own threat scanner.

    A SOUL.md that trips the scanner is silently replaced with a
    `[BLOCKED: ...]` placeholder at prompt-build time (Hermes core,
    agent/prompt_builder.py), so the agent answers with no persona - the
    same "answers as the wrong agent" symptom as a routing bug, with none of
    the routing causes. Observed live: a SOUL.md containing the ordinary
    English phrase "controlled mythic weight" matched Hermes's
    known_c2_framework pattern (Mythic is a C2 brand) and was blocked for
    days. Returns [] when no scanner is available (outside Hermes's venv).
    """
    if scanner is None:
        scanner = _default_soul_scanner()
    if scanner is None:
        return []
    findings: list[TopologyFinding] = []
    seen: set[Path] = set()
    for route in [DEFAULT_ROUTE, *distinct_routes(routes)]:
        pdir = profile_dir(hermes_home, route)
        soul = pdir / "SOUL.md"
        if soul in seen or not soul.is_file():
            continue
        seen.add(soul)
        try:
            content = soul.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            hits = scanner(content)
        except Exception:
            log.debug("SOUL scan failed for %s", soul, exc_info=True)
            continue
        if hits:
            findings.append(TopologyFinding(
                check="soul_blocked",
                severity=FAIL,
                detail=(
                    f"{soul} trips Hermes's prompt-injection scanner "
                    f"({', '.join(hits)}) - Hermes silently replaces it with "
                    f"a [BLOCKED: ...] placeholder at prompt build, so this "
                    f"agent answers with NO persona"
                ),
                fix=(
                    f"reword the matched phrase in {soul} (e.g. a C2 brand "
                    f"name like 'mythic' used as an ordinary word), then "
                    f"clear stale session prompts (see the "
                    f"stale_blocked_sessions check)"
                ),
            ))
    return findings


def stale_blocked_session_finding(hermes_home: Path) -> TopologyFinding | None:
    """Count sessions in state.db whose STORED system prompt carries the
    [BLOCKED: placeholder.

    Hermes reuses a continuing session's stored system prompt byte for byte
    (agent/conversation_loop.py, prefix-cache stability), so a session
    created with a blocked SOUL or under the wrong profile replays that
    prompt forever - a correct fix "does not take" on existing chats. A NULL
    system_prompt makes the next turn rebuild it, which is the remediation.
    Returns None when state.db is absent or holds no affected session.
    """
    db = hermes_home / "state.db"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END), 0) "
                "FROM sessions WHERE system_prompt LIKE ?",
                (f"%{BLOCKED_MARKER}%",),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        log.debug("state.db scan failed for %s", db, exc_info=True)
        return None
    total, active = (row or (0, 0))
    if not total:
        return None
    severity = FAIL if active else WARN
    return TopologyFinding(
        check="stale_blocked_sessions",
        severity=severity,
        detail=(
            f"{total} session(s) in {db} ({active} still open) carry a "
            f"stored system prompt containing '{BLOCKED_MARKER}' - those "
            f"chats replay the blocked prompt on every turn even after the "
            f"SOUL.md is fixed"
        ),
        fix=(
            "after fixing the SOUL.md, stop the gateway and run: sqlite3 "
            f"{db} \"UPDATE sessions SET system_prompt = NULL WHERE "
            f"system_prompt LIKE '%{BLOCKED_MARKER}%'\" - the next turn "
            "rebuilds each prompt. Or send /new in each affected chat."
        ),
    )
