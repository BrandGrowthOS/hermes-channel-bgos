"""Hermes plugin registration hooks for the BGOS platform.

The plugin directory (`plugins/platforms/bgos/adapter.py`) is a thin shim that
imports these from the installed pip package and passes them to
`ctx.register_platform(...)`. Keeping the hooks here (not in the plugin dir)
means they're versioned and unit-tested with the rest of the package.

A single `ctx.register_platform()` call replaces the entire fork patch on
plugin-capable Hermes; the patch stays as a fallback for older installs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .bgos_api import BgosApi, BgosApiError
from .config import BgosConfig

_PROD_BASE_URL = "https://api.brandgrowthos.ai"


def _secrets_path() -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "secrets" / "bgos.json"


def resolve_pairing() -> tuple[str | None, str]:
    """Resolve (pairing_token, base_url) from env + the secrets file — the same
    precedence the adapter uses (`BGOS_API_KEY` → secrets token; `BGOS_BACKEND_URL`
    → secrets base_url → prod default). Returns token=None when not paired.
    Standalone (no adapter import) so it works in the cron path and in tests."""
    secrets: dict = {}
    sp = _secrets_path()
    if sp.is_file():
        try:
            secrets = json.loads(sp.read_text())
        except (OSError, ValueError):
            secrets = {}
    token = os.environ.get("BGOS_API_KEY") or secrets.get("pairing_token")
    base_url = (
        os.environ.get("BGOS_BACKEND_URL")
        or secrets.get("base_url")
        or _PROD_BASE_URL
    )
    return token, base_url


def env_enablement() -> dict | None:
    """Seed `PlatformConfig.extra` from env during gateway config load (before
    adapter construction), so `hermes gateway status` reflects env-only setups.
    Returns None when BGOS isn't minimally configured (no pairing token). The
    special `home_channel` key is promoted to a `HomeChannel` dataclass by the
    plugin registry's core hook. Mirrors ntfy's `_env_enablement`."""
    token, base_url = resolve_pairing()
    if not token:
        return None
    seed: dict[str, Any] = {"backend_url": base_url}
    home = os.environ.get("BGOS_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.environ.get("BGOS_HOME_CHANNEL_NAME", home),
        }
    return seed


async def standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict:
    """Out-of-process send for cron / `send_message_tool` when the gateway
    runner isn't in this process. Without this hook, `deliver=bgos` cron jobs
    fail with "No live adapter for platform" — and historically the fork left
    bgos sending unimplemented ("Direct sending not yet implemented for bgos").

    `thread_id` / `media_files` / `force_document` are accepted for signature
    parity with the registry's sender protocol; BGOS delivers text here.
    """
    token, base_url = resolve_pairing()
    if not token:
        return {"error": "bgos standalone send: not paired (no token)"}
    api = BgosApi(BgosConfig(base_url=base_url, pairing_token=token))
    try:
        resp = await api.post_message(chat_id=int(chat_id), text=message)
    except BgosApiError as exc:
        return {"error": f"bgos standalone send: HTTP {exc.status}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"bgos standalone send failed: {exc.__class__.__name__}"}
    finally:
        await api.close()
    msg_id = resp.get("id") if isinstance(resp, dict) else None
    return {"success": True, "platform": "bgos", "chat_id": chat_id, "message_id": msg_id}


# Set in Task 3 (extracted verbatim from the fork patch's PLATFORM_HINTS entry).
BGOS_PLATFORM_HINT = ""
