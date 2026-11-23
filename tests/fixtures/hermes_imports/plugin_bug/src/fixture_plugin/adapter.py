"""Fixture plugin with the slash confirm bug: the block below is the adapter's
import as it stood before 0.32.1 (and as the BGOS repo's pinned copy still
has it). Hermes never had ``gateway.slash_confirm``; the helper lives in
``tools.slash_confirm``. Parsed by the check, never imported."""

try:  # pragma: no cover - exercised only when Hermes is installed
    from tools.approval import resolve_gateway_approval  # type: ignore
except ImportError:
    def resolve_gateway_approval(session_key: str, choice: str) -> None:  # type: ignore[no-redef]
        raise RuntimeError("Hermes not installed")


try:  # pragma: no cover - exercised only when Hermes is installed
    # Phase 1 stub; replace import target when Hermes ships its slash_confirm
    # helper. Telegram has its own override at gateway/platforms/telegram.py:2119
    # but the fork hasn't surfaced a shared resolver path yet, tests monkeypatch
    # this at the module level (hermes_channel_bgos.bgos_adapter.resolve_slash_confirm).
    from gateway.slash_confirm import resolve as resolve_slash_confirm  # type: ignore
except ImportError:
    def resolve_slash_confirm(session_key: str, confirm_id: str, choice: str) -> None:  # type: ignore[no-redef]
        raise RuntimeError(
            "gateway.slash_confirm.resolve is not importable, either Hermes "
            "is not installed, or this Hermes build hasn't shipped the slash-"
            "confirm helper yet (Phase 1 stub in hermes_channel_bgos.bgos_adapter). "
            "Tests should monkeypatch this at the module level."
        )
