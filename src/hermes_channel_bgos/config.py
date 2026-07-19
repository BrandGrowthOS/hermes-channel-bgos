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


# Pairing tokens issued by the backend carry this prefix (e.g. pair_9dB...).
# A BGOS USER api key does not. The distinction lets us detect a stale
# BGOS_API_KEY env export silently shadowing a freshly paired secrets-file
# token (seen in the wild: whoami 401 forever, doctor could not explain it).
PAIRING_TOKEN_PREFIX = "pair_"

# Display-safe token truncation length. Never print more of a token than this.
TOKEN_DISPLAY_CHARS = 8


def looks_like_pairing_token(token: str | None) -> bool:
    """True when the value has the backend-issued pairing-token shape."""
    return bool(token) and token.startswith(PAIRING_TOKEN_PREFIX)


def redact_token(token: str | None, keep: int = TOKEN_DISPLAY_CHARS) -> str:
    """Display-safe token prefix; never returns the full secret.

    Tokens at or under the display budget are cut harder so even a short
    value is never echoed back whole.
    """
    if not token:
        return ""
    if len(token) <= keep:
        return token[: max(0, len(token) - 2)] + "..."
    return token[:keep] + "..."


@dataclass(frozen=True)
class TokenChoice:
    """Outcome of the env-vs-secrets pairing-token precedence decision.

    `source` is one of "env", "secrets", "none". `ignored_env_token` is set
    when a non-pairing env value was bypassed in favor of the secrets token,
    so callers can log or report the shadowing honestly.
    """
    token: str | None
    source: str
    ignored_env_token: str | None = None


def choose_pairing_token(
    env_token: str | None, secrets_token: str | None,
) -> TokenChoice:
    """Decide which token to use, honestly.

    Rules:
    - An env token with the pair_ prefix is an explicit override and wins.
    - An env token WITHOUT the pair_ prefix (e.g. a stale BGOS user api key)
      yields to a secrets-file pairing_token when one exists; the caller
      should warn about the ignored env value.
    - With no secrets token, the env token is used as before (back-compat
      for setups that auth via a plain env key and never paired).
    """
    env_token = (env_token or "").strip() or None
    secrets_token = (secrets_token or "").strip() or None
    if env_token and looks_like_pairing_token(env_token):
        return TokenChoice(env_token, "env")
    if env_token and secrets_token:
        return TokenChoice(secrets_token, "secrets", ignored_env_token=env_token)
    if env_token:
        return TokenChoice(env_token, "env")
    if secrets_token:
        return TokenChoice(secrets_token, "secrets")
    return TokenChoice(None, "none")


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
