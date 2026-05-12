"""Tests for BGOSAdapter's outbound-media methods (Task 6).

Five optional overrides — send_image, send_voice, send_video, send_document,
send_animation — each round-trips through _upload_and_attach which chooses
inline base64 (<500 KB) or S3 presigned PUT (≥500 KB).
"""
from __future__ import annotations

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter, S3_THRESHOLD
from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


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
    finally:
        await adapter.disconnect()


async def test_send_image_presigned_above_threshold(mock_bgos_server):
    """File ≥500 KB triggers presigned S3 PUT; POST /messages references s3_key."""
    mock_bgos_server.on("POST", "/api/v1/integrations/files/upload-url").respond(
        200,
        {
            "upload_url": f"{mock_bgos_server.url}/s3/fake-put",
            "s3_key": "k/abc",
            "expires_at": "2099-01-01T00:00:00Z",
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

        # S3 PUT received the raw bytes with correct Content-Type
        put = mock_bgos_server.last_request("PUT", "/s3/fake-put")
        assert put.body == big
        assert put.headers["Content-Type"] == "image/png"

        # POST /messages references s3Key, NOT fileData
        msg = mock_bgos_server.last_request("POST", "/api/v1/messages")
        file_entry = msg.json_body["files"][0]
        assert file_entry["s3Key"] == "k/abc"
        assert "fileData" not in file_entry
        assert file_entry["size"] == len(big)
    finally:
        await adapter.disconnect()


async def test_send_voice_routes_through_same_path(mock_bgos_server):
    mock_bgos_server.on("POST", "/api/v1/messages").respond(201, {"id": 702})
    adapter = await _connected_adapter(mock_bgos_server)
    try:
        await adapter.send_voice(
            chat_id=11, file_bytes=b"voice-bytes", filename="a.ogg",
            mime="audio/ogg", caption=None,
        )
        body = mock_bgos_server.last_request("POST", "/api/v1/messages").json_body
        assert body["files"][0]["fileName"] == "a.ogg"
        assert body["text"] == ""
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


# -----------------------------------------------------------------------------
# send_multiple_images (Task 3.3) — multi-image carousel as a single
# POST /messages with files[] holding multiple entries. Same UX as
# Telegram's sendMediaGroup. Routes each entry through _upload_and_attach
# so the same inline-vs-S3 policy applies per file.
# -----------------------------------------------------------------------------


def _make_adapter() -> BGOSAdapter:
    """Lightweight adapter for unit-testing send_multiple_images without
    spinning up the mock backend. Matches the helper in
    tests/test_bgos_adapter.py."""
    return BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))


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
