from __future__ import annotations

import base64
import logging
from pathlib import Path
import sys

import pytest

from hermes_channel_bgos.voice_notes import (
    cache_voice_note_bytes,
    collect_voice_notes,
    decode_data_uri,
    fetch_url_bytes,
    is_voice_note_candidate,
    voice_note_ext,
    voice_notes_enabled,
)


def test_voice_note_ext_maps_supported_mimes() -> None:
    cases = {
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/ogg": ".ogg",
        "audio/OGG; codecs=opus": ".ogg",
        "audio/opus": ".ogg",
        "audio/webm": ".webm",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/flac": ".flac",
    }
    for mime, expected in cases.items():
        assert voice_note_ext(mime) == expected

    for mime in ("application/pdf", "image/png", None, ""):
        assert voice_note_ext(mime) is None

    assert is_voice_note_candidate({"mime": "audio/ogg", "url": "https://x/n.ogg"})
    assert is_voice_note_candidate({"mime": "audio/opus", "data_uri": "data:x"})
    assert not is_voice_note_candidate({"mime": "audio/ogg"})
    assert not is_voice_note_candidate({"mime": "application/pdf", "url": "https://x/a"})


def test_decode_data_uri_handles_valid_malformed_and_oversized_data() -> None:
    payload = b"OggS\x00voice-note"
    uri = "data:audio/ogg;base64," + base64.b64encode(payload).decode("ascii")

    assert decode_data_uri(uri) == payload
    assert decode_data_uri("not-a-data-uri") is None
    assert decode_data_uri("data:audio/ogg;base64,%%not-base64%%") is None

    oversized = base64.b64encode(b"x" * 1025).decode("ascii")
    assert decode_data_uri(f"data:audio/ogg;base64,{oversized}", cap=1024) is None


@pytest.mark.asyncio
async def test_fetch_url_bytes_streams_and_rejects_large_or_error_responses(
    mock_bgos_server,
) -> None:
    payload = b"OggS\x00downloaded-voice"
    mock_bgos_server.on("GET", "/voice.ogg").respond(200, data=payload)
    mock_bgos_server.on("GET", "/large.ogg").respond(200, data=b"x" * 1025)
    mock_bgos_server.on("GET", "/missing.ogg").respond(404)

    assert await fetch_url_bytes(f"{mock_bgos_server.url}/voice.ogg") == payload
    assert await fetch_url_bytes(
        f"{mock_bgos_server.url}/large.ogg", cap=1024,
    ) is None
    assert await fetch_url_bytes(f"{mock_bgos_server.url}/missing.ogg") is None


def test_cache_voice_note_bytes_uses_fallback_audio_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setitem(sys.modules, "gateway", None)
    payload = b"OggS\x00cached-voice"

    result = cache_voice_note_bytes(payload, ".ogg")

    assert result is not None
    path = Path(result)
    assert path.is_absolute()
    assert path.is_relative_to(hermes_home / "cache" / "audio")
    assert path.suffix == ".ogg"
    assert path.read_bytes() == payload


def test_voice_notes_enabled_defaults_on_and_honors_switch(monkeypatch) -> None:
    monkeypatch.delenv("BGOS_VOICE_NOTES", raising=False)
    assert voice_notes_enabled() is True

    monkeypatch.setenv("BGOS_VOICE_NOTES", "off")
    assert voice_notes_enabled() is False

    monkeypatch.setenv("BGOS_VOICE_NOTES", "0")
    assert voice_notes_enabled() is False

    monkeypatch.setenv("BGOS_VOICE_NOTES", "on")
    assert voice_notes_enabled() is True


@pytest.mark.asyncio
async def test_collect_voice_notes_skips_a_broken_file(
    caplog,
    monkeypatch,
) -> None:
    class BrokenFile(dict):
        def get(self, key, default=None):
            raise RuntimeError("broken file entry")

    monkeypatch.setitem(sys.modules, "gateway", None)
    caplog.set_level(logging.WARNING)
    payload = base64.b64encode(b"OggS").decode("ascii")

    result = await collect_voice_notes([
        BrokenFile(),
        {"mime": "audio/ogg", "dataUri": f"data:audio/ogg;base64,{payload}"},
    ])

    assert len(result) == 1
    assert result[0][1] == "audio/ogg"
    assert "failed to collect BGOS voice note file at index 0" in caplog.text
