"""Background setup card for the local speech to text model download."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx


DEFAULT_STT_MODEL = "base"

_POLL_SECONDS = 1.0
_EDIT_SECONDS = 2.5
_STALL_SECONDS = 90.0
_HARD_STOP_SECONDS = 15.0 * 60.0
_MB = 1024 * 1024

_INITIAL_TEXT = (
    "Getting ready to listen. Your agent is downloading a small speech model "
    "(about 150 MB) so it can understand voice notes. Typing works normally "
    "in the meantime, and the note you just sent will be heard once this "
    "finishes."
)
_READY_TEXT = "Voice is ready. Your voice notes are understood from now on."
_STALLED_TEXT = (
    "The download paused. It will pick up again with your next voice note."
)

log = logging.getLogger(__name__)

MessageId = int | str
EventMeta = dict[str, Any]
PostEvent = Callable[[str, str, EventMeta], Awaitable[MessageId | None]]
EditEvent = Callable[[str, MessageId, str, EventMeta], Awaitable[None]]
HeadTotal = Callable[[str], Awaitable[int | None]]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def stt_setup_card_enabled() -> bool:
    value = os.environ.get("BGOS_VOICE_SETUP_CARD", "").strip().lower()
    return value not in {"0", "off", "false"}


def hf_hub_root() -> Path:
    configured = os.environ.get("HUGGINGFACE_HUB_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    hf_home = os.environ.get("HF_HOME", "").strip()
    if hf_home:
        return Path(hf_home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def model_dir(root: Path, model: str) -> Path:
    return root / f"models--Systran--faster-whisper-{model}"


def is_model_cached(directory: Path) -> bool:
    try:
        return any(
            candidate.is_file()
            for candidate in (directory / "snapshots").glob("*/model.bin")
        )
    except OSError:
        log.debug("failed to inspect speech model snapshots", exc_info=True)
        return False


def downloaded_bytes(directory: Path) -> int:
    total = 0
    try:
        candidates = (directory / "blobs").glob("*.incomplete")
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                total += candidate.stat().st_size
            except OSError:
                log.debug(
                    "speech model partial file changed during inspection",
                    exc_info=True,
                )
                continue
    except OSError:
        log.debug("failed to inspect speech model blobs", exc_info=True)
        return total
    return total


def format_progress(downloaded: int, total: int | None) -> str:
    downloaded_mb = int(round(downloaded / _MB))
    if total is None or total <= 0:
        return f"Downloading the speech model: {downloaded_mb} MB so far."
    percent = int(round(100 * downloaded / total))
    percent = max(0, min(100, percent))
    total_mb = int(round(total / _MB))
    return (
        f"Downloading the speech model: {percent}% "
        f"({downloaded_mb} of {total_mb} MB)."
    )


def faster_whisper_available() -> bool:
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        log.warning("failed to check faster_whisper availability", exc_info=True)
        return False


async def _head_model_total(model: str) -> int | None:
    url = (
        "https://huggingface.co/Systran/"
        f"faster-whisper-{model}/resolve/main/model.bin"
    )
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
        ) as client:
            response = await client.head(url)
            response.raise_for_status()
            value = response.headers.get("Content-Length")
            if value is None:
                return None
            total = int(value)
            return total if total > 0 else None
    except Exception:
        log.warning(
            "failed to read speech model Content-Length",
            exc_info=True,
        )
        return None


def _event_meta(
    stage: str,
    downloaded: int,
    total: int | None,
) -> EventMeta:
    return {
        "source": "voice_setup",
        "title": "Voice setup",
        "payload": {
            "progress": {
                "stage": stage,
                "downloadedBytes": downloaded,
                "totalBytes": total,
            },
        },
    }


class SttSetupNotifier:
    def __init__(
        self,
        post_event: PostEvent,
        edit_event: EditEvent,
        *,
        head_total: HeadTotal = _head_model_total,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        hub_root: Path | None = None,
    ) -> None:
        self._post_event = post_event
        self._edit_event = edit_event
        self._head_total = head_total
        self._clock = clock
        self._sleep = sleep
        self._hub_root = hub_root if hub_root is not None else hf_hub_root()
        self._lock = asyncio.Lock()
        self._active = False
        self._completed = False

    async def maybe_notify(self, chat_id: str) -> None:
        claimed = False
        try:
            if not stt_setup_card_enabled() or not faster_whisper_available():
                return

            model = os.environ.get("BGOS_STT_MODEL", "").strip()
            if not model:
                model = DEFAULT_STT_MODEL
            directory = model_dir(self._hub_root, model)

            async with self._lock:
                if (
                    self._active
                    or self._completed
                    or is_model_cached(directory)
                ):
                    return
                self._active = True
                claimed = True

            await self._run(chat_id, model, directory)
        except asyncio.CancelledError:
            log.info("speech model setup notifier cancelled")
        except Exception:
            log.warning("speech model setup notifier failed", exc_info=True)
        finally:
            if claimed:
                self._active = False

    async def _run(
        self,
        chat_id: str,
        model: str,
        directory: Path,
    ) -> None:
        try:
            total = await self._head_total(model)
        except Exception:
            log.warning(
                "speech model size request failed",
                exc_info=True,
            )
            total = None
        if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
            total = None

        observed = downloaded_bytes(directory)
        message_id = await self._post_event(
            chat_id,
            _INITIAL_TEXT,
            _event_meta("downloading", observed, total),
        )
        if message_id is None:
            return

        started_at = self._clock()
        last_growth_at = started_at
        last_edit_at = started_at
        last_emitted = observed
        has_edited = False

        while True:
            await self._sleep(_POLL_SECONDS)
            now = self._clock()

            if is_model_cached(directory):
                if has_edited and now - last_edit_at < _EDIT_SECONDS:
                    continue
                ready_bytes = total if total is not None else observed
                await self._edit_event(
                    chat_id,
                    message_id,
                    _READY_TEXT,
                    _event_meta("ready", ready_bytes, total),
                )
                self._completed = True
                return

            current = downloaded_bytes(directory)
            if current > observed:
                last_growth_at = now
            observed = current

            if (
                now - started_at >= _HARD_STOP_SECONDS
                or now - last_growth_at >= _STALL_SECONDS
            ):
                if has_edited and now - last_edit_at < _EDIT_SECONDS:
                    continue
                await self._edit_event(
                    chat_id,
                    message_id,
                    _STALLED_TEXT,
                    _event_meta("stalled", observed, total),
                )
                return

            if (
                observed != last_emitted
                and now - last_edit_at >= _EDIT_SECONDS
            ):
                await self._edit_event(
                    chat_id,
                    message_id,
                    format_progress(observed, total),
                    _event_meta("downloading", observed, total),
                )
                last_emitted = observed
                last_edit_at = now
                has_edited = True
