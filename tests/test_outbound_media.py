"""Tests for BGOSAdapter's outbound-media methods (Task 6).

Five optional overrides — send_image, send_voice, send_video, send_document,
send_animation — each round-trips through _upload_and_attach which chooses
inline base64 (<500 KB) or S3 presigned PUT (≥500 KB).
"""
from __future__ import annotations

import struct

import pytest

from hermes_channel_bgos.bgos_adapter import (
    BGOSAdapter,
    S3_THRESHOLD,
    _classify_media,
    _parse_media_markers,
    _sniff_image_dimensions,
)
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


def _png_bytes(width: int, height: int) -> bytes:
    """Minimal byte string with a valid PNG signature + IHDR carrying the
    given dimensions — enough for `_sniff_image_dimensions` and for the
    image classifier. Not a renderable PNG (no IDAT), but the adapter only
    reads the header."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0DIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00" * 16
    )


async def _connected_adapter(server) -> BGOSAdapter:
    server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 42, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]},
    )
    adapter = BGOSAdapter(BgosConfig(base_url=server.url, pairing_token="pair_xyz"))
    await adapter.connect()
    return adapter


async def test_send_image_inline_under_threshold(mock_bgos_server):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 700})

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        small_png = b"\x89PNG\r\n\x1a\n" + b"0" * 1024  # ~1 KB
        result = await adapter.send_image(
            chat_id=11, file_bytes=small_png, filename="tiny.png",
            mime="image/png", caption="hi",
        )
        assert result.message_id == "700"  # fork's SendResult typing is str

        req = mock_bgos_server.last_request("POST", "/api/v1/messages")
        body = req.json_body
        assert body["chatId"] == 11
        assert body["text"] == "hi"
        assert body["sender"] == "assistant"
        files = body["files"]
        assert len(files) == 1
        # Wire shape matches openclaw-channel-bgos types.ts OutboundMessagePayload.files
        assert files[0]["fileName"] == "tiny.png"
        assert files[0]["fileMimeType"] == "image/png"
        assert "fileData" in files[0]
        assert "s3Key" not in files[0]
        assert files[0]["size"] == len(small_png)
        # Render-critical: inline bytes go as a data: URI (a bare base64
        # string won't load in the client's <Image>), and the image is
        # classified so the frontend renders it as an image, not a doc card.
        assert files[0]["fileData"].startswith("data:image/png;base64,")
        assert files[0]["isImage"] is True
        assert files[0]["isVideo"] is False
        assert files[0]["isDocument"] is False
    finally:
        await adapter.disconnect()


async def test_send_image_presigned_above_threshold(mock_bgos_server):
    """File ≥500 KB triggers presigned S3 PUT; POST /messages references s3_key.

    Exercises the corrected upload-url contract (2026-05-31): the real route
    is `/api/v1/files/upload-url` with `{fileName, contentType, size}` in and
    `{uploadUrl, key}` out — the old `/integrations/files/upload-url` +
    snake_case shape 404'd, silently breaking every ≥500 KB media send.
    """
    mock_bgos_server.on("POST", "/api/v1/files/upload-url").respond(
        200,
        {
            "uploadUrl": f"{mock_bgos_server.url}/s3/fake-put",
            "key": "k/abc",
        },
    )
    mock_bgos_server.on("PUT", "/s3/fake-put").respond(200)
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 701})

    adapter = await _connected_adapter(mock_bgos_server)
    try:
        big = b"X" * (S3_THRESHOLD + 10_000)
        await adapter.send_image(
            chat_id=11, file_bytes=big, filename="big.png", mime="image/png",
        )

        # Request used the camelCase DTO keys the backend's GetUploadUrlDto wants.
        up = mock_bgos_server.last_request("POST", "/api/v1/files/upload-url")
        assert up.json_body == {
            "fileName": "big.png", "contentType": "image/png", "size": len(big),
        }

        # S3 PUT received the raw bytes with correct Content-Type
        put = mock_bgos_server.last_request("PUT", "/s3/fake-put")
        assert put.body == big
        assert put.headers["Content-Type"] == "image/png"

        # POST /messages references s3Key, NOT fileData, and still classifies.
        msg = mock_bgos_server.last_request("POST", "/api/v1/messages")
        file_entry = msg.json_body["files"][0]
        assert file_entry["s3Key"] == "k/abc"
        assert "fileData" not in file_entry
        assert file_entry["size"] == len(big)
        assert file_entry["isImage"] is True
    finally:
        await adapter.disconnect()


async def test_send_voice_posts_message_level_audio_fields(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 702}},
    )
    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(11)
    try:
        await adapter.send_voice(
            chat_id=11, file_bytes=b"voice-bytes", filename="a.ogg",
            mime="audio/ogg", caption=None,
        )
        body = mock_bgos_server.last_request("POST", "/api/v1/send-message").json_body
        assert body["chatId"] == 11
        assert body["assistantId"] == 7
        assert body["text"] == ""
        assert body["sender"] == "assistant"
        assert body["hasAttachment"] is True
        assert body["isAudioMessage"] is True
        assert body["audioFileName"] == "a.ogg"
        assert body["audioMimeType"] == "audio/ogg"
        assert body["audioData"] == "dm9pY2UtYnl0ZXM="
        assert "files" not in body
    finally:
        await adapter.disconnect()




async def test_send_voice_accepts_hermes_audio_path_signature(mock_bgos_server, tmp_path):
    mock_bgos_server.on("GET", "/api/v1/chats/11").respond(200, {"assistantId": 7})
    mock_bgos_server.on("POST", "/api/v1/send-message").respond(
        201, {"message": {"id": 703}},
    )
    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"mp3-bytes")

    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(11)
    try:
        await adapter.send_voice(
            chat_id=11,
            audio_path=str(audio_path),
            metadata={"thread": "ignored-but-accepted"},
        )
        body = mock_bgos_server.last_request("POST", "/api/v1/send-message").json_body
        assert body["isAudioMessage"] is True
        assert body["audioFileName"] == "reply.mp3"
        assert body["audioMimeType"] == "audio/mpeg"
        assert body["audioData"] == "bXAzLWJ5dGVz"
    finally:
        await adapter.disconnect()


async def test_send_video_document_animation_all_work(mock_bgos_server):
    """Spot-check the remaining three overrides exist and post correctly."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 999})
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.send_video(chat_id=11, file_bytes=b"vid", filename="v.mp4", mime="video/mp4")
        await adapter.send_document(chat_id=11, file_bytes=b"doc", filename="d.pdf", mime="application/pdf")
        await adapter.send_animation(chat_id=11, file_bytes=b"gif", filename="g.gif", mime="image/gif")

        posts = [r for r in mock_bgos_server.requests
                 if r.method == "POST" and r.path == "/api/v1/messages"]
        assert len(posts) == 3
        filenames = [r.json_body["files"][0]["fileName"] for r in posts]
        assert filenames == ["v.mp4", "d.pdf", "g.gif"]
    finally:
        await adapter.disconnect()


async def test_send_document_accepts_hermes_file_path_signature(mock_bgos_server, tmp_path):
    """Hermes core dispatches extracted MEDIA: documents with file_path+metadata."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 704})
    doc_path = tmp_path / "agent-upload-key.txt"
    doc_path.write_text("secret-placeholder", encoding="utf-8")

    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(11)
    try:
        result = await adapter.send_document(
            chat_id=11,
            file_path=str(doc_path),
            metadata={"notify": True},
        )
        assert result.message_id == "704"
        body = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert body["text"] == ""
        assert body["files"][0]["fileName"] == "agent-upload-key.txt"
        assert body["files"][0]["fileMimeType"] == "text/plain"
        assert body["files"][0]["isDocument"] is True
        assert body["files"][0]["isImage"] is False
        assert body["files"][0]["fileData"].startswith("data:text/plain;base64,")
    finally:
        await adapter.disconnect()


async def test_send_multiple_images_accepts_hermes_file_uri_signature(mock_bgos_server, tmp_path):
    """Hermes core sends image MEDIA markers to send_multiple_images as file:// URIs."""
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 705})
    img_path = tmp_path / "thumb.png"
    img_path.write_bytes(_png_bytes(32, 24))

    adapter = await _connected_adapter(mock_bgos_server)
    adapter._state.record_inbound_chat(11)
    try:
        result = await adapter.send_multiple_images(
            chat_id=11,
            images=[(f"file://{img_path}", "")],
            metadata={"notify": True},
        )
        assert result.message_id == "705"
        body = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert body["files"][0]["fileName"] == "thumb.png"
        assert body["files"][0]["fileMimeType"] == "image/png"
        assert body["files"][0]["isImage"] is True
        assert body["files"][0]["width"] == 32
        assert body["files"][0]["height"] == 24
    finally:
        await adapter.disconnect()


# -----------------------------------------------------------------------------
# send_multiple_images (Task 3.3) — multi-image carousel as a single
# POST /messages with files[] holding multiple entries. Same UX as
# Telegram's sendMediaGroup. Routes each entry through _upload_and_attach
# so the same inline-vs-S3 policy applies per file.
# -----------------------------------------------------------------------------


def _make_adapter() -> BGOSAdapter:
    """Lightweight adapter for unit-testing send_multiple_images without
    spinning up the mock backend. Matches the helper in
    tests/test_bgos_adapter.py.

    Pre-seeds the chat ids these tests address into the received-chat allow-set
    — server-authoritative chat addressing (2026-05-30 hardening) makes send()
    reject outbound to a chat the adapter never received inbound."""
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    for chat_id in (1, 11, 42, 99):
        adapter._state.record_inbound_chat(chat_id)
    return adapter


async def test_send_multiple_images_single_post(monkeypatch):
    """3 images → 1 POST with files[3]."""
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 50}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    images = [
        (b"\x89PNG_a", "a.png", "image/png"),
        (b"\x89PNG_b", "b.png", "image/png"),
        (b"\x89PNG_c", "c.png", "image/png"),
    ]
    result = await adapter.send_multiple_images(
        chat_id=42, images=images, caption="three pics",
    )
    assert len(posts) == 1
    assert posts[0]["chat_id"] == 42
    assert posts[0]["text"] == "three pics"
    assert len(posts[0]["files"]) == 3
    # All inline (small files), so fileData populated
    for entry in posts[0]["files"]:
        assert "fileData" in entry
        assert entry["fileMimeType"] == "image/png"
    assert result.message_id == "50"


async def test_send_multiple_images_caps_at_10(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    images = [(b"x", f"img{i}.png", "image/png") for i in range(15)]
    await adapter.send_multiple_images(chat_id=1, images=images)
    assert len(posts[0]["files"]) == 10
    # The warning must surface so operators can spot runaway agents that
    # silently lose images past the carousel cap.
    assert any(
        "15" in record.message and "10" in record.message
        for record in caplog.records
    )


async def test_send_multiple_images_empty_is_noop(monkeypatch):
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    result = await adapter.send_multiple_images(chat_id=1, images=[])
    assert posts == []
    assert result.success is True
    assert result.message_id is None


async def test_send_multiple_images_with_reply_to(monkeypatch):
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send_multiple_images(
        chat_id=1,
        images=[(b"x", "a.png", "image/png")],
        reply_to=99,
    )
    assert posts[0]["reply_to_id"] == 99


# -----------------------------------------------------------------------------
# Media classification + MEDIA:/path marker parsing (2026-05-31 image-delivery
# fix). The BGOS backend stores isImage/isVideo/... verbatim and the frontend
# renders a file as an image ONLY when isImage is true — so the adapter MUST
# classify on the wire, and MUST actually parse the documented `MEDIA:/path`
# convention (which it never did before).
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("image/png", "isImage"),
        ("image/jpeg", "isImage"),
        ("video/mp4", "isVideo"),
        ("audio/ogg", "isAudio"),
        ("application/pdf", "isDocument"),
        ("application/octet-stream", "isDocument"),
        ("", "isDocument"),
    ],
)
def test_classify_media_sets_exactly_one_flag(mime, expected):
    flags = _classify_media(mime)
    assert flags[expected] is True
    # Exactly one kind flag is ever true — they're mutually exclusive.
    assert sum(1 for v in flags.values() if v) == 1


def test_sniff_image_dimensions_png():
    assert _sniff_image_dimensions(_png_bytes(640, 480)) == (640, 480)


def test_sniff_image_dimensions_unknown_is_none():
    assert _sniff_image_dimensions(b"not an image at all") == (None, None)


def test_parse_media_markers_extracts_and_strips():
    text = "Here's your poster!\nMEDIA:/tmp/poster.png\nHope you like it."
    cleaned, paths = _parse_media_markers(text)
    assert paths == ["/tmp/poster.png"]
    assert "MEDIA:" not in cleaned
    assert "Here's your poster!" in cleaned
    assert "Hope you like it." in cleaned


def test_parse_media_markers_multiple_and_spaces_in_path():
    text = "MEDIA:/tmp/a.png\ntext\nMEDIA:/tmp/my poster.jpg"
    cleaned, paths = _parse_media_markers(text)
    assert paths == ["/tmp/a.png", "/tmp/my poster.jpg"]
    assert cleaned == "text"


def test_parse_media_markers_ignores_midsentence_token():
    text = "Use the MEDIA: convention to send files."
    cleaned, paths = _parse_media_markers(text)
    assert paths == []
    assert cleaned == text


def test_parse_media_markers_skips_code_fence():
    """A MEDIA: line shown INSIDE a code fence (agent documenting the
    convention to the user) must NOT be parsed or stripped — doing so both
    eats a phantom path and mangles the rendered code block."""
    text = "Here's how:\n```\nMEDIA:/example/path.png\n```\nThat's the format."
    cleaned, paths = _parse_media_markers(text)
    assert paths == []
    assert cleaned == text  # fence preserved verbatim


def test_parse_media_markers_real_outside_example_inside_fence():
    """A real marker outside a fence is delivered; an example inside the fence
    is preserved."""
    text = (
        "Sending it now.\n"
        "MEDIA:/tmp/real.png\n"
        "For reference, the syntax is:\n"
        "```\nMEDIA:/some/example.png\n```"
    )
    cleaned, paths = _parse_media_markers(text)
    assert paths == ["/tmp/real.png"]
    # The real marker is gone; the in-fence example survives.
    assert "MEDIA:/tmp/real.png" not in cleaned
    assert "```\nMEDIA:/some/example.png\n```" in cleaned


async def test_send_media_with_tool_progress_shaped_caption(monkeypatch, tmp_path):
    """A media reply whose residual caption coincidentally parses as
    tool_progress (emoji + CamelCase) must still deliver the file, not get
    short-circuited into a tool_progress card."""
    adapter = _make_adapter()
    img = tmp_path / "poster.png"
    img.write_bytes(_png_bytes(64, 64))

    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 88}

    # If the bug were present, send() would route to _handle_tool_progress_edit
    # instead of post_message — fail loudly if that path is taken.
    async def fail_tool_progress(*a, **k):
        raise AssertionError("media reply was wrongly short-circuited to tool_progress")

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    monkeypatch.setattr(adapter, "_handle_tool_progress_edit", fail_tool_progress)
    await adapter.send(chat_id=1, content=f"📸 GenerateImage: poster\nMEDIA:{img}")

    assert len(posts) == 1
    assert posts[0]["text"] == "📸 GenerateImage: poster"
    assert len(posts[0]["files"]) == 1
    assert posts[0]["files"][0]["isImage"] is True


async def test_send_delivers_media_marker(monkeypatch, tmp_path):
    """A reply containing `MEDIA:/abs/path` attaches the file as a classified
    image and strips the marker from the visible text."""
    adapter = _make_adapter()
    img = tmp_path / "poster.png"
    img.write_bytes(_png_bytes(800, 600))

    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 77}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send(
        chat_id=1, content=f"Here is the poster you asked for.\nMEDIA:{img}",
    )

    assert len(posts) == 1
    assert posts[0]["text"] == "Here is the poster you asked for."
    files = posts[0]["files"]
    assert len(files) == 1
    assert files[0]["fileName"] == "poster.png"
    assert files[0]["fileMimeType"] == "image/png"
    assert files[0]["isImage"] is True
    assert files[0]["fileData"].startswith("data:image/png;base64,")
    assert files[0]["width"] == 800 and files[0]["height"] == 600


async def test_send_media_only_reply_posts_empty_caption(monkeypatch, tmp_path):
    """Image-only reply (no surrounding text) still posts, with empty text."""
    adapter = _make_adapter()
    img = tmp_path / "x.png"
    img.write_bytes(_png_bytes(10, 10))
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send(chat_id=1, content=f"MEDIA:{img}")
    assert len(posts) == 1
    assert posts[0]["text"] == ""
    assert len(posts[0]["files"]) == 1


async def test_send_media_marker_bad_path_dropped_text_kept(monkeypatch):
    """A MEDIA: marker pointing at a missing file is dropped (no files), but
    the surrounding text is still delivered."""
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send(
        chat_id=1, content="Done!\nMEDIA:/nope/does-not-exist.png",
    )
    assert len(posts) == 1
    assert posts[0]["text"] == "Done!"
    # No file survived, and `files` is None (not an empty list) on this path.
    assert not posts[0].get("files")


async def test_send_media_only_all_bad_posts_nothing(monkeypatch):
    """If the reply is ONLY a bad MEDIA: marker, nothing is posted (no empty
    bubble)."""
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    result = await adapter.send(chat_id=1, content="MEDIA:/nope/missing.png")
    assert posts == []
    assert result.message_id is None


# -----------------------------------------------------------------------------
# send_image / send_image_file / send_animation — gateway URL+path contract.
#
# Hermes's gateway calls send_image(chat_id=…, image_url=<http url>),
# send_image_file(chat_id=…, image_path=<local>), send_animation(…,
# animation_url=…). These were byte-only / path-only and dropped markdown
# http-image links. _fetch_media_source downloads URLs (re-uploaded to BGOS so
# they outlive the ephemeral source) and reads local/file:// paths.
# -----------------------------------------------------------------------------


class _FakeHttpResp:
    def __init__(self, content: bytes, ctype: str = "image/png") -> None:
        self.content = content
        self.headers = {"content-type": ctype}

    def raise_for_status(self) -> None:
        return None


class _FakeHttpClient:
    """Stand-in for httpx.AsyncClient so _fetch_media_source exercises the URL
    branch without real network. Returns a fixed PNG for any GET."""

    _payload = _png_bytes(40, 30)

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def get(self, url: str):
        return _FakeHttpResp(self._payload, "image/png")


async def test_send_image_downloads_url_and_reuploads(monkeypatch):
    """Gateway form send_image(image_url=http) downloads + re-uploads to BGOS."""
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.httpx.AsyncClient", _FakeHttpClient,
    )

    await adapter.send_image(
        chat_id=1, image_url="https://fal.media/files/cat.png",
        caption="a cat", metadata={"platform": "bgos"},
    )
    assert len(posts) == 1
    f = posts[0]["files"][0]
    assert f["fileName"] == "cat.png"
    assert f["isImage"] is True
    assert f["fileData"].startswith("data:image/png;base64,")
    assert posts[0]["text"] == "a cat"


async def test_send_image_byte_form_still_works(monkeypatch):
    """Internal/byte callers (the pre-Hermes signature) keep working."""
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send_image(
        chat_id=1, file_bytes=_png_bytes(8, 8), filename="b.png", mime="image/png",
    )
    assert posts[0]["files"][0]["fileName"] == "b.png"


async def test_send_image_file_reads_local_path(monkeypatch, tmp_path):
    """Gateway form send_image_file(image_path=local) reads + posts."""
    adapter = _make_adapter()
    img = tmp_path / "local.png"
    img.write_bytes(_png_bytes(20, 20))
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    await adapter.send_image_file(
        chat_id=1, image_path=str(img), caption="c", metadata={"platform": "bgos"},
    )
    assert posts[0]["files"][0]["fileName"] == "local.png"
    assert posts[0]["files"][0]["isImage"] is True
    assert posts[0]["text"] == "c"


async def test_send_animation_url_downloads(monkeypatch):
    """send_animation accepts a URL (positional or animation_url=) and downloads."""
    adapter = _make_adapter()
    posts = []

    async def fake_post(**kw):
        posts.append(kw)
        return {"id": 2}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.httpx.AsyncClient", _FakeHttpClient,
    )
    await adapter.send_animation(chat_id=1, animation_url="https://media.test/loop.gif")
    assert len(posts) == 1
    assert posts[0]["files"][0]["fileName"] == "loop.gif"


async def test_image_methods_ignore_unknown_gateway_kwargs(monkeypatch, tmp_path):
    """**kwargs absorbs unexpected gateway kwargs — the exact regression class."""
    adapter = _make_adapter()
    img = tmp_path / "k.png"
    img.write_bytes(_png_bytes(10, 10))

    async def fake_post(**kw):
        return {"id": 1}

    monkeypatch.setattr(adapter._api, "post_message", fake_post)
    res = await adapter.send_image_file(
        chat_id=1, image_path=str(img), reply_to=7,
        some_future_kwarg="x", thread_id="t1",
    )
    assert res.message_id == "1"


async def test_send_image_dead_url_is_graceful(monkeypatch):
    """A dead image URL degrades to a no-op, never raises."""
    adapter = _make_adapter()

    class _BoomClient(_FakeHttpClient):
        async def get(self, url: str):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "hermes_channel_bgos.bgos_adapter.httpx.AsyncClient", _BoomClient,
    )
    res = await adapter.send_image(chat_id=1, image_url="https://dead.link/x.png")
    assert res.message_id is None
