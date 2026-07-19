"""Tests for base_url normalization (config.normalize_base_url + BgosConfig).

Regression guard for the first-install 404: the client appends full
`/api/v1/...` paths, so a persisted base_url that already ends in `/api/v1`
doubled the prefix and 404d every call (seen live on a fresh macOS install,
whoami HTTP 404 with base_url=https://api.brandgrowthos.ai/api/v1).
"""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_api import BgosApi
from hermes_channel_bgos.config import BgosConfig, normalize_base_url


# The exact bad value observed on KC's machine.
BAD_PROD_BASE = "https://api.brandgrowthos.ai/api/v1"
GOOD_PROD_BASE = "https://api.brandgrowthos.ai"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (BAD_PROD_BASE, GOOD_PROD_BASE),
        (GOOD_PROD_BASE, GOOD_PROD_BASE),
        ("https://api.brandgrowthos.ai/", GOOD_PROD_BASE),
        ("https://api.brandgrowthos.ai/api/v1/", GOOD_PROD_BASE),
        ("https://api.brandgrowthos.ai/API/V1", GOOD_PROD_BASE),
        ("https://api.brandgrowthos.ai/api/v1/api/v1", GOOD_PROD_BASE),
        ("  https://api.brandgrowthos.ai/api/v1  ", GOOD_PROD_BASE),
        ("http://localhost:4000/api/v1", "http://localhost:4000"),
        ("http://localhost:4000", "http://localhost:4000"),
        # A path component that merely CONTAINS api/v1 must survive.
        ("https://host/api/v1x", "https://host/api/v1x"),
        ("", ""),
    ],
)
def test_normalize_base_url(raw: str, expected: str):
    assert normalize_base_url(raw) == expected


def test_bgos_config_self_heals_suffixed_base_url():
    cfg = BgosConfig(base_url=BAD_PROD_BASE, pairing_token="tok")
    assert cfg.base_url == GOOD_PROD_BASE


def test_bgos_config_leaves_clean_base_url_alone():
    cfg = BgosConfig(base_url=GOOD_PROD_BASE, pairing_token="tok")
    assert cfg.base_url == GOOD_PROD_BASE


async def test_api_with_suffixed_base_url_hits_correct_path(mock_bgos_server):
    """End-to-end: even a persisted bad base_url must reach /api/v1/... once."""
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 1, "assistants": []},
    )
    api = BgosApi(
        BgosConfig(base_url=f"{mock_bgos_server.url}/api/v1", pairing_token="tok"),
    )
    try:
        me = await api.whoami()
    finally:
        await api.close()
    assert me["pairing_id"] == 1


# ---------------------------------------------------------------------------
# Token-source precedence (choose_pairing_token) + display redaction.
# Regression guard for the shadowed-token incident: a stale BGOS_API_KEY env
# export holding a BGOS USER api key (not a pairing token) silently shadowed
# the freshly paired secrets-file token and produced whoami 401 forever.
# ---------------------------------------------------------------------------

from hermes_channel_bgos.config import choose_pairing_token, redact_token  # noqa: E402


def test_choose_env_pair_shaped_token_overrides_secrets():
    c = choose_pairing_token("pair_envAAAA", "pair_secretBBBB")
    assert c.token == "pair_envAAAA"
    assert c.source == "env"
    assert c.ignored_env_token is None


def test_choose_non_pairing_env_token_yields_to_secrets():
    c = choose_pairing_token("bgos_user_api_key_123", "pair_secretBBBB")
    assert c.token == "pair_secretBBBB"
    assert c.source == "secrets"
    assert c.ignored_env_token == "bgos_user_api_key_123"


def test_choose_non_pairing_env_token_used_when_no_secrets():
    c = choose_pairing_token("bgos_user_api_key_123", None)
    assert c.token == "bgos_user_api_key_123"
    assert c.source == "env"
    assert c.ignored_env_token is None


def test_choose_secrets_when_no_env():
    c = choose_pairing_token(None, "pair_secretBBBB")
    assert c.token == "pair_secretBBBB"
    assert c.source == "secrets"


def test_choose_none_when_nothing():
    c = choose_pairing_token(None, None)
    assert c.token is None
    assert c.source == "none"


def test_choose_treats_blank_strings_as_absent():
    c = choose_pairing_token("   ", "pair_secretBBBB")
    assert c.token == "pair_secretBBBB"
    assert c.source == "secrets"
    assert c.ignored_env_token is None


def test_redact_token_truncates_to_8_chars():
    full = "pair_9dB4567890abcdefgh"
    out = redact_token(full)
    assert out == "pair_9dB..."
    assert full not in out


def test_redact_token_handles_missing():
    assert redact_token(None) == ""
    assert redact_token("") == ""


def test_redact_token_never_returns_full_short_token():
    # A token shorter than the display budget must still be truncated.
    out = redact_token("bad")
    assert "bad" not in out
    assert out.endswith("...")
