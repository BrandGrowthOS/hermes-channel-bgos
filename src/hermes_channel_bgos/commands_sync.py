"""Slash-command manifest construction + native-command discovery.

The manifest BGOS stores for a given assistant is the merge of:
- Hermes's native slash commands (from `hermes_cli.commands` or equivalent —
  the exact discovery API is resolved at runtime; this module falls back to
  an empty list when Hermes isn't installed).
- Four curated bridge-locals handled adapter-side: `/new`, `/retry`,
  `/status`, `/quiet`. See the design spec §4 for the policy.

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
    "quiet": "Show or change how much behind-the-scenes work shows in this chat",
}


def build_manifest(native: list[dict]) -> list[dict]:
    """Merge `native` (from Hermes) with the bridge-local commands.

    - Bridge-local names always win over native names (collision resolution).
    - Native commands are deduplicated on name (first occurrence wins).
    - Descriptions are truncated to 100 chars (BGOS DB constraint).
    - Returns entries shaped `{command, description, scope}` with scope="all".

    Order: deduped native commands first (in their original order), then the
    four bridge-locals appended in insertion order.
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
    """Return Hermes's gateway-available slash manifest for ``agent_route``.

    BGOS stores this list for the selected assistant and uses it to populate
    the composer slash picker.  The source of truth in modern Hermes is
    ``hermes_cli.commands.COMMAND_REGISTRY``.  Older drafts of this plugin
    tried to import a non-existent ``list_commands`` helper, so discovery
    always fell back to ``[]`` and BGOS showed only the three bridge-local
    commands.  Keep this function intentionally defensive so command sync
    never prevents the adapter from connecting.
    """
    del agent_route  # Hermes slash commands are currently profile-wide.

    try:  # pragma: no cover - covered with synthetic modules in tests
        from hermes_cli import commands as hc  # type: ignore
    except Exception:
        return []

    entries: list[dict] = []
    seen: set[str] = set()

    def add(name: str, description: str) -> None:
        normalized = (name or "").strip().lower().lstrip("/")
        if not normalized or normalized in seen:
            return
        entries.append({"command": normalized, "description": description or f"Run /{normalized}"})
        seen.add(normalized)

    try:
        registry = list(getattr(hc, "COMMAND_REGISTRY", []) or [])
        resolve_gates = getattr(hc, "_resolve_config_gates", None)
        overrides = resolve_gates() if callable(resolve_gates) else set()
        is_available = getattr(hc, "_is_gateway_available", None)
        build_description = getattr(hc, "_build_description", None)

        for cmd in registry:
            if callable(is_available) and not is_available(cmd, overrides):
                continue
            elif not callable(is_available) and getattr(cmd, "cli_only", False) and not getattr(cmd, "gateway_config_gate", None):
                continue

            name = getattr(cmd, "name", "")
            if callable(build_description):
                desc = build_description(cmd)
            else:
                args_hint = getattr(cmd, "args_hint", "") or ""
                base = getattr(cmd, "description", "") or f"Run /{name}"
                desc = f"{base} (usage: /{name} {args_hint})" if args_hint else base
            add(name, str(desc))

            aliases = getattr(cmd, "aliases", ()) or ()
            if isinstance(aliases, (list, tuple, set)):
                for alias in aliases:
                    base = getattr(cmd, "description", "") or f"Run /{name}"
                    add(str(alias), f"{base} (alias for /{name})")

        iter_plugins = getattr(hc, "_iter_plugin_command_entries", None)
        if callable(iter_plugins):
            for name, description, args_hint in iter_plugins() or []:
                suffix = f" (usage: /{name} {args_hint})" if args_hint else ""
                add(name, f"{description}{suffix}")

        return entries
    except Exception:  # pragma: no cover
        # Last-ditch compatibility with older Hermes versions that expose only
        # COMMANDS = {'/help': '...'}; less precise, but still better than an
        # empty native manifest.
        try:
            for name, description in (getattr(hc, "COMMANDS", {}) or {}).items():
                add(name, str(description or ""))
            return entries
        except Exception:
            return []
