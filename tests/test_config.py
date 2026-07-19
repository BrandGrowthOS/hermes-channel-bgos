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
