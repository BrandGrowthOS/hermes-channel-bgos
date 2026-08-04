"""BGOS inbound voice note routing into the host's STT pipeline.

Telegram caches downloaded audio locally and sends VOICE events with file paths
and MIME types. This module mirrors that contract and leaves transcription to
the Hermes gateway.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .hermes_profiles import resolve_hermes_home


MAX_VOICE_NOTE_BYTES = 25 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0
MAX_VOICE_NOTES_PER_MESSAGE = 5


log = logging.getLogger(__name__)


_VOICE_NOTE_EXTENSIONS = {
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/webm": ".webm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}


def _normalize_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


def voice_note_ext(mime: str | None) -> str | None:
    if not isinstance(mime, str):
        return None
    return _VOICE_NOTE_EXTENSIONS.get(_normalize_mime(mime))


def is_voice_note_candidate(f: dict) -> bool:
    if not isinstance(f, dict) or voice_note_ext(f.get("mime")) is None:
        return False
    return bool(f.get("url") or f.get("dataUri") or f.get("data_uri"))


def voice_notes_enabled() -> bool:
    value = os.environ.get("BGOS_VOICE_NOTES", "").strip().lower()
    return value not in {"0", "off", "false"}


def decode_data_uri(
    uri: str,
    cap: int = MAX_VOICE_NOTE_BYTES,
) -> bytes | None:
    if not isinstance(uri, str) or cap < 0:
        return None
    header, separator, payload = uri.partition(",")
    if (
        not separator
        or not header.lower().startswith("data:")
        or not header.lower().endswith(";base64")
        or len(payload) % 4
    ):
        return None

    padding = 2 if payload.endswith("==") else 1 if payload.endswith("=") else 0
    decoded_size = (len(payload) // 4) * 3 - padding
    if decoded_size > cap:
        return None

    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(data) > cap:
        return None
    return data


async def fetch_url_bytes(
    url: str,
    *,
    cap: int = MAX_VOICE_NOTE_BYTES,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> bytes | None:
    if not isinstance(url, str) or cap < 0:
        return None
    try:
        if urlparse(url).scheme.lower() not in {"http", "https"}:
            return None
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > cap:
                            return None
                    except ValueError:
                        pass

                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > cap:
                        return None
                    data.extend(chunk)
                return bytes(data)
    except Exception:
        return None


def cache_voice_note_bytes(data: bytes, ext: str) -> str | None:
    try:
        from gateway.platforms.base import cache_audio_from_bytes  # type: ignore

        cached_path = cache_audio_from_bytes(data, ext=ext)
        if cached_path:
            return str(cached_path)
    except Exception:
        pass

    try:
        cache_dir = resolve_hermes_home().expanduser().resolve() / "cache" / "audio"
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = cache_dir / f"bgos-{uuid4().hex}{ext}"
        path.write_bytes(data)
        return str(path)
    except Exception:
        return None


async def collect_voice_notes(
    files: list[dict] | None,
) -> list[tuple[str, str]]:
    if not isinstance(files, list):
        return []

    voice_media: list[tuple[str, str]] = []
    candidate_count = 0
    for index, file_entry in enumerate(files):
        try:
            if not is_voice_note_candidate(file_entry):
                continue
            if candidate_count >= MAX_VOICE_NOTES_PER_MESSAGE:
                break
            candidate_count += 1

            mime = file_entry.get("mime")
            ext = voice_note_ext(mime)
            if ext is None or not isinstance(mime, str):
                continue
            data_uri = file_entry.get("dataUri") or file_entry.get("data_uri")
            if data_uri:
                data = decode_data_uri(data_uri)
            else:
                data = await fetch_url_bytes(file_entry.get("url"))
            if data is None:
                log.warning("unable to read BGOS voice note file at index %s", index)
                continue

            local_path = cache_voice_note_bytes(data, ext)
            if local_path is None:
                log.warning("unable to cache BGOS voice note file at index %s", index)
                continue
            voice_media.append((local_path, _normalize_mime(mime)))
        except Exception:
            log.warning(
                "failed to collect BGOS voice note file at index %s",
                index,
                exc_info=True,
            )
    return voice_media
