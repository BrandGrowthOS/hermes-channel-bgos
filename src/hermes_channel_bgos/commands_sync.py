"""Slash-command manifest construction + native-command discovery.

The manifest BGOS stores for a given assistant is the merge of:
- Hermes's native slash commands (from `hermes_cli.commands` or equivalent —
  the exact discovery API is resolved at runtime; this module falls back to
  an empty list when Hermes isn't installed).
- Three curated bridge-locals handled adapter-side: `/new`, `/retry`,
  `/status`. See the design spec §4 for the policy.

Collision resolution: if Hermes defines a command with the same name as a
bridge-local (most likely `/status`), the bridge-local wins. The adapter is
the only layer with full-stack health visibility (BGOS + pairing + Hermes),
so its `/status` is more useful than Hermes's.
"""
from __future__ import annotations

BRIDGE_LOCAL_COMMANDS: dict[str, str] = {
    "new": "Start a fresh conversation in this chat (bridge).",
    "retry": "Resend the last message (bridge).",
    "status": "Show adapter + Hermes health (bridge).",
}


def build_manifest(native: list[dict]) -> list[dict]:
    """Merge `native` (from Hermes) with the bridge-local commands.

    - Bridge-local names always win over native names (collision resolution).
    - Native commands are deduplicated on name (first occurrence wins).
    - Descriptions are truncated to 100 chars (BGOS DB constraint).
    - Returns entries shaped `{command, description, scope}` with scope="all".

    Order: deduped native commands first (in their original order), then the
    three bridge-locals appended in insertion order.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for entry in native:
        name = entry.get("command", "").strip().lower()
        if not name or name in seen or name in BRIDGE_LOCAL_COMMANDS:
            continue
        description = (entry.get("description", "") or "")[:100]
        out.append({"command": name, "description": description, "scope": "all"})
        seen.add(name)
    for name, desc in BRIDGE_LOCAL_COMMANDS.items():
        out.append({"command": name, "description": desc, "scope": "all"})
    return out


def fetch_hermes_native_commands(agent_route: str) -> list[dict]:
    """Return Hermes's native slash manifest for the given agent.

    The exact internal API on Hermes is TBD (confirmed in Phase 4 against the
    real repo). For now, attempt a best-effort import and fall back to an
    empty list so the adapter still functions with only bridge-locals.
    """
    try:  # pragma: no cover - exercised only when Hermes is installed
        from hermes_cli.commands import list_commands  # type: ignore
    except ImportError:
        return []
    try:  # pragma: no cover
        return [
            {"command": c.name, "description": getattr(c, "description", "") or ""}
            for c in list_commands(agent_route)
        ]
    except Exception:  # pragma: no cover
        # Defensive: if Hermes's API shape differs from what we guess, degrade
        # gracefully rather than crashing the manifest sync.
        return []
