"""Adapter configuration — a plain dataclass passed around by value."""
from __future__ import annotations

from dataclasses import dataclass

# The BGOS client (`bgos_api.BgosApi._request`) always appends full paths that
# already carry the API prefix (e.g. `/api/v1/integrations/me`). A base_url
# that itself ends in `/api/v1` therefore doubles the prefix and every call
# 404s (`/api/v1/api/v1/...`). Seen in the wild on a fresh macOS install where
# the operator pasted the app-facing API base (which includes `/api/v1`) as
# the backend base URL.
_API_PREFIX = "/api/v1"


def normalize_base_url(url: str) -> str:
    """Normalize a BGOS backend base URL to origin form.

    - strips surrounding whitespace
    - strips trailing slashes
    - strips a trailing `/api/v1` (repeatedly, case-insensitive), since the
      API client appends `/api/v1/...` paths itself

    `"https://api.brandgrowthos.ai/api/v1"` → `"https://api.brandgrowthos.ai"`.
    Values already in origin form pass through unchanged.
    """
    u = (url or "").strip()
    while True:
        u = u.rstrip("/")
        if u.lower().endswith(_API_PREFIX):
            u = u[: -len(_API_PREFIX)]
            continue
        return u


@dataclass(frozen=True)
class BgosConfig:
    base_url: str
    pairing_token: str | None
    device_label: str = ""
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        # Self-heal any persisted/env base_url that carries the `/api/v1`
        # suffix (or a trailing slash) - every consumer builds requests off
        # this config, so normalizing here fixes them all at once.
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
