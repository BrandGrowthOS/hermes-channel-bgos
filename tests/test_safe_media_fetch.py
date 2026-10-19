"""Remote media fetch must not reach private addresses, follow a redirect to
one, or buffer an unbounded body (the bytes are re-posted into a shared chat).

The Hermes helpers (tools.url_safety) are not installed in CI, so the tests
stand in for them with a faithful address check and an httpx MockTransport.
Every test installs the same mock client under BOTH the new helper name and
`httpx.AsyncClient`, so on the old code (which called httpx directly and
followed redirects) the same scenarios run for real and the assertions fail.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlparse

import httpx
import pytest

import hermes_channel_bgos.bgos_adapter as mod
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig

pytestmark = pytest.mark.asyncio

MIB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _fake_is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "metadata.google.internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a public DNS name
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _fake_redirect_target(resp: httpx.Response):
    if not resp.is_redirect:
        return None
    return urljoin(str(resp.url), resp.headers["location"])


class _Wire:
    """Records every request that actually leaves the mock transport."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return self.handler(request)


def _install(monkeypatch, handler) -> _Wire:
    wire = _Wire(handler)
    real_client = httpx.AsyncClient  # captured before any patching

    def _client(**kwargs):
        # The old code asked for follow_redirects=True; honour whatever the
        # caller asked for, as the real client would.
        return real_client(transport=httpx.MockTransport(wire), **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _client)
    monkeypatch.setattr(mod, "create_ssrf_safe_async_client", _client, raising=False)
    monkeypatch.setattr(mod, "is_safe_url", _fake_is_safe_url, raising=False)
    monkeypatch.setattr(
        mod, "redirect_target_from_response", _fake_redirect_target, raising=False,
    )
    return wire


def _adapter() -> BGOSAdapter:
    return BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))


def _png_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/secret.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal.png",
        "http://localhost/admin.png",
    ],
)
async def test_private_address_is_refused_without_a_request(monkeypatch, url):
    wire = _install(monkeypatch, _png_ok)
    data, _name, _mime = await _adapter()._fetch_media_source(url)
    assert data is None
    assert wire.requests == []


async def test_redirect_to_private_address_is_refused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example.com":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"},
            )
        return httpx.Response(200, content=b"SECRET", headers={"content-type": "image/png"})

    wire = _install(monkeypatch, handler)
    data, _name, _mime = await _adapter()._fetch_media_source(
        "https://cdn.example.com/cat.png",
    )
    assert data is None
    assert wire.requests == ["https://cdn.example.com/cat.png"]


async def test_redirect_to_a_public_address_is_followed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/short":
            return httpx.Response(301, headers={"location": "/files/cat.png"})
        return _png_ok(request)

    wire = _install(monkeypatch, handler)
    data, name, mime = await _adapter()._fetch_media_source("https://cdn.example.com/short")
    assert data == PNG
    assert mime == "image/png"
    assert wire.requests == [
        "https://cdn.example.com/short", "https://cdn.example.com/files/cat.png",
    ]


async def test_redirect_loop_is_cut_off(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://cdn.example.com/again"})

    wire = _install(monkeypatch, handler)
    data, _name, _mime = await _adapter()._fetch_media_source("https://cdn.example.com/a")
    assert data is None
    assert len(wire.requests) == mod._MEDIA_MAX_REDIRECTS + 1


async def test_oversize_stream_stops_at_the_cap(monkeypatch):
    chunk = b"\0" * MIB
    sent = {"chunks": 0}

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(60):  # 60 MiB on offer, no content-length
                sent["chunks"] += 1
                yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=_Stream(), headers={"content-type": "image/png"},
        )

    _install(monkeypatch, handler)
    data, _name, _mime = await _adapter()._fetch_media_source(
        "https://cdn.example.com/huge.png",
    )
    assert data is None
    # Stopped right after crossing 25 MiB instead of reading all 60.
    assert sent["chunks"] <= mod._MEDIA_REMOTE_MAX_BYTES // MIB + 2


async def test_declared_oversize_is_refused_before_reading(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=PNG,
            headers={"content-type": "image/png", "content-length": str(30 * MIB)},
        )

    _install(monkeypatch, handler)
    # httpx validates content-length against the body on the wire, so the
    # mismatch may surface as an error; either way no bytes come back.
    data, _name, _mime = await _adapter()._fetch_media_source(
        "https://cdn.example.com/huge.png",
    )
    assert data is None


async def test_good_public_url_is_fetched_and_posted(monkeypatch):
    wire = _install(monkeypatch, _png_ok)
    adapter = _adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send_image(chat_id=1, image_url="https://cdn.example.com/cat.png")
    assert [u for u in wire.requests if "cdn.example.com" in u] == [
        "https://cdn.example.com/cat.png"
    ]
    assert posts[0]["files"][0]["fileName"] == "cat.png"
    assert posts[0]["files"][0]["fileData"].startswith("data:image/png;base64,")


async def test_payload_just_under_the_cap_is_accepted(monkeypatch):
    body = b"\0" * (mod._MEDIA_REMOTE_MAX_BYTES - 1)
    _install(
        monkeypatch,
        lambda r: httpx.Response(200, content=body, headers={"content-type": "video/mp4"}),
    )
    data, _name, _mime = await _adapter()._fetch_media_source(
        "https://cdn.example.com/clip.mp4",
    )
    assert data == body


@pytest.mark.parametrize(
    "missing", ["create_ssrf_safe_async_client", "is_safe_url", "redirect_target_from_response"],
)
async def test_fails_closed_when_a_helper_is_missing(monkeypatch, missing):
    adapter = _adapter()  # built first: its API client uses httpx too
    wire = _install(monkeypatch, _png_ok)
    monkeypatch.setattr(mod, missing, None, raising=False)
    # The old code never looked at the helpers; make sure a bare httpx client
    # is not a way around the guard either.
    monkeypatch.setattr(
        mod.httpx, "AsyncClient",
        lambda **k: pytest.fail("fell back to a bare httpx client"),
    )
    data, _name, _mime = await adapter._fetch_media_source(
        "https://cdn.example.com/cat.png",
    )
    assert data is None
    assert wire.requests == []


async def test_local_paths_do_not_need_the_helpers(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "create_ssrf_safe_async_client", None, raising=False)
    img = tmp_path / "local.png"
    img.write_bytes(PNG)
    data, name, _mime = await _adapter()._fetch_media_source(str(img))
    assert data == PNG
    assert name == "local.png"


async def test_the_real_hermes_helpers_block_private_targets():
    """Only runs where Hermes is installed (a dev box); proves the names we
    import exist with the semantics the fake above mimics."""
    url_safety = pytest.importorskip("tools.url_safety")
    assert url_safety.is_safe_url("http://169.254.169.254/latest/") is False
    assert url_safety.is_safe_url("http://127.0.0.1:8080/") is False
    assert url_safety.is_safe_url("http://10.0.0.5/") is False
    assert callable(url_safety.create_ssrf_safe_async_client)
    assert callable(url_safety.redirect_target_from_response)
