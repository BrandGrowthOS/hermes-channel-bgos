"""BGOS channel adapter — the `BasePlatformAdapter` subclass that Hermes's
gateway instantiates (via the 5-line shim at `gateway/platforms/bgos.py` in
the fork, which imports this class).

Task 4 (this commit): connect/disconnect lifecycle, `send()`, `get_chat_info()`,
and placeholder callback/inbound hooks that later tasks flesh out.

Later tasks wire in: inbound translation → `handle_message` (Task 5), outbound
media overrides (Task 6), `send_exec_approval` (Task 7), callback routing with
`resolve_gateway_approval` (Task 8), slash-manifest sync + bridge-locals (Task 9).
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import mimetypes
import os
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .bgos_api import BgosApi, BgosApiError
from .bgos_ws import BgosWs
from .commands_sync import (
    BRIDGE_LOCAL_COMMANDS,
    build_manifest,
    fetch_hermes_native_commands,
)
from .agents import enumerate_agents_from_env
from .config import BgosConfig
from .state_store import StateStore

try:  # pragma: no cover - exercised only when Hermes is installed
    from tools.approval import resolve_gateway_approval  # type: ignore
except ImportError:
    # Hermes not installed — provide a stub that tests can monkeypatch. The
    # real function is synchronous and uses threading.Lock + Event internally.
    def resolve_gateway_approval(session_key: str, choice: str) -> None:  # type: ignore[no-redef]
        raise RuntimeError(
            "tools.approval.resolve_gateway_approval is not importable — "
            "Hermes not installed. Tests should monkeypatch this at the "
            "module level (hermes_channel_bgos.bgos_adapter)."
        )


try:  # pragma: no cover - exercised only when Hermes is installed
    # Phase 1 stub; replace import target when Hermes ships its slash_confirm
    # helper. Telegram has its own override at gateway/platforms/telegram.py:2119
    # but the fork hasn't surfaced a shared resolver path yet — tests monkeypatch
    # this at the module level (hermes_channel_bgos.bgos_adapter.resolve_slash_confirm).
    from gateway.slash_confirm import resolve as resolve_slash_confirm  # type: ignore
except ImportError:
    def resolve_slash_confirm(session_key: str, confirm_id: str, choice: str) -> None:  # type: ignore[no-redef]
        raise RuntimeError(
            "gateway.slash_confirm.resolve is not importable — either Hermes "
            "is not installed, or this Hermes build hasn't shipped the slash-"
            "confirm helper yet (Phase 1 stub in hermes_channel_bgos.bgos_adapter). "
            "Tests should monkeypatch this at the module level."
        )


# Matches Telegram's callback_data format at gateway/platforms/telegram.py:1275.
# Four choice values: once, session, always, deny — mapped 1:1 from Hermes's
# approval vocabulary.
_APPROVAL_CALLBACK_RE = re.compile(r"^ea:(once|session|always|deny):(\d+)$")

# Slash-confirm callback shape — three choices (once / always / cancel) for
# the 3-button UX rendered by send_slash_confirm. confirm_id is opaque to the
# adapter (Hermes mints it); we treat it as an arbitrary string.
_SLASH_CONFIRM_CALLBACK_RE = re.compile(r"^sc:(once|always|cancel):(.+)$")

# Agent-emitted inline-button marker block. See PLATFORM_HINTS["bgos"] in the
# Hermes fork patch — the agent writes its choices between these tags and the
# adapter translates them into a BGOS options payload before posting. `mode=`
# is optional (default inline). Entries are one per line as `Label | value`
# (pipe-separated). Case-insensitive tags. Blank lines are skipped.
_BUTTONS_BLOCK_RE = re.compile(
    r"\[\[BGOS_BUTTONS(?:\s+mode=(inline|modal))?\]\]"
    r"(.*?)"
    r"\[\[/BGOS_BUTTONS\]\]",
    re.IGNORECASE | re.DOTALL,
)

# Agent-emitted reply-quote marker. The agent writes the source message id
# between these tags and the adapter extracts it into the `replyToId` field
# on the outbound POST so BGOS renders a Telegram-style quoted header on
# the receiving bubble. The marker is stripped from the visible text before
# posting. Spec: BGOS/docs/superpowers/specs/2026-05-19-reply-quote-design.md
_REPLY_TO_BLOCK_RE = re.compile(
    r"\[\[BGOS_REPLY_TO\]\]\s*(\d+)\s*\[\[/BGOS_REPLY_TO\]\]",
    re.IGNORECASE,
)

# Backend rejects inline messages with >6 options (see PR #62 + memory
# `inline_buttons_shipped.md`). Agents that emit more get truncated; we log
# a warning so this isn't silent.
_INLINE_OPTION_LIMIT = 6

# Threshold below which we inline base64 into POST /messages; at or above,
# we upload via a presigned S3 PUT and reference by s3_key. Mirrors
# openclaw-channel-bgos's policy + the memory note `bug_base64_body_limit.md`.
S3_THRESHOLD = 500 * 1024

# Hard upper bound on a single outbound media file we'll read into memory.
# The backend caps images at 10 MB and video at 100 MB; we use the video cap
# as the ceiling so a pathological path can't OOM the gateway process. Files
# above this are skipped with a warning rather than read.
_MEDIA_MAX_BYTES = 100 * 1024 * 1024

# Agent-emitted outbound-media marker. The agent writes `MEDIA:/abs/path` on
# its own line (per PLATFORM_HINTS["bgos"] "Sending files and media") and the
# adapter turns each into a real BGOS attachment, stripping the marker from
# the visible text. Anchored at line start (after optional indent) so the
# token `MEDIA:` appearing mid-sentence is not mistaken for a command. The
# capture group is the rest of the line (path may contain spaces); trailing
# whitespace is trimmed by the parser. Case-sensitive `MEDIA:` to match the
# documented contract exactly.
_MEDIA_MARKER_RE = re.compile(r"^[ \t]*MEDIA:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)

# Fenced code-block matcher (``` … ``` or ~~~ … ~~~). `_parse_media_markers`
# splits on this and scans ONLY the non-fenced segments, so a `MEDIA:` line an
# agent shows INSIDE a code fence — e.g. documenting the convention to the user
# — is left intact rather than consumed as a real attachment (which would both
# eat a non-existent file and mangle the rendered code block).
_CODE_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)

# MIME types Python's `mimetypes` stdlib doesn't reliably know across the
# platforms Hermes runs on (notably webp/heic and the OGG/M4A audio family).
# Mirrors gobot-channel-bgos/src/attachment-bridge.ts `guessMimeType`.
_EXTRA_MIME_TYPES: dict[str, str] = {
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".svg": "image/svg+xml",
    ".mov": "video/quicktime",
    ".m4a": "audio/mp4",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}


def _guess_media_mime(filename: str) -> str:
    """Best-effort MIME from a filename. Falls back to the stdlib `mimetypes`
    table, then a small map for types it commonly misses, then the generic
    `application/octet-stream` (still delivered — renders as a download card).
    """
    ext = Path(filename).suffix.lower()
    if ext in _EXTRA_MIME_TYPES:
        return _EXTRA_MIME_TYPES[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _classify_media(mime: str) -> dict[str, bool]:
    """Map a MIME type to BGOS's four boolean attachment-kind flags.

    The BGOS backend stores these verbatim (`is_image = dto.isImage ?? null`
    — it does NOT derive them from the MIME) and the frontend renders a file
    as an inline image/video ONLY when `isImage`/`isVideo` is true, else as a
    generic document card (FileAttachmentDisplay.tsx). Omitting these — as
    this adapter did before 2026-05-31 — made every agent-sent image land as
    a non-rendering document. So we must classify on the wire.
    """
    m = (mime or "").lower()
    is_image = m.startswith("image/")
    is_video = m.startswith("video/")
    is_audio = m.startswith("audio/")
    return {
        "isImage": is_image,
        "isVideo": is_video,
        "isAudio": is_audio,
        # Anything that isn't recognized media is a document — the frontend's
        # download-card fallback. (Mutually exclusive with the three above.)
        "isDocument": not (is_image or is_video or is_audio),
    }


def _sniff_image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Best-effort intrinsic (width, height) for the common image formats,
    dependency-free (no Pillow). Returns (None, None) for anything we can't
    parse — width/height are optional; the frontend's MediaAlbum falls back to
    a default aspect, so a miss only costs a slightly-off thumbnail ratio.
    Fully guarded: any malformed header yields (None, None), never raises.
    """
    try:
        # PNG: 8-byte sig, then IHDR chunk with width/height as big-endian u32.
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        # GIF: 'GIF87a'/'GIF89a' then logical screen width/height little-endian.
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        # JPEG: scan for a Start-Of-Frame (SOFn) marker carrying dimensions.
        if data[:2] == b"\xff\xd8":
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                i += 2
                # SOF0..SOF15 except DHT(C4)/JPG(C8)/DAC(CC) carry frame size.
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 3 : i + 7])
                    return int(w), int(h)
                if i + 2 > n:
                    break
                seg_len = struct.unpack(">H", data[i : i + 2])[0]
                i += seg_len
            return None, None
        # WebP (RIFF container): VP8 (lossy) / VP8L (lossless) / VP8X (extended).
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = data[12:16]
            if fmt == b"VP8 ":
                w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
                h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return int(w), int(h)
            if fmt == b"VP8L":
                b1, b2, b3, b4 = data[21], data[22], data[23], data[24]
                w = ((b2 & 0x3F) << 8 | b1) + 1
                h = ((b4 & 0x0F) << 10 | b3 << 2 | (b2 & 0xC0) >> 6) + 1
                return int(w), int(h)
            if fmt == b"VP8X":
                w = (data[24] | data[25] << 8 | data[26] << 16) + 1
                h = (data[27] | data[28] << 8 | data[29] << 16) + 1
                return int(w), int(h)
    except Exception:  # pragma: no cover - defensive; dims are best-effort
        return None, None
    return None, None


def _parse_media_markers(content: str) -> tuple[str, list[str]]:
    """Extract `MEDIA:/abs/path` lines from agent text.

    Returns `(cleaned_text, [paths])`. Every marker line is removed from the
    visible text (collapsing the blank-line noise it leaves behind) and its
    path collected in order. Paths are returned verbatim — existence, size,
    and MIME are validated later in `_build_media_attachments`. If no marker
    matches, returns `(content, [])` unchanged.

    The documented contract (PLATFORM_HINTS["bgos"]) promised these are
    "Handled natively by the BGOS adapter" — but until 2026-05-31 the adapter
    never parsed them, so the agent's `MEDIA:` line was posted as literal text
    and the file was never delivered. This is the parser that makes the
    promise true.
    """
    if not content or "MEDIA:" not in content:
        return content, []
    # Split into alternating non-fenced / fenced segments (capturing split →
    # even indices are outside fences, odd indices are the fences themselves).
    # Only scan + strip the non-fenced segments so a `MEDIA:` line inside a
    # ``` block is preserved verbatim.
    segments = _CODE_FENCE_RE.split(content)
    paths: list[str] = []
    for idx in range(0, len(segments), 2):
        seg = segments[idx]
        if "MEDIA:" not in seg:
            continue
        for m in _MEDIA_MARKER_RE.finditer(seg):
            p = m.group(1).strip()
            if p:
                paths.append(p)
        segments[idx] = _MEDIA_MARKER_RE.sub("", seg)
    if not paths:
        # No real (non-fenced) markers — leave the text exactly as-is so any
        # in-fence example MEDIA: line renders untouched.
        return content, []
    cleaned = "".join(segments)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, paths


# camelCase → snake_case aliases for inbound payloads. The BGOS backend's
# WS `inbound_message` event has migrated to camelCase keys (matching the
# rest of the BGOS DTO fleet — assistantId, chatId, messageId,
# messageType, userId), while the REST `/api/v1/integrations/inbound`
# endpoint still returns Python-native snake_case. The adapter was
# originally written for snake_case, so without this shim WS events were
# silently dropped at the `assistant_id is None` check and only the REST
# poll loop's 5-second fallback was actually delivering messages.
# Caught live on kc's server 2026-05-13 — see diag output showing
# `assistantId=885 ... messageId=6041` reaching the adapter while
# data.get("assistant_id") returned None.
_INBOUND_CAMEL_ALIASES: dict[str, str] = {
    "assistantId": "assistant_id",
    "chatId": "chat_id",
    "messageId": "message_id",
    "userId": "user_id",
    "messageType": "message_type",
    "commandName": "command_name",
    "commandArgs": "command_args",
    "replyToId": "reply_to_id",
    "peerConversationId": "peer_conversation_id",
    "turnState": "turn_state",
}


def _normalize_inbound_payload(data: dict) -> dict:
    """Translate camelCase WS event keys to the snake_case shape the
    adapter was originally written for.

    Idempotent: snake_case keys present in `data` take precedence over
    camelCase aliases so backfill paths or future call sites that already
    normalize aren't overridden. Returns the original dict object when no
    aliases need translating (zero-copy fast path).
    """
    if not any(camel in data for camel in _INBOUND_CAMEL_ALIASES):
        return data
    out = dict(data)
    for camel, snake in _INBOUND_CAMEL_ALIASES.items():
        if camel in out and snake not in out:
            out[snake] = out[camel]
    return out

# Strip Telegram MarkdownV2 punctuation escapes that some agents emit out
# of habit. BGOS's mobile renderer is CommonMark; the backslashes show
# through as ugly visible characters otherwise. Match only the
# punctuation-class characters Telegram requires escaping; do NOT match
# CommonMark-meaningful escapes (\\, \*, \_, \[, \], \(, \), \`).
# Punctuation set per Telegram MarkdownV2 spec minus the CommonMark-shared
# ones: , . ! ? : ; @ - + = | # < > { }
_MDV2_LEAK_RE = re.compile(r"\\([,.!?:;@\-+=|#<>{}])")

# Hard length cap for a single message body. The backend's exact limit
# is in flux; this is the comfortable zone for mobile rendering. Longer
# messages get split into (1/N) chunks. Tests can override via
# adapter._max_message_length on the instance.
_DEFAULT_MAX_MESSAGE_LENGTH = 10_000


class UnknownChatTarget(ValueError):
    """Raised when an outbound send/edit/get_chat_info targets a chat the
    adapter never received on an inbound event.

    Server-authoritative chat addressing (2026-05-30 hardening): the adapter
    must never originate a chat id the agent invented. See
    `BGOSAdapter._resolve_outbound_target` and
    docs/bgos-agent-capabilities.md §"Chat addressing".
    """


@dataclass
class MessageEvent:
    """Inbound user event translated for Hermes's `handle_message`.

    Mirrors the shape of `gateway.platforms.base.MessageEvent` closely enough
    that Hermes's downstream agent dispatch accepts it. Fields:
      platform       — always "bgos"
      chat_id        — BGOS chat id
      message_id     — BGOS message id (monotonic per chat; used for dedup)
      user_id        — Clerk/DEV user id of the sender
      assistant_id   — BGOS assistant id the message is addressed to
      agent_route    — Hermes agent route resolved from assistant_id
      text           — message text
      files          — list of {filename, mime, size, url | s3_key, ...}
      message_type   — "standard" | "slash_command" | ...
      command_name   — present when message_type == "slash_command"
      command_args   — raw args string
    """

    platform: str
    chat_id: int
    message_id: int
    user_id: str
    assistant_id: int
    agent_route: str
    text: str
    files: list[dict]
    message_type: str
    command_name: str | None
    command_args: str | None
    reply_to_id: int | None = None
    peer_conversation_id: int | None = None
    turn_state: str | None = None

    @classmethod
    def from_ws(cls, data: dict, *, agent_route: str) -> "MessageEvent":
        return cls(
            platform="bgos",
            chat_id=data["chat_id"],
            message_id=data["message_id"],
            user_id=data.get("user_id", ""),
            assistant_id=data["assistant_id"],
            agent_route=agent_route,
            text=data.get("text", ""),
            files=data.get("files") or [],
            message_type=data.get("message_type", "standard"),
            command_name=data.get("command_name"),
            command_args=data.get("command_args"),
            reply_to_id=data.get("reply_to_id"),
            peer_conversation_id=data.get("peer_conversation_id"),
            turn_state=data.get("turn_state"),
        )

try:  # pragma: no cover - exercised only when Hermes is installed
    if os.environ.get("HERMES_CHANNEL_BGOS_FORCE_MOCK_HERMES"):
        raise ImportError("test suite requested mock Hermes adapter")
    from gateway.config import Platform as _HermesPlatform  # type: ignore
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent as _GatewayMessageEvent,
        MessageType as _GatewayMessageType,
        SendResult,
    )
    _HERMES_BGOS_PLATFORM = getattr(_HermesPlatform, "BGOS", None)
except ImportError:
    # Hermes not installed (e.g. CI without the fork applied) — use the
    # in-repo stub so this module remains importable and testable.
    from tests.mocks.mock_hermes import BasePlatformAdapter, SendResult  # type: ignore
    _HERMES_BGOS_PLATFORM = None
    _GatewayMessageEvent = None  # type: ignore
    _GatewayMessageType = None  # type: ignore


def _format_inbound_files(text: str, files: list[dict]) -> str:
    """Append inbound user attachments to the agent's view of the message.

    Hermes's `MessageEvent` only carries `text` through to the agent's prompt
    — there's no first-class `files`/`attachments` field downstream, so the
    only way the agent learns about images and documents is to surface them
    inside the text. We format images as markdown images (`![name](url)`)
    so vision-capable models can fetch them automatically; other files as
    a labeled link line.

    The backend (BGOS) emits one of `url` (presigned S3, 1h TTL) or `dataUri`
    (`data:<mime>;base64,...`) per file. Older backends without the URL
    plumbing emit neither; we surface those as a placeholder so the agent
    knows files arrived even if it can't fetch them.
    """
    if not files:
        return text

    lines: list[str] = []
    for f in files:
        name = f.get("filename") or f.get("file_name") or "file"
        mime = f.get("mime") or "application/octet-stream"
        url = f.get("url") or f.get("dataUri") or f.get("data_uri")
        if not url:
            lines.append(f"- {name} ({mime}) — [no fetch URL — backend out of date?]")
            continue
        if mime.startswith("image/"):
            lines.append(f"![{name}]({url})")
        else:
            lines.append(f"- [{name}]({url}) ({mime})")

    if not lines:
        return text

    suffix = "\n\n## Attachments from user\n" + "\n".join(lines)
    if text:
        return text + suffix
    return f"[User attached {len(files)} file(s)]" + suffix


def _parse_reply_to_block(content: str) -> tuple[str, int | None]:
    """Extract a `[[BGOS_REPLY_TO]]<id>[[/BGOS_REPLY_TO]]` marker from agent text.

    Returns `(cleaned_text, reply_to_id_or_None)`. The marker is stripped
    from the text before the message is posted so the user never sees it.
    If the marker contains a non-integer payload it's silently dropped
    (text still cleaned). Only the first match is honored — a reply can
    target at most one source message.

    Use case: AI quoting an older user message or its own past commitment.
    See spec BGOS/docs/superpowers/specs/2026-05-19-reply-quote-design.md
    """
    if not content:
        return content, None
    match = _REPLY_TO_BLOCK_RE.search(content)
    if match is None:
        return content, None
    try:
        reply_to_id = int(match.group(1))
    except (ValueError, TypeError):
        reply_to_id = None
    cleaned = _REPLY_TO_BLOCK_RE.sub("", content).strip()
    if reply_to_id is not None and reply_to_id <= 0:
        reply_to_id = None
    return cleaned, reply_to_id


def _parse_buttons_block(content: str) -> tuple[str, list[dict], str | None]:
    """Extract a `[[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]` block from agent text.

    Returns `(cleaned_text, options, render_mode)`. If no block matches,
    returns `(content, [], None)` unchanged. The block is stripped from the
    text and stray blank lines around it are collapsed. Lines inside the
    block shaped `label | value` (pipe-separated) become
    `{"text": label, "callbackData": value}` entries.

    Malformed lines (missing pipe, empty label or value) are skipped and
    logged. If the block is empty after parsing, we return `(cleaned, [],
    None)` so the text still goes through without an options payload.

    Backend caps inline options at 6; we truncate and warn rather than
    letting the POST 400.
    """
    match = _BUTTONS_BLOCK_RE.search(content)
    if match is None:
        return content, [], None

    mode = (match.group(1) or "inline").lower()
    body = match.group(2)
    options: list[dict] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Accept both bullet-prefixed and bare lines: "- Foo | foo" or "Foo | foo".
        if line.startswith(("-", "*")):
            line = line[1:].strip()
        if "|" not in line:
            log.warning(
                "BGOS_BUTTONS: skipping malformed line (missing '|'): %r", raw_line,
            )
            continue
        label, _, value = line.partition("|")
        label = label.strip()
        value = value.strip()
        if not label or not value:
            log.warning(
                "BGOS_BUTTONS: skipping line with empty label or value: %r",
                raw_line,
            )
            continue
        options.append({"text": label, "callbackData": value})

    if len(options) > _INLINE_OPTION_LIMIT:
        log.warning(
            "BGOS_BUTTONS: %d options exceeds inline limit (%d) — truncating",
            len(options), _INLINE_OPTION_LIMIT,
        )
        options = options[:_INLINE_OPTION_LIMIT]

    # Strip the block from the text and tidy up resulting blank-line noise.
    cleaned = (content[: match.start()] + content[match.end():])
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if not options:
        # Block was empty or fully malformed — drop options/mode so we don't
        # send an empty payload the backend would reject.
        return cleaned, [], None
    return cleaned, options, mode


# Hermes gateway emits tool-progress as emoji-prefixed text via edit_message.
# The shape is one of:
#   "🔍 search_files: \"approval|exec_approval\""
#   "💻 terminal: \"echo 'hi'\""
#   "📖 read_file: \"/etc/hostname\""
#   "⚡ default_tool ..."
# We match a leading emoji + ASCII tool_name + optional ":"/space + the
# rest as args. Any leading whitespace is tolerated. Args are quote-stripped
# and truncated to 120 chars to fit the backend's MaxLength validator on
# ToolProgressEntryDto.args. Returns None when the text doesn't look like
# a tool-progress line — the adapter then falls back to the regular streaming
# edit path. Spec:
#   docs/superpowers/specs/2026-05-15-tool-progress-message-type-design.md
# `icon` is constrained to Unicode emoji ranges (Miscellaneous Symbols,
# Dingbats, Emoticons + Supplemental Symbols & Pictographs, etc.) plus
# the variation-selector / ZWJ joiner used in ZWJ sequences. The earlier
# `[^\x00-\x7F]` form was too broad — it matched CJK ideographs, accented
# Latin, etc., misclassifying multilingual agent text as tool-progress.
_TOOL_PROGRESS_RE = re.compile(
    r'^\s*'
    r'(?P<icon>'
    r'[\U00002600-\U000027BF'          # Misc Symbols + Dingbats
    r'\U0001F300-\U0001FAFF'           # Emoticons / Symbols & Pictographs
    r'\U0001F1E6-\U0001F1FF'           # Regional Indicator (flags)
    r'‍️]'                   # ZWJ + variation selector glue
    r'{1,8})\s+'                       # up to 8 to accommodate ZWJ sequences
    r'(?P<name>[A-Za-z][A-Za-z0-9_]*)' # canonical tool name
    r'(?:\s*[:\s]\s*(?P<args>.*))?$',  # optional : or space + args
    re.DOTALL,
)


def _parse_tool_progress_line(line: str) -> dict | None:
    """Parse a single line of gateway tool-progress text into a structured
    entry: {icon, name, args, status='done'} or None if the line doesn't
    match the expected emoji-prefixed shape.

    Status defaults to 'done' — each tool-progress line the gateway emits
    represents a completed tool call. The CARD's outer state distinguishes
    "still running with N tools so far" from "all tools complete".
    """
    line = line.strip()
    if not line:
        return None
    m = _TOOL_PROGRESS_RE.match(line)
    if not m:
        return None
    icon = m.group("icon").strip()
    name = m.group("name").strip()
    args = (m.group("args") or "").strip()
    if len(args) >= 2 and args[0] == args[-1] and args[0] in ('"', "'"):
        args = args[1:-1]
    if len(args) > 120:
        args = args[:117] + "…"
    return {"icon": icon, "name": name, "args": args, "status": "done"}


def _parse_tool_progress_text(text: str | None) -> list[dict] | None:
    """Parse the gateway's tool-progress content into the full list of
    accumulated tool entries, or None when no line looks like a tool-
    progress emit.

    The gateway accumulates tool lines into one bubble (see upstream
    gateway/run.py:14454 — `full_text = "\\n".join(progress_lines)`),
    so subsequent edit_message calls deliver the WHOLE list each time.
    We mirror that semantic: parse every line, return the list, and the
    handler REPLACES the tracked tools rather than appending. That way
    we never drop entries on multi-line edits and we don't need dedup.

    Returns:
      - None if `text` is empty, or no non-blank line matches the tool-
        progress shape (caller falls back to the regular streaming path).
      - A list of {icon, name, args, status} dicts when at least one line
        parses. Lines that DON'T parse are silently dropped — the gateway
        sometimes appends `(×N)` dedup tails (gateway/run.py:14416), and
        those won't match our regex, but the previous tool entries we
        captured are still authoritative.
    """
    if not text:
        return None
    entries: list[dict] = []
    for raw_line in text.splitlines():
        parsed = _parse_tool_progress_line(raw_line)
        if parsed is not None:
            entries.append(parsed)
    if not entries:
        return None
    return entries


def _build_tool_progress_summary(tools: list[dict], *, done: bool) -> str:
    """One-line summary text used as the card's `text` field. Legacy
    clients (no tool_progress rendering) display this; structured clients
    show the card body instead.
    """
    count = len(tools)
    if count == 0:
        return "Working…" if not done else "No tools used"
    names = [t.get("name", "?") for t in tools][:4]
    verb = (
        f"Used {count} tool{'s' if count != 1 else ''}"
        if done
        else f"Using {count} tool{'s' if count != 1 else ''}"
    )
    summary = f"{verb} · {', '.join(names)}"
    if count > 4:
        summary += f", +{count - 4} more"
    return summary


def _send_result(*, message_id: int | None) -> SendResult:
    """Construct a SendResult using the fork's actual field names.

    Fork's SendResult wants `success=True` and `message_id: str | None` (see
    gateway/platforms/base.py); earlier drafts used `ok=True` and passed an
    int. This helper centralizes the adaptation so all send paths stay
    consistent. Test-mode SendResult (tests/mocks/mock_hermes.py) accepts
    either field name because it's a dataclass we control — we mirror the
    real fork there.
    """
    mid = str(message_id) if message_id is not None else None
    try:
        return SendResult(success=True, message_id=mid)  # type: ignore[call-arg]
    except TypeError:
        # Older mock path (kept for backwards compat on any third-party test
        # suite that hasn't updated to the `success=` name yet)
        return SendResult(ok=True, message_id=mid)  # type: ignore[call-arg]

log = logging.getLogger(__name__)


class BGOSAdapter(BasePlatformAdapter):
    """Hermes channel adapter for BGOS.

    Lifecycle:
      1. Hermes instantiates `BGOSAdapter(config)` (where `config` is Hermes's
         own config object, NOT our `BgosConfig`). The shim in the fork reads
         the BGOS-specific fields off `config` and constructs a `BgosConfig`.
         For test convenience, this class accepts a `BgosConfig` directly.
      2. Gateway calls `await adapter.connect()` — we fetch the pairing scope
         via `GET /integrations/me`, build the assistant→route map, and open
         the Socket.IO connection.
      3. Inbound events flow via `_handle_inbound` / `_handle_callback` (wired
         in Tasks 5 and 8). Outbound goes through `send()` + optional
         `send_image/voice/video/document/animation` (Task 6) +
         `send_exec_approval` (Task 7).
      4. Gateway calls `await adapter.disconnect()` on shutdown.
    """

    platform_name = "bgos"

    def __init__(self, config: Any = None) -> None:
        """Hermes's gateway passes its own per-platform config object here —
        not our BgosConfig dataclass. We normalize both shapes: if given a
        BgosConfig, use it directly; otherwise resolve from the Hermes
        config / env vars / secrets file in that priority order.

        Super signature: real Hermes `BasePlatformAdapter.__init__(config,
        platform)` requires both args; the in-repo mock accepts `*args,
        **kwargs`. We pass `Platform.BGOS` when Hermes is importable,
        fall back otherwise.
        """
        hermes_platform = _HERMES_BGOS_PLATFORM
        if hermes_platform is None:
            try:
                from gateway.config import Platform as _RuntimeHermesPlatform  # type: ignore
                hermes_platform = _RuntimeHermesPlatform("bgos")  # plugin-registered pseudo-member
            except Exception:
                hermes_platform = None
        if hermes_platform is not None:
            super().__init__(config, hermes_platform)
        else:
            # If real Hermes is importable before the BGOS plugin has registered
            # Platform('bgos'), BasePlatformAdapter still requires (config,
            # platform). A string platform is sufficient for the base class and
            # keeps tests/import-order probes constructible. The in-repo mock
            # also accepts these args.
            super().__init__(config, "bgos")
        bgos_config = self._resolve_config(config)
        self._config = bgos_config
        self._api = BgosApi(bgos_config)
        self._state = StateStore()
        self._consumed_peer_wait_replies_mtime_ns: int | None = None
        self._load_consumed_peer_wait_replies()
        self._ws: BgosWs | None = None
        self.pairing_id: int | None = None
        # The user who owns this pairing. Populated in connect() from
        # /api/v1/integrations/me. Used as the canonical fallback for
        # PATCH userId when last_user_id_by_chat is empty for a chat
        # (happens when the first inbound on a chat was a REST backfill
        # entry, which carries no user_id — caught live 2026-05-15 with
        # 0.6.1 returning 400 on tool_progress card PATCH). All chats
        # under this pairing belong to assistants owned by this user,
        # so the ownership check in UpdateMessageDto always passes.
        self.pairing_user_id: str | None = None
        # Approval bookkeeping — approval_id → session_key. Populated by
        # send_exec_approval, drained by Task 8's callback router.
        self._approval_state: dict[int, str] = {}
        self._approval_id_counter = itertools.count(1)
        # Slash-confirm bookkeeping — confirm_id → session_key. Populated
        # by send_slash_confirm; drained by the sc:* branch of
        # _handle_callback. Telegram-parity 3-button UX for non-destructive
        # but expensive commands (current caller: /reload-mcp).
        self._slash_confirm_state: dict[str, str] = {}
        # REST poll loop — fallback for server-side WS push gap (see
        # _poll_loop for details). Started in connect(), cancelled in
        # disconnect().
        self._poll_task: asyncio.Task | None = None
        # edit_message throttle — mirrors Telegram's
        # _PROGRESS_EDIT_INTERVAL=1.5 (gateway/run.py:14382). Keeps the
        # BGOS backend from drowning under tool-progress / streaming
        # edits when the agent emits many small chunks. Per-chat so
        # high-traffic chats don't starve quieter ones.
        self._edit_throttle_seconds: float = 1.5
        self._pending_edits: dict[int, asyncio.Task] = {}
        self._last_edit_at: dict[int, float] = {}
        # chat_id -> (message_id, cleaned_text, options, render_mode) —
        # the latest content waiting for the deferred flush. Later
        # writes inside the window supersede earlier ones (coalesce).
        self._pending_edit_content: dict[
            int, tuple[int, str, list[dict] | None, str | None],
        ] = {}
        # tool_progress card tracking — chat_id → message_id of the
        # currently-active "Tool calls" card. Populated on first emoji
        # edit_message intercept of an agent turn, transitioned to
        # state='done' when delete_message fires on the streaming preview
        # (signals end-of-turn). Mirror dict holds the accumulated tools
        # list so PATCHes can rebuild the full options payload. Cleared
        # together at end of turn. Spec:
        #   docs/superpowers/specs/2026-05-15-tool-progress-message-type-design.md
        self._tool_progress_card_id_by_chat: dict[int, int] = {}
        self._tool_progress_tools_by_chat: dict[int, list[dict]] = {}
        # Maps the gateway's streaming-preview message_id to the chat_id —
        # so when delete_message fires on a preview, we know which card to
        # finalize. Cleared after card transitions to done.
        self._tool_progress_preview_to_chat: dict[int, int] = {}
        # Per-chat asyncio.Lock guarding the check-then-POST-then-set
        # critical section in _handle_tool_progress_edit. Without this,
        # two concurrent edit_message calls for the same chat both observe
        # `card_id_by_chat is None`, both POST a fresh card, and one is
        # orphaned (state=running forever, never finalized). Reviewer flag
        # 2026-05-15.
        self._tool_progress_lock_by_chat: dict[int, asyncio.Lock] = {}
        # Adaptive text-batching for rapid inbound user messages. Mobile
        # clients sometimes split a long transcription or paste into multiple
        # sub-4KB messages — without batching, the agent gets N separate
        # dispatches and writes N separate replies. See
        # gateway/platforms/telegram.py:3803-3859 for the upstream pattern.
        # Window adapts to the LAST chunk's length (see _enqueue_text_batch).
        self._text_batch_window: float = 0.6
        self._pending_text_batches: dict[int, dict[str, Any]] = {}
        self._pending_text_tasks: dict[int, asyncio.Task] = {}
        # Hard cap for a single outbound message body. Messages longer
        # than this get split into multiple chunks with `(i/N)`
        # continuation suffixes (mirrors Telegram parity at
        # gateway/platforms/telegram.py:1457).
        self._max_message_length: int = _DEFAULT_MAX_MESSAGE_LENGTH
        # Pairing-scope hot-refresh (v0.8.0). When inbound arrives for an
        # assistant_id we don't recognize — almost always because the user
        # exposed a new agent in the BGOS Integrations UI *after* the gateway
        # started — we re-fetch whoami and reconcile the route map in place
        # instead of dropping the message until the next restart.
        # _scope_refresh_lock serializes concurrent refreshes (live WS + the
        # REST poll loop can both hit an unknown id at once);
        # _last_scope_refresh + _scope_refresh_cooldown rate-limit whoami so a
        # genuinely-unknown id can't trigger a fetch on every poll tick.
        self._scope_refresh_lock = asyncio.Lock()
        self._last_scope_refresh: float = 0.0
        self._scope_refresh_cooldown: float = float(
            os.environ.get("BGOS_SCOPE_REFRESH_COOLDOWN", "10")
        )

    @staticmethod
    def _resolve_config(hermes_config: Any) -> BgosConfig:
        """Resolve a BgosConfig from multiple sources, in priority order:

        1. If `hermes_config` is already a BgosConfig, return as-is.
        2. Attributes on `hermes_config` (api_key or pairing_token; base_url
           or backend_url) — populated by Hermes's config loader (see
           gateway/config.py's `_apply_env_overrides` BGOS block).
        3. Env vars BGOS_API_KEY + BGOS_BACKEND_URL.
        4. Secrets file at `$HERMES_HOME/secrets/bgos.json` (default
           `~/.hermes/secrets/bgos.json`) written by `hermes-pair-bgos`.
        5. Default base_url to production.

        Raises RuntimeError with a clear message if no pairing token is
        found anywhere — the user needs to run `hermes-pair-bgos <CODE>`.
        """
        if isinstance(hermes_config, BgosConfig):
            return hermes_config

        def _attr(obj: Any, name: str) -> Any:
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj.get(name)
            return getattr(obj, name, None)

        # Load secrets file if present
        secrets: dict[str, Any] = {}
        hermes_home = Path(
            os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")),
        )
        secrets_path = hermes_home / "secrets" / "bgos.json"
        if secrets_path.is_file():
            try:
                secrets = json.loads(secrets_path.read_text())
            except (OSError, json.JSONDecodeError):
                log.warning(
                    "BGOS secrets file at %s is unreadable — ignoring",
                    secrets_path,
                )
                secrets = {}

        pairing_token = (
            _attr(hermes_config, "api_key")
            or _attr(hermes_config, "pairing_token")
            or os.environ.get("BGOS_API_KEY")
            or secrets.get("pairing_token")
        )
        if not pairing_token:
            raise RuntimeError(
                "BGOS pairing token not found. Run "
                "`hermes-pair-bgos <CODE> --device-label <label>` to pair, "
                "or set the BGOS_API_KEY environment variable."
            )

        base_url = (
            _attr(hermes_config, "base_url")
            or _attr(hermes_config, "backend_url")
            or os.environ.get("BGOS_BACKEND_URL")
            or secrets.get("base_url")
            or "https://api.brandgrowthos.ai"
        )

        return BgosConfig(base_url=base_url, pairing_token=pairing_token)

    # -------------------------------------------------------------------------
    # Lifecycle (abstract on BasePlatformAdapter)
    # -------------------------------------------------------------------------

    async def connect(self) -> bool:
        """Fetch pairing scope, build route map, open the Socket.IO connection,
        and push the agent catalog so the BGOS Integrations UI shows the
        available-to-bind agents.

        Propagates `BgosApiError` on failure (notably 401 / PAIRING_REVOKED so
        the caller can clear stored secrets and prompt for re-pair).
        """
        me = await self._api.whoami()
        self.pairing_id = me["pairing_id"]
        # Cache the pairing owner's user_id for use as the PATCH /messages
        # userId fallback when last_user_id_by_chat is empty. The whoami
        # response always carries it (pairing.controller.ts returns
        # `user_id: pairing.userId`). Falls back to None gracefully if a
        # mock or older backend omits it — PATCH still fails in that case
        # but no worse than before.
        self.pairing_user_id = me.get("user_id") or me.get("userId")
        # Backend /api/v1/integrations/me returns assistants with the key
        # `assistant_id` (see backend/src/integrations/pairing.controller.ts
        # line ~128: `assistant_id: Number(r.id)`). Earlier drafts guessed
        # `id` and silently failed with KeyError on real traffic. The fall-
        # back to `entry.get("id")` keeps old-shape mocks in third-party
        # test suites working, but the canonical key is `assistant_id`.
        for entry in me.get("assistants", []):
            assistant_id = entry.get("assistant_id", entry.get("id"))
            if assistant_id is None or entry.get("agent_route") is None:
                log.warning(
                    "whoami assistant entry missing fields (skipping): %s",
                    entry,
                )
                continue
            self._state.set_route(assistant_id, entry["agent_route"])

        self._ws = BgosWs(
            self._config,
            on_inbound_message=self._handle_inbound,
            on_callback_result=self._handle_callback,
            on_reconnect=self._on_reconnect,
            on_inbound_click=self._handle_inbound_click,
        )
        if self.pairing_id is not None:
            self._ws.bind_pairing(self.pairing_id)
        self._ws.bind_assistants(list(self._state.assistant_route.keys()))
        await self._ws.start()

        # Publish the agent catalog so the BGOS user can tick which Hermes
        # agents to expose. Fail-open: an empty catalog is valid — the user
        # can either set BGOS_AGENTS env var next time or bind via curl.
        await self._push_agent_catalog_safe()

        # Push each bound assistant's slash-command manifest so the BGOS
        # composer's slash picker has commands to show. Without this the
        # picker is empty for Hermes agents (the catalog was never synced —
        # sync_commands_for had no caller). Each call is fail-open.
        for assistant_id in list(self._state.assistant_route.keys()):
            await self.sync_commands_for(assistant_id)

        # Surface bound-assistant state explicitly so operators see at a glance
        # whether any agents are exposed yet. Zero is the common fresh-install
        # state (catalog pushed, user hasn't ticked agents in the UI) — and
        # with hot-refresh that resolves on its own, no restart required.
        bound = sorted(self._state.assistant_route.items())
        if bound:
            log.info(
                "BGOS bound assistants: %s",
                ", ".join(f"{aid}:{route}" for aid, route in bound),
            )
        else:
            log.warning(
                "BGOS: 0 assistants exposed yet — open BGOS Integrations → "
                "Hermes → tick agent(s) → Save. New exposures hot-load "
                "automatically (no gateway restart needed)."
            )

        # Replay any messages that arrived while the adapter was down. The
        # cursor comes from the persisted last-id file ($HERMES_HOME/
        # bgos_last_id) — WITHOUT persistence every restart would backfill
        # from 0, replaying all history and sending duplicate agent replies
        # indefinitely. Fresh installs start at 0 (replay once), then the
        # file auto-advances as messages flow through _handle_inbound.
        asyncio.create_task(self._run_backfill(self._load_last_id()))

        # Start the REST poll loop. Server currently doesn't deliver
        # inbound_message over WS to integration sockets — the join-room
        # ack is silently dropped, so io.to("assistant:<id>").emit(...)
        # reaches nobody. Until that's fixed server-side, we poll REST
        # every BGOS_POLL_INTERVAL seconds (default 5s).
        poll_interval = float(os.environ.get("BGOS_POLL_INTERVAL", "5"))
        self._poll_task = asyncio.create_task(self._poll_loop(poll_interval))
        return True

    async def _refresh_pairing_scope(self) -> bool:
        """Re-fetch the pairing scope from `GET /api/v1/integrations/me` and
        reconcile the in-process assistant→route map (and WS room bindings)
        with what BGOS now reports.

        Called from `_handle_inbound` when a message arrives for an
        assistant_id we don't recognize — the common case being the user
        exposing a new agent in the BGOS Integrations UI *after* the gateway
        started. Lets new assistants hot-load without a gateway restart.

        Rate-limited: serialized by `_scope_refresh_lock` and gated by
        `_scope_refresh_cooldown` (env `BGOS_SCOPE_REFRESH_COOLDOWN`, default
        10s) so a flood of inbound for a genuinely-unknown id can't hammer
        whoami. Fail-open — any error is logged and the route map left as-is.
        Mirrors `connect()` (route map + WS bind + pairing_user_id); it does
        NOT sync per-assistant command manifests, since connect() doesn't.

        Returns True if the route map changed.
        """
        async with self._scope_refresh_lock:
            now = time.monotonic()
            if self._last_scope_refresh and (
                now - self._last_scope_refresh < self._scope_refresh_cooldown
            ):
                return False
            self._last_scope_refresh = now

            try:
                me = await self._api.whoami()
            except Exception:
                log.exception("pairing-scope refresh: whoami failed")
                return False

            if self.pairing_user_id is None:
                self.pairing_user_id = me.get("user_id") or me.get("userId")

            new_routes: dict[int, str] = {}
            for entry in me.get("assistants", []):
                aid = entry.get("assistant_id", entry.get("id"))
                route = entry.get("agent_route")
                if aid is None or route is None:
                    continue
                new_routes[aid] = route

            old_ids = set(self._state.assistant_route.keys())
            new_ids = set(new_routes.keys())
            added = sorted(new_ids - old_ids)
            removed = sorted(old_ids - new_ids)
            if not added and not removed:
                return False

            for aid in added:
                self._state.set_route(aid, new_routes[aid])
            for aid in removed:
                self._state.remove_assistant(aid)
                if self._ws is not None:
                    self._ws.unbind_assistant(aid)
            if self._ws is not None and added:
                self._ws.bind_assistants(list(new_routes.keys()))

            # Sync the slash-command manifest for newly-bound assistants so
            # the picker populates without a restart (mirrors connect()).
            for aid in added:
                await self.sync_commands_for(aid)

            log.info(
                "bgos scope refreshed: added=%s removed=%s bound=%s",
                added, removed, sorted(new_ids),
            )
            return True

    # -------------------------------------------------------------------------
    # Last-seen message-id persistence — prevents duplicate-replay on restart.
    # Lives at $HERMES_HOME/bgos_last_id. Monotonically advances; never
    # regresses. Read on connect(), written after every successful inbound
    # (both WS live and REST backfill).
    # -------------------------------------------------------------------------

    def _last_id_path(self) -> Path:
        hermes_home = Path(
            os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")),
        )
        return hermes_home / "bgos_last_id"

    def _consumed_peer_wait_replies_path(self) -> Path:
        hermes_home = Path(
            os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")),
        )
        return hermes_home / "bgos_consumed_peer_wait_replies.json"

    def _load_consumed_peer_wait_replies(self) -> None:
        path = self._consumed_peer_wait_replies_path()
        try:
            stat = path.stat()
            raw = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            return
        if isinstance(raw, list):
            self._state.consumed_peer_wait_replies = [
                item for item in raw if isinstance(item, dict)
            ][-200:]
            self._consumed_peer_wait_replies_mtime_ns = stat.st_mtime_ns

    def _refresh_consumed_peer_wait_replies_if_changed(self, *, force: bool = False) -> None:
        path = self._consumed_peer_wait_replies_path()
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        if force or self._consumed_peer_wait_replies_mtime_ns != mtime_ns:
            self._load_consumed_peer_wait_replies()

    @staticmethod
    def _peer_wait_receipt_key(receipt: dict) -> tuple[Any, Any, Any, Any, Any]:
        return (
            receipt.get("conversationId"),
            receipt.get("sideThreadChatId"),
            receipt.get("sentMessageId"),
            receipt.get("returnedReplyMessageId"),
            receipt.get("createdAt"),
        )

    def _persist_consumed_peer_wait_replies(self) -> None:
        try:
            path = self._consumed_peer_wait_replies_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{id(self)}.tmp")
            tmp.write_text(json.dumps(self._state.consumed_peer_wait_replies[-200:]))
            tmp.replace(path)
            self._consumed_peer_wait_replies_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            log.warning("could not persist BGOS consumed peer wait replies")

    def _record_consumed_peer_wait_reply(self, receipt: dict) -> None:
        now = int(time.time())
        clean = {
            "pending": bool(receipt.get("pending")),
            "conversationId": receipt.get("conversationId"),
            "sideThreadChatId": receipt.get("sideThreadChatId"),
            "sentMessageId": receipt.get("sentMessageId"),
            "returnedReplyMessageId": receipt.get("returnedReplyMessageId"),
            "callerAssistantId": receipt.get("callerAssistantId"),
            "targetAssistantId": receipt.get("targetAssistantId"),
            "parentMessageId": receipt.get("parentMessageId"),
            "createdAt": receipt.get("createdAt") or now,
        }
        self._refresh_consumed_peer_wait_replies_if_changed()
        existing = [
            item for item in self._state.consumed_peer_wait_replies
            if self._peer_wait_receipt_key(item) != self._peer_wait_receipt_key(clean)
        ]
        if not clean["pending"] and clean.get("callerAssistantId") is None:
            # Upgrade/remove this process's pending marker for the same target
            # once the HTTP response gives us concrete message ids.
            target_assistant_id = self._int_or_none(clean.get("targetAssistantId"))
            existing = [
                item for item in existing
                if not (
                    item.get("pending")
                    and self._int_or_none(item.get("targetAssistantId")) == target_assistant_id
                )
            ]
        existing.append(clean)
        self._state.consumed_peer_wait_replies = existing
        self._state.consumed_peer_wait_replies = self._state.consumed_peer_wait_replies[-200:]
        self._persist_consumed_peer_wait_replies()

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _is_consumed_peer_wait_reply(self, data: dict) -> bool:
        self._refresh_consumed_peer_wait_replies_if_changed()
        msg_id = self._int_or_none(data.get("message_id"))
        chat_id = self._int_or_none(data.get("chat_id"))
        reply_to_id = self._int_or_none(data.get("reply_to_id"))
        conversation_id = self._int_or_none(data.get("peer_conversation_id"))
        now = int(time.time())
        for receipt in list(self._state.consumed_peer_wait_replies):
            created_at = self._int_or_none(receipt.get("createdAt"))
            if created_at is not None and now - created_at > 300:
                continue
            if receipt.get("pending"):
                # Pending markers are advisory only: they tell the adapter a
                # cross-process waitForReply may soon persist a concrete
                # consumed-reply receipt. They must not suppress by themselves,
                # because legitimate later peer turns can share the same caller
                # assistant/conversation shape. _wait_for_consumed_peer_wait_reply_race()
                # gives helpers a short window to upgrade pending -> concrete,
                # then this method drops only on exact message/reply IDs.
                continue
            side_thread_chat_id = self._int_or_none(receipt.get("sideThreadChatId"))
            if side_thread_chat_id is not None and chat_id != side_thread_chat_id:
                continue
            receipt_conversation_id = self._int_or_none(receipt.get("conversationId"))
            if (
                receipt_conversation_id is not None
                and conversation_id is not None
                and conversation_id != receipt_conversation_id
            ):
                continue
            returned_reply_id = self._int_or_none(receipt.get("returnedReplyMessageId"))
            sent_message_id = self._int_or_none(receipt.get("sentMessageId"))
            if returned_reply_id is not None and msg_id == returned_reply_id:
                return True
            if sent_message_id is not None and reply_to_id == sent_message_id:
                return True
        return False

    def _has_pending_peer_wait_marker(self, data: dict) -> bool:
        """Return True when a helper is actively waiting for this assistant's peer reply.

        A pending marker is written before the blocking peer HTTP request. It
        does not contain sideThreadChatId/messageId yet, so it is not safe to
        suppress on its own. It is safe to briefly delay likely peer-wait
        messages addressed to the caller assistant so the helper can upgrade
        pending -> concrete receipt before we dispatch to Hermes.
        """
        self._refresh_consumed_peer_wait_replies_if_changed()
        assistant_id = self._int_or_none(data.get("assistant_id"))
        now = int(time.time())
        for receipt in list(self._state.consumed_peer_wait_replies):
            if not receipt.get("pending"):
                continue
            created_at = self._int_or_none(receipt.get("createdAt"))
            if created_at is not None and now - created_at > 60:
                continue
            caller_assistant_id = self._int_or_none(receipt.get("callerAssistantId"))
            if caller_assistant_id is not None and assistant_id == caller_assistant_id:
                return True
        return False

    @staticmethod
    def _looks_like_peer_wait_reply(data: dict) -> bool:
        return (
            data.get("message_type") == "standard"
            and (
                data.get("turn_state") == "expecting_reply"
                or data.get("peer_conversation_id") is not None
            )
        )

    async def _wait_for_consumed_peer_wait_reply_race(self, data: dict) -> bool:
        if not self._looks_like_peer_wait_reply(data):
            return False
        # Cross-process waitForReply helpers can receive the HTTP reply and
        # persist the consumed receipt shortly after this live gateway receives
        # the same side-thread message over WS. If a pending marker exists for
        # this caller assistant, hold the likely reply briefly and poll for the
        # concrete returnedReplyMessageId before dispatching it to Hermes.
        deadline = time.monotonic() + (1.25 if self._has_pending_peer_wait_marker(data) else 0.05)
        while True:
            await asyncio.sleep(0.05)
            self._refresh_consumed_peer_wait_replies_if_changed(force=True)
            if self._is_consumed_peer_wait_reply(data):
                return True
            if time.monotonic() >= deadline:
                return False

    @staticmethod
    def _peer_wait_data_from_event(event: "MessageEvent") -> dict:
        """Reconstruct peer-wait matching fields retained on a queued event.

        Admission-time suppression can miss a cross-process waitForReply receipt
        that is persisted after the websocket event is enqueued for text
        batching. Re-checking at flush time needs the original BGOS message
        identifiers, not just the merged text.
        """
        return {
            "chat_id": getattr(event, "chat_id", None),
            "message_id": getattr(event, "message_id", None),
            "reply_to_id": getattr(event, "reply_to_id", None),
            "peer_conversation_id": getattr(event, "peer_conversation_id", None),
            "message_type": getattr(event, "message_type", None),
            "turn_state": getattr(event, "turn_state", None),
        }

    def _load_last_id(self) -> int:
        try:
            return int(self._last_id_path().read_text().strip())
        except (OSError, ValueError):
            return 0

    def _save_last_id(self, message_id: int) -> None:
        if not isinstance(message_id, int) or message_id <= 0:
            return
        try:
            path = self._last_id_path()
            if message_id > self._load_last_id():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(message_id))
        except OSError:
            # Disk full / read-only / permission denied — don't crash the
            # connect; worst case we replay on next restart.
            log.warning("could not persist bgos_last_id=%d", message_id)

    async def _push_agent_catalog_safe(self) -> None:
        if self.pairing_id is None:
            return
        try:
            agents = self._enumerate_agents()
        except Exception:
            log.exception("agent enumeration failed — skipping catalog push")
            return
        if not agents:
            log.warning(
                "no Hermes agents discovered — Hermes Integrations card will "
                "show an empty catalog. Set BGOS_AGENTS "
                "(e.g. 'hades:Hades,ramy:Ramy') or BGOS_AGENTS_JSON env var."
            )
            return
        try:
            await self._api.push_agent_catalog(
                pairing_id=self.pairing_id, entries=agents,
            )
            log.info(
                "BGOS catalog pushed: %s",
                ", ".join(
                    f"{a['agent_route']}:{a.get('name', a['agent_route'])}"
                    for a in agents
                ),
            )
        except Exception:
            log.exception("agent catalog push failed (non-fatal)")

    def _enumerate_agents(self) -> list[dict]:
        """Discover Hermes's configured agents for the agent-catalog push.

        Delegates to `agents.enumerate_agents_from_env`, the single source of
        truth for the `BGOS_AGENTS_JSON` / `BGOS_AGENTS` precedence and the
        `route:Display Name` spec format (also used by the pair CLI's
        `--agents` flag and the doctor). Returns `[]` when nothing is
        configured — callers treat that as warn-but-continue, not an error.
        """
        return enumerate_agents_from_env()

    async def _poll_loop(self, interval: float) -> None:
        """Poll REST inbound every `interval` seconds.

        Fallback for the server-side WS push gap: the backend currently does
        not emit `inbound_message` to integration sockets — the join-room
        message (`42["join",{"room":"assistant:<id>"}]`) receives no
        acknowledgement and sockets are never added to rooms, so
        `io.to("assistant:<id>").emit(...)` reaches nobody. Until the server
        joins integration sockets into their rooms, this poll is the only
        way we see inbound user messages.

        Cancel-safe. Crashes are logged but don't kill the adapter — the
        gateway would have no other way to recover inbound traffic.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                await self._run_backfill(self._load_last_id())
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("poll_loop crashed unexpectedly")

    async def disconnect(self) -> None:
        """Idempotent — safe to call multiple times (e.g. from a signal handler).

        Cancels any pending edit-throttle flushes and awaits their
        completion BEFORE closing the api client, so cancelled tasks
        don't get a chance to hit a closed httpx client (which would
        raise `RuntimeError: Cannot send a request...` deep in httpx).
        """
        # Snapshot + cancel pending edit flushes before clearing the dict
        # so iteration doesn't race with a flush mutating it.
        pending_tasks = [
            t for t in self._pending_edits.values() if not t.done()
        ]
        for task in pending_tasks:
            task.cancel()
        self._pending_edits.clear()
        self._pending_edit_content.clear()
        # Await cancelled tasks so they fully unwind before we close the
        # underlying api client. Exceptions are intentionally swallowed —
        # we already initiated the cancellation, and CancelledError /
        # any teardown noise is expected here.
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        # Cancel any pending text-batch flushes too. Same snapshot-then-
        # cancel pattern as the edit flushes above — protects against a
        # flush task racing with disconnect() and trying to call
        # handle_message after the adapter has torn down.
        pending_text_tasks = [
            t for t in self._pending_text_tasks.values() if not t.done()
        ]
        for task in pending_text_tasks:
            task.cancel()
        if pending_text_tasks:
            await asyncio.gather(*pending_text_tasks, return_exceptions=True)
        self._pending_text_tasks.clear()
        self._pending_text_batches.clear()
        # tool_progress card tracking — clear so a reconnect doesn't reuse
        # a card id from before the disconnect (the gateway would never
        # signal end-of-turn for that card on the new session, and the
        # frontend would see the stale card stuck in state=running).
        self._tool_progress_card_id_by_chat.clear()
        self._tool_progress_tools_by_chat.clear()
        self._tool_progress_preview_to_chat.clear()
        self._tool_progress_lock_by_chat.clear()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._ws is not None:
            try:
                await self._ws.stop()
            finally:
                self._ws = None
        await self._api.close()

    async def _assistant_id_for_chat(self, chat_id: int) -> int | None:
        """Return the BGOS assistant that owns `chat_id`.

        For normal chats this lets us use `/api/v1/send-message` consistently.
        For kind='a2a' side threads it is REQUIRED: `/api/v1/messages` only
        saves a visible bubble, while `/api/v1/send-message` runs BGOS's
        peer-conversation bridge so replies are delivered to the other agent
        and the peer lifecycle/turn state is updated.
        """
        # Do not trust the inbound event's addressed assistant id as the
        # chat owner. In BGOS a2a side threads can be owned by the peer chat
        # while the inbound event is addressed to this Hermes assistant. Using
        # that addressed id in /send-message makes the backend skip the peer
        # bridge (`chat.assistantId !== senderAssistantId`), so replies become
        # visible bubbles that the initiating peer never sees. Always refresh
        # from GET /chats/:id when possible; keep the cache only as a legacy
        # fallback for older/self-hosted backends.
        cached = self._state.assistant_id_by_chat.get(chat_id)
        try:
            chat = await self._api.get_chat(chat_id)
        except Exception:
            log.debug("failed to resolve assistant_id for chat=%s", chat_id, exc_info=True)
            return cached
        assistant_id = chat.get("assistantId", chat.get("assistant_id")) if isinstance(chat, dict) else None
        try:
            assistant_id_int = int(assistant_id)
        except (TypeError, ValueError):
            return None
        self._state.assistant_id_by_chat[chat_id] = assistant_id_int
        return assistant_id_int

    def format_message(self, content: str) -> str:
        """Translate the agent's outbound text into BGOS-native form.

        BGOS's mobile app renders CommonMark. Telegram-tuned prompts often
        emit MarkdownV2 punctuation escapes (`\\,` `\\!` `\\.` etc.) that
        survive as visible backslashes here. Strip those defensively; leave
        real CommonMark escapes (\\\\, \\*, \\_, \\[, \\], \\(, \\), \\`)
        alone since users may legitimately want them.

        Idempotent — re-running has no effect after the first pass.
        """
        return _MDV2_LEAK_RE.sub(r"\1", content)

    def _resolve_outbound_target(
        self, chat_id: int | str,
    ) -> tuple[int, str | None]:
        """Validate an agent-supplied outbound `chat_id` and resolve its
        opaque `sessionHandle`.

        Server-authoritative chat addressing (2026-05-30 hardening): the
        adapter must NEVER originate a chat id the agent invented. Every
        outbound `send` / `edit_message` / `get_chat_info` target MUST be a
        chat the adapter actually received on a prior inbound MessageEvent
        (tracked in `StateStore.received_chat_ids`). If the agent supplies a
        chat the adapter never saw, raise `UnknownChatTarget` so the caller
        rejects the dispatch with a clear error instead of POSTing a guessed
        id.

        Returns `(chat_key, session_handle)`. When the inbound that seeded
        this chat carried a `sessionHandle`, it's returned so the outbound
        POST can send it back (the backend prioritizes `sessionHandle` over a
        raw `chatId`); otherwise `None` and the raw id is used (still valid
        during the `ALLOW_RAW_CHATID` rollout window).

        Raises:
            UnknownChatTarget: `chat_id` isn't a received chat, or isn't a
                parseable integer.
        """
        chat_key = self._int_or_none(chat_id)
        if chat_key is None:
            raise UnknownChatTarget(
                f"refusing outbound to non-integer chat_id={chat_id!r}"
            )
        if not self._state.has_received_chat(chat_key):
            raise UnknownChatTarget(
                f"refusing outbound to chat_id={chat_key} — the adapter never "
                "received an inbound event for this chat. Agents must reply to "
                "a chat they were addressed in (server-authoritative chat "
                "addressing); inventing or incrementing a chat id is rejected."
            )
        return chat_key, self._state.session_handle_for_chat(chat_key)

    async def send(
        self,
        chat_id: int | str,
        content: str,
        reply_to: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Post an assistant message to BGOS.

        If the agent embeds a `[[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]` block
        in its reply, the adapter extracts it into a BGOS options payload so
        the user sees inline chips instead of raw marker text. Block syntax
        lives in PLATFORM_HINTS["bgos"] (fork patch `agent/prompt_builder.py`).

        Splits messages longer than `_max_message_length` into multiple
        chunks with `(i/N)` continuation suffixes (mirrors Telegram parity
        at gateway/platforms/telegram.py:1457). The first chunk carries
        inline buttons (extracted from `[[BGOS_BUTTONS]]` block) and
        reply_to; continuation chunks are plain text. Return value's
        message_id targets the LAST chunk so streaming edits land there.

        Media variants (`send_image`, `send_voice`, …) are optional
        class-level overrides the gateway duck-types. `metadata` is accepted
        for Hermes interface compatibility but not currently plumbed through.
        `reply_to` IS plumbed: it maps to backend `replyToId`, which is what
        agent-to-agent (a2a) side-thread chats use to correlate the target
        assistant's reply with the inbound peer message — without it, the
        originator's pollForReply() falls back to positional matching, which
        works for 1:1 side threads but not for any future fan-in patterns.

        Reply-quote: agents can also embed
        `[[BGOS_REPLY_TO]]<message_id>[[/BGOS_REPLY_TO]]` in the reply to
        anchor it to a specific earlier message — the adapter extracts the
        id and forwards it as `replyToId`. The explicit `reply_to` kwarg
        from Hermes (a2a side-thread) wins if both are set. See spec
        BGOS/docs/superpowers/specs/2026-05-19-reply-quote-design.md.
        """
        # Server-authoritative chat addressing: reject up front if the agent
        # is targeting a chat the adapter never received inbound, and resolve
        # the opaque sessionHandle to round-trip (see _resolve_outbound_target).
        # Done before any marker parsing / tool_progress intercept so an
        # invented chat id can't slip through a side path.
        try:
            chat_key, session_handle = self._resolve_outbound_target(chat_id)
        except UnknownChatTarget as exc:
            log.warning("send rejected: %s", exc)
            return SendResult(  # type: ignore[call-arg]
                success=False, error="unknown_chat_target",
            )

        formatted = self.format_message(content)
        formatted, marker_reply_to = _parse_reply_to_block(formatted)
        cleaned_text, options, render_mode = _parse_buttons_block(formatted)
        # Agent-emitted `MEDIA:/abs/path` lines → real BGOS attachments. The
        # markers are stripped from the visible text here; the files are built
        # (read + uploaded) just before the post loop so a tool_progress
        # short-circuit doesn't pay for disk/network it won't use.
        cleaned_text, media_paths = _parse_media_markers(cleaned_text)
        effective_reply_to: int | None = (
            int(reply_to) if reply_to is not None else marker_reply_to
        )

        # tool_progress intercept on the SEND path too — the gateway's
        # progress loop calls `adapter.send()` for the FIRST tool of a
        # turn (no prior progress_msg_id), then `edit_message` for
        # subsequent tools (upstream gateway/run.py:14483-14488). Without
        # this hook we'd POST the first tool as a standard chat bubble
        # and ONLY route the later ones into a card, which would look
        # broken.
        # Skip when buttons are present — those would be meaningless on
        # a tool_progress card. Spec:
        #   docs/superpowers/specs/2026-05-15-tool-progress-message-type-design.md
        # Also skip when the reply carries MEDIA: markers: the residual text
        # of a media reply can coincidentally parse as tool_progress (emoji +
        # CamelCase), and short-circuiting here would silently drop the
        # attachment AND the already-stripped marker. A media reply is never a
        # tool_progress card.
        parsed_tools = (
            _parse_tool_progress_text(cleaned_text)
            if not options and not media_paths
            else None
        )
        if parsed_tools is not None:
            return await self._handle_tool_progress_edit(
                chat_key, 0, parsed_tools, None,
            )

        # End-of-turn signal: this send() is delivering a plain agent reply
        # (no tool markers). If we still have an open tool_progress card for
        # this chat, the gateway never called delete_message on it (some
        # gateway flows skip the preview-cleanup hook entirely — observed
        # live in chat 830 where every batch in a turn re-PATCHed msg 7462
        # because the cache never cleared). Finalize the card now so the
        # next batch in the next turn starts a fresh one. No-op when there
        # is no active card.
        try:
            await self._finalize_tool_progress_card(chat_key)
        except Exception:
            # Finalization is best-effort — a failure here must not block
            # the actual agent reply. The cache entry is popped before the
            # PATCH attempt inside the finalizer, so even on error the
            # NEXT turn starts cleanly.
            log.warning(
                "tool_progress pre-send finalize failed chat=%s",
                chat_key, exc_info=True,
            )

        # Build outbound media (read + inline/S3) now that we know we're
        # delivering a real reply. Attached to the FIRST chunk only so a
        # multi-chunk caption doesn't duplicate the files.
        media_files = (
            await self._build_media_attachments(media_paths)
            if media_paths else []
        )
        # A media-only reply whose files all failed to attach has nothing to
        # deliver — don't post an empty bubble.
        if media_paths and not media_files and not cleaned_text:
            log.warning(
                "send: media-only reply but no attachments survived; "
                "nothing to post (chat=%s)", chat_key,
            )
            return _send_result(message_id=None)

        chunks = self._chunk_text(cleaned_text)
        last_result: SendResult | None = None
        last_message_id: int | None = None
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            suffix = f"\n({i}/{total})" if total > 1 else ""
            is_first = (i == 1)
            files_for_chunk = (media_files or None) if is_first else None
            assistant_id = await self._assistant_id_for_chat(chat_key)
            if assistant_id is not None:
                resp = await self._api.post_send_message(
                    chat_id=chat_key,
                    assistant_id=assistant_id,
                    text=chunk + suffix,
                    sender="assistant",
                    message_type="standard",
                    options=(options or None) if is_first else None,
                    render_mode=render_mode if is_first else None,
                    reply_to_id=(
                        effective_reply_to if effective_reply_to is not None and is_first else None
                    ),
                    session_handle=session_handle,
                    files=files_for_chunk,
                )
                message_obj = resp.get("message") if isinstance(resp, dict) else None
                message_id = (
                    message_obj.get("id") if isinstance(message_obj, dict) else None
                )
            else:
                # Fallback for older/self-hosted BGOS backends that do not expose
                # GET /chats/:id to pairings. This preserves legacy delivery, but
                # a2a replies will not bridge without assistantId.
                resp = await self._api.post_message(
                    chat_id=chat_key,
                    text=chunk + suffix,
                    sender="assistant",
                    message_type="standard",
                    options=(options or None) if is_first else None,
                    render_mode=render_mode if is_first else None,
                    reply_to_id=(
                        effective_reply_to if effective_reply_to is not None and is_first else None
                    ),
                    session_handle=session_handle,
                    files=files_for_chunk,
                )
                message_id = resp.get("id") if isinstance(resp, dict) else None
            if isinstance(message_id, int):
                last_message_id = message_id
            last_result = _send_result(message_id=message_id)
        if last_message_id is not None:
            self._state.last_assistant_message_by_chat[chat_key] = last_message_id
        return last_result or _send_result(message_id=None)

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into ≤_max_message_length chunks.

        Reserves 10 chars per chunk for the "\\n(NNN/NNN)" suffix so the final
        chunk doesn't push back over the cap. Prefers newline-aligned splits
        when one exists near the cap; falls back to a hard cut otherwise.
        """
        if len(text) <= self._max_message_length:
            return [text]
        chunks: list[str] = []
        remaining = text
        # Reserve 10 chars for the "\n(NNN/NNN)" suffix. 8 would be enough
        # for N<100, but 10 covers up to N=999 which is the practical
        # ceiling for any reasonable single send.
        cap = self._max_message_length - 10
        while len(remaining) > cap:
            split_at = remaining.rfind("\n", 0, cap)
            if split_at < cap // 2:
                split_at = cap
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    # -------------------------------------------------------------------------
    # edit_message / delete_message / send_typing (Task 1.3–1.5) —
    # OVERRIDING these unlocks the gateway-driven tool-progress / streaming /
    # typing UX. Hermes's gateway probes
    # `type(adapter).edit_message is BasePlatformAdapter.edit_message` at
    # gateway/run.py:14370 to decide whether to drive the entire feature —
    # if we inherit the base defaults, every animated tool-bubble, every
    # streamed chunk, and every typing indicator silently no-ops.
    # -------------------------------------------------------------------------

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously-sent message via PATCH /api/v1/messages/{id}.

        The gateway's stream consumer and tool-progress loop call this
        repeatedly to animate streaming responses and edit-in-place tool
        bubbles (see upstream gateway/run.py:14370 — having edit_message
        overridden is what UNLOCKS the entire tool-progress UI; the
        gateway short-circuits the whole code path when the adapter
        inherits the base-class default).

        `finalize` is a no-op for BGOS (the backend has no draft/finalize
        state machine — every edit is just an edit). Accepted for
        interface compatibility with Hermes's BasePlatformAdapter.

        Buttons: respects the same [[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]
        marker block as send(). When the block is absent we send options=[]
        so the backend CLEARS any prior keyboard — necessary for the
        typical streaming pattern where the first send carried buttons
        and the streamed update is plain text.

        Throttle: per-chat, _edit_throttle_seconds=1.5 by default
        (mirrors Telegram's _PROGRESS_EDIT_INTERVAL). The first call in
        a window fires immediately; subsequent calls within the window
        stash their content and schedule a single deferred flush — later
        writes supersede earlier ones, so only the latest text lands.
        Without this the BGOS backend (and the user's chat list) drown
        under tool-progress / streaming edits.

        Returns SendResult(success=False) on 4xx (message too old / not
        editable / deleted) so the gateway falls back to a fresh send().
        Within-window calls return a `success=True` placeholder — real
        failures from the deferred flush surface as warning logs only,
        which matches Telegram's parity behavior (the gateway treats
        progress edits as fire-and-forget).
        """
        # Server-authoritative chat addressing: refuse to edit into a chat the
        # adapter never received inbound. PATCH targets a message id, but the
        # chat scope must still be one the agent was legitimately addressed in.
        try:
            chat_key, _ = self._resolve_outbound_target(chat_id)
        except UnknownChatTarget as exc:
            log.warning("edit_message rejected: %s", exc)
            return SendResult(  # type: ignore[call-arg]
                success=False, error="unknown_chat_target",
            )

        cleaned_text, options, render_mode = _parse_buttons_block(
            self.format_message(content)
        )
        mid_int = int(message_id)
        # Backend's UpdateMessageDto requires userId on PATCH (caught live
        # 2026-05-13: missing userId → 400 "userId should not be empty"
        # → adapter falls back to fresh POST → duplicate visible on BGOS).
        # The most-recent inbound from this chat is the preferred value
        # (that user prompted the assistant reply we're editing), but
        # REST-backfill seeded chats may have no recorded user_id — fall
        # back to the pairing owner's user_id, which always owns the
        # chain chat → assistant → user for this pairing.
        user_id = self._patch_user_id_for_chat(chat_key)

        # tool_progress intercept — if the gateway is editing the streaming
        # preview with emoji-prefixed tool text, route to a separate
        # tool_progress card so the visual treatment matches what users
        # expect (tinted card with pulsing-→-hollow dot) instead of looking
        # like a regular agent message bubble. The gateway joins ALL
        # accumulated tool lines with `\n` and re-sends them every edit
        # (upstream gateway/run.py:14454), so we parse the full list and
        # REPLACE the tracked tools each time.
        # Spec: docs/superpowers/specs/2026-05-15-tool-progress-message-type-design.md
        parsed_tools = _parse_tool_progress_text(cleaned_text)
        if parsed_tools is not None:
            return await self._handle_tool_progress_edit(
                chat_key, mid_int, parsed_tools, user_id,
            )
        now = asyncio.get_running_loop().time()
        last = self._last_edit_at.get(chat_key, 0.0)
        elapsed = now - last
        if elapsed >= self._edit_throttle_seconds:
            # Window expired (or first call) — fire immediately, cancel any
            # stale pending flush since we just spoke for this chat.
            pending = self._pending_edits.pop(chat_key, None)
            if pending is not None and not pending.done():
                pending.cancel()
            self._pending_edit_content.pop(chat_key, None)
            result = await self._do_patch(
                mid_int, cleaned_text, options, render_mode, user_id,
            )
            self._last_edit_at[chat_key] = now
            return result

        # Within throttle window — stash latest content (coalesces with
        # any earlier-stashed write for the same chat), schedule deferred
        # flush if one isn't already pending.
        self._pending_edit_content[chat_key] = (
            mid_int, cleaned_text, options, render_mode, user_id,
        )
        existing = self._pending_edits.get(chat_key)
        if existing is None or existing.done():
            wait_for = self._edit_throttle_seconds - elapsed
            self._pending_edits[chat_key] = asyncio.create_task(
                self._deferred_edit_flush(chat_key, wait_for)
            )
        return _send_result(message_id=mid_int)

    async def _do_patch(
        self,
        mid_int: int,
        text: str,
        options: list[dict] | None,
        render_mode: str | None,
        user_id: str | None,
    ) -> SendResult:
        """The actual PATCH call — extracted so the throttle path can share
        it with the deferred flush path. Returns SendResult(success=False,
        error=not_editable_*) on 4xx; re-raises 5xx."""
        try:
            await self._api.patch_message(
                mid_int,
                text=text,
                options=options if options else [],
                render_mode=render_mode,
                user_id=user_id,
            )
        except BgosApiError as exc:
            if 400 <= exc.status < 500:
                return SendResult(  # type: ignore[call-arg]
                    success=False,
                    message_id=str(mid_int),
                    error=f"not_editable_{exc.status}",
                )
            raise
        return _send_result(message_id=mid_int)

    def _patch_user_id_for_chat(self, chat_key: int) -> str | None:
        """Resolve the userId to attach to a PATCH /api/v1/messages/{id}.

        Preference order:
          1. The most-recent inbound user_id for this chat — the user
             who just prompted the assistant reply we're editing.
          2. The pairing owner's user_id — always valid for any chat
             in this pairing (all assistants under a pairing belong to
             pairing.userId, so the backend's ownership check
             `WHERE m.id = $1 AND a.user_id = $2` always passes).

        Returns None only when neither is set, which means we never
        successfully called connect() — in that case PATCH will fail
        with the same 400 it would have failed with before this fix.
        """
        return (
            self._state.last_user_id_by_chat.get(chat_key)
            or self.pairing_user_id
        )

    async def _handle_tool_progress_edit(
        self,
        chat_key: int,
        preview_mid: int,
        parsed_tools: list[dict],
        user_id: str | None,
    ) -> SendResult:
        """The gateway is editing the streaming-preview message with
        tool-progress content (one or more emoji-prefixed lines). Absorb
        the edit and route it to a dedicated tool_progress card instead.

        The gateway accumulates ALL tool lines and re-sends the full
        list every edit (upstream gateway/run.py:14454,
        `full_text = "\\n".join(progress_lines)`), so the parsed list IS
        the new authoritative state — we REPLACE the tracked tools each
        call rather than appending.

        First call per agent turn: POST a new tool_progress message.
        Subsequent calls: PATCH the same card with the refreshed list.

        Each preview_mid is recorded against this chat so delete_message
        can finalize the card to state="done" when the gateway cleans up
        the preview at end of turn.

        Returns a placeholder SendResult so the gateway treats the edit as
        successful. On HTTP failure we log and return success anyway —
        tool_progress is opportunistic UI polish; we never want to break
        the agent's reply path over it.
        """
        # Serialize all card mutations for one chat — the gateway can
        # call into the adapter concurrently (the send() path for the
        # first tool, then edit_message for subsequent tools, sometimes
        # racing within the same 1.5s throttle window).
        # The send() path passes user_id=None (no inbound context for the
        # first-tool case where it's invoked). Fall back to the pairing
        # owner so any subsequent PATCH on this card has a valid userId.
        if user_id is None:
            user_id = self.pairing_user_id
        lock = self._tool_progress_lock_by_chat.setdefault(
            chat_key, asyncio.Lock(),
        )
        async with lock:
            # Replace, don't accumulate — the gateway's content is the
            # full accumulated list.
            self._tool_progress_tools_by_chat[chat_key] = list(parsed_tools)
            # Map the streaming-preview message_id → chat. delete_message
            # uses this to know "the agent's turn is wrapping up; flip my
            # card to done". preview_mid=0 is used by send()-path callers
            # (no preview exists yet) and we skip the mapping for that.
            if preview_mid > 0:
                self._tool_progress_preview_to_chat[preview_mid] = chat_key

            summary = _build_tool_progress_summary(parsed_tools, done=False)
            card_payload = {"state": "running", "tools": parsed_tools}
            existing_card_id = self._tool_progress_card_id_by_chat.get(chat_key)

            try:
                if existing_card_id is None:
                    resp = await self._api.post_message(
                        chat_id=chat_key,
                        text=summary,
                        message_type="tool_progress",
                        tool_progress=card_payload,
                    )
                    new_id_val = resp.get("id") if isinstance(resp, dict) else None
                    if isinstance(new_id_val, int):
                        self._tool_progress_card_id_by_chat[chat_key] = new_id_val
                else:
                    await self._api.patch_message(
                        existing_card_id,
                        text=summary,
                        user_id=user_id,
                        tool_progress=card_payload,
                    )
            except BgosApiError as exc:
                log.warning(
                    "tool_progress card emit failed chat=%d status=%s body=%s; "
                    "falling back to plain streaming edits",
                    chat_key, exc.status, getattr(exc, "body", None),
                )
                self._tool_progress_card_id_by_chat.pop(chat_key, None)
                self._tool_progress_tools_by_chat.pop(chat_key, None)
                if preview_mid > 0:
                    self._tool_progress_preview_to_chat.pop(preview_mid, None)
            except Exception:
                log.warning(
                    "tool_progress card emit failed chat=%d (unexpected); "
                    "falling back to plain streaming edits",
                    chat_key, exc_info=True,
                )
                self._tool_progress_card_id_by_chat.pop(chat_key, None)
                self._tool_progress_tools_by_chat.pop(chat_key, None)
                if preview_mid > 0:
                    self._tool_progress_preview_to_chat.pop(preview_mid, None)

        # Return the right message id to the gateway so its
        # `progress_msg_id` tracking aligns with what we did:
        #   - edit_message path (preview_mid > 0): echo the preview id;
        #     the gateway already knows it.
        #   - send() path (preview_mid == 0): return the CARD id we
        #     just created/PATCHed, so the gateway treats it as the
        #     progress bubble and routes subsequent edits/deletes to it.
        if preview_mid > 0:
            return _send_result(message_id=preview_mid)
        card_id = self._tool_progress_card_id_by_chat.get(chat_key)
        return _send_result(message_id=card_id)

    async def _finalize_tool_progress_card(self, chat_key: int) -> None:
        """Transition the active tool_progress card for this chat from
        state='running' to state='done' so the frontend auto-collapses it.
        Called by delete_message when the gateway cleans up the streaming
        preview at end of turn.

        No-op when there's no active card for this chat (the turn didn't
        use any tools, or the previous emit failed).
        """
        card_id = self._tool_progress_card_id_by_chat.pop(chat_key, None)
        tools_list = self._tool_progress_tools_by_chat.pop(chat_key, [])
        if card_id is None:
            return
        # Mark every tool entry as done — the gateway emits one line per
        # completed tool call, so by end-of-turn they're all complete.
        for entry in tools_list:
            if entry.get("status") == "running":
                entry["status"] = "done"
        summary = _build_tool_progress_summary(tools_list, done=True)
        user_id = self._patch_user_id_for_chat(chat_key)
        try:
            await self._api.patch_message(
                card_id,
                text=summary,
                user_id=user_id,
                tool_progress={"state": "done", "tools": tools_list},
            )
        except Exception:
            log.warning(
                "tool_progress finalize failed chat=%d card=%d",
                chat_key, card_id, exc_info=True,
            )

    async def _deferred_edit_flush(self, chat_key: int, wait_for: float) -> None:
        """Sleep until the throttle window expires, then fire whatever
        content is currently stashed for this chat. Cancellation is
        expected on disconnect — return cleanly without flushing."""
        try:
            await asyncio.sleep(wait_for)
        except asyncio.CancelledError:
            return
        pending = self._pending_edit_content.pop(chat_key, None)
        if pending is None:
            return
        mid_int, text, options, render_mode, user_id = pending
        try:
            await self._do_patch(mid_int, text, options, render_mode, user_id)
        except Exception:
            log.warning(
                "deferred edit flush failed chat=%d msg=%d",
                chat_key, mid_int, exc_info=True,
            )
        self._last_edit_at[chat_key] = asyncio.get_running_loop().time()
        # Drop the now-completed task reference so _pending_edits doesn't
        # accumulate one entry per chat for the lifetime of the adapter.
        # disconnect()'s snapshot-then-cancel pattern handles in-flight
        # tasks; this clears done ones so we don't leak memory in steady
        # state when chats keep flowing.
        self._pending_edits.pop(chat_key, None)

    async def delete_message(
        self,
        chat_id: int | str,
        message_id: int | str,
    ) -> bool:
        """Delete a previously-sent message via DELETE /api/v1/messages/{id}.

        Used by the gateway's stream consumer to clean up intermediate
        streaming-preview messages once the final answer is delivered as
        a fresh message (so the visible timestamp reflects completion
        time rather than start-of-stream).

        Before the DELETE, if this message_id was the streaming preview
        for an active tool_progress card (tracked by
        _tool_progress_preview_to_chat), finalize the card by PATCHing
        its state from 'running' to 'done' — that's how the card learns
        the agent's turn is wrapping up and auto-collapses on the
        frontend.

        Returns False on any HTTP error (404 = already deleted; 501 =
        backend doesn't implement DELETE yet) so the caller falls back to
        leaving the message visible. Re-raises 5xx other than 501 so real
        backend incidents surface.
        """
        mid_int = int(message_id)
        # End-of-turn signal for the tool_progress card. Two cases:
        #
        # Case A — `mid_int` is the gateway's streaming preview message,
        # which we recorded against the active card when intercepting an
        # edit_message. Finalize the card to state="done" and continue
        # with the actual DELETE (the preview is real and the gateway
        # owns it).
        #
        # Case B — `mid_int` IS the card itself (which happens when the
        # adapter's send() intercept absorbed the very first tool: the
        # card_id we returned to the gateway is what it now wants to
        # delete). Finalize the card to state="done" and DO NOT delete —
        # the card is the historical record we want to keep.
        finalize_chat = self._tool_progress_preview_to_chat.pop(mid_int, None)
        card_chat = None
        for chat_id_iter, card_id in list(self._tool_progress_card_id_by_chat.items()):
            if card_id == mid_int:
                card_chat = chat_id_iter
                break
        if card_chat is not None:
            await self._finalize_tool_progress_card(card_chat)
            return True
        if finalize_chat is not None:
            await self._finalize_tool_progress_card(finalize_chat)
        try:
            await self._api.delete_message(mid_int)
            return True
        except BgosApiError as exc:
            if 400 <= exc.status < 500 or exc.status == 501:
                log.debug("delete_message message_id=%s failed: %s", message_id, exc)
                return False
            raise

    async def send_typing(
        self,
        chat_id: int | str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Emit a typing indicator over WS for this chat.

        The gateway calls this between tool-progress edits and during
        long-running tool calls so the user sees the bot is still alive.
        BGOS is currently DM-only — one assistant per pairing — so we
        pick the only assistant in the route map. If multi-assistant
        pairings are added later, `metadata` could carry an explicit
        assistant_id.

        Cosmetic — never raises.
        """
        if self._ws is None:
            return
        assistant_id: int | None = None
        if metadata and isinstance(metadata, dict):
            candidate = metadata.get("assistant_id")
            if isinstance(candidate, int):
                assistant_id = candidate
        if assistant_id is None:
            for aid in self._state.assistant_route:
                assistant_id = aid
                break
        if assistant_id is None:
            return
        try:
            await self._ws.emit_typing(
                chat_id=int(chat_id), assistant_id=assistant_id,
            )
        except Exception:
            log.debug("send_typing failed (non-fatal)", exc_info=True)

    # -------------------------------------------------------------------------
    # Optional media overrides (Task 6) — Hermes duck-types these at send time.
    # The gateway's fallback is to call `send()` with a caption, so NOT
    # defining these is "fine" — it just degrades media to a text notice.
    # -------------------------------------------------------------------------

    async def _upload_and_attach(
        self, *, file_bytes: bytes, filename: str, mime: str,
    ) -> dict:
        """Return a files[] entry ready for POST /messages or /send-message.

        Policy: inline below `S3_THRESHOLD`, presigned S3 PUT above. Keys are
        camelCase on the wire to match OpenClaw's shape
        (openclaw-channel-bgos/src/types.ts OutboundMessagePayload.files):
        `fileName`, `fileMimeType`, `fileData` (inline), `s3Key` (presigned).

        Two corrections shipped 2026-05-31 (the "agent images don't render"
        bug):

        1. **Classification flags.** We now stamp `isImage`/`isVideo`/
           `isAudio`/`isDocument` (+ image `width`/`height`). The backend
           persists these verbatim and the frontend renders a file as an
           image ONLY when `isImage` is true — without them every image
           arrived as a non-rendering document card.
        2. **Inline as a data URI.** Inline bytes are sent as
           `data:<mime>;base64,...` rather than bare base64. The `/messages`
           path stores `fileData` verbatim and the client feeds it straight
           into `<Image source={{uri}}>`, which needs a real URI — bare
           base64 won't load. (The `/send-message` path additionally coerces
           the data URI to S3; both forms render.)
        """
        size = len(file_bytes)
        flags = _classify_media(mime)
        entry: dict[str, Any] = {
            "fileName": filename,
            "fileMimeType": mime,
            "size": size,
            **flags,
        }
        if flags["isImage"]:
            width, height = _sniff_image_dimensions(file_bytes)
            if width and height:
                entry["width"] = width
                entry["height"] = height

        if size < S3_THRESHOLD:
            b64 = base64.b64encode(file_bytes).decode("ascii")
            entry["fileData"] = f"data:{mime};base64,{b64}"
            return entry

        presigned = await self._api.create_upload_url(
            filename=filename, mime=mime, size=size,
        )
        # Direct S3 PUT — bypasses the BGOS backend entirely.
        async with httpx.AsyncClient(
            timeout=self._config.request_timeout_seconds,
        ) as put_client:
            resp = await put_client.put(
                presigned["upload_url"],
                content=file_bytes,
                headers={"Content-Type": mime},
            )
            resp.raise_for_status()
        entry["s3Key"] = presigned["s3_key"]
        return entry

    async def _build_media_attachments(self, paths: list[str]) -> list[dict]:
        """Turn agent-emitted `MEDIA:` paths into `files[]` entries.

        Reads each local file (the agent wrote it to the Hermes host's disk),
        guesses its MIME from the extension, and runs it through
        `_upload_and_attach` (inline data URI <500 KB, presigned S3 above).
        Bad paths (missing, not a file, too large, unreadable, or whose
        upload fails) are skipped with a warning rather than aborting the
        whole reply — partial delivery beats a dropped message.
        """
        attachments: list[dict] = []
        for raw_path in paths:
            try:
                path = Path(raw_path).expanduser()
                if not path.is_file():
                    log.warning("MEDIA: not a file, skipping: %r", raw_path)
                    continue
                file_size = path.stat().st_size
                if file_size <= 0:
                    log.warning("MEDIA: empty file, skipping: %r", raw_path)
                    continue
                if file_size > _MEDIA_MAX_BYTES:
                    log.warning(
                        "MEDIA: file %r is %d bytes (> %d cap) — skipping",
                        raw_path, file_size, _MEDIA_MAX_BYTES,
                    )
                    continue
                # Read off the event loop — a multi-MB read shouldn't stall
                # streaming edits / typing indicators for other chats.
                file_bytes = await asyncio.to_thread(path.read_bytes)
                mime = _guess_media_mime(path.name)
                attachments.append(
                    await self._upload_and_attach(
                        file_bytes=file_bytes, filename=path.name, mime=mime,
                    )
                )
            except Exception:
                log.warning(
                    "MEDIA: failed to attach %r — skipping", raw_path,
                    exc_info=True,
                )
        return attachments

    async def _send_media(
        self, *, chat_id: int | str, file_bytes: bytes, filename: str,
        mime: str, caption: str | None, reply_to: int | None = None,
    ) -> SendResult:
        attach = await self._upload_and_attach(
            file_bytes=file_bytes, filename=filename, mime=mime,
        )
        chat_key = int(chat_id)
        session_handle: str | None = None
        try:
            chat_key, session_handle = self._resolve_outbound_target(chat_id)
        except UnknownChatTarget:
            # Legacy direct-media helpers were historically allowed to post to
            # numeric chat ids without prior inbound state. Keep that fallback
            # for direct callers/tests, while normal gateway delivery has the
            # state and uses /send-message below.
            chat_key = int(chat_id)
        assistant_id = await self._assistant_id_for_chat(chat_key)
        if assistant_id is not None:
            resp = await self._api.post_send_message(
                chat_id=chat_key,
                assistant_id=assistant_id,
                text=caption or "",
                sender="assistant",
                message_type="standard",
                has_attachment=True,
                files=[attach],
                reply_to_id=int(reply_to) if reply_to is not None else None,
                session_handle=session_handle,
            )
            message_obj = resp.get("message") if isinstance(resp, dict) else None
            message_id = (
                message_obj.get("id") if isinstance(message_obj, dict) else resp.get("id") if isinstance(resp, dict) else None
            )
        else:
            resp = await self._api.post_message(
                chat_id=chat_key,
                text=caption or "",
                sender="assistant",
                message_type="standard",
                files=[attach],
                reply_to_id=int(reply_to) if reply_to is not None else None,
            )
            message_id = resp.get("id") if isinstance(resp, dict) else None
        if isinstance(message_id, str):
            try:
                message_id = int(message_id)
            except ValueError:
                message_id = None
        if isinstance(message_id, int):
            self._state.last_assistant_message_by_chat[chat_key] = message_id
        return _send_result(message_id=message_id)

    async def _fetch_media_source(
        self, src: str, filename: str | None = None, mime: str | None = None,
    ) -> tuple[bytes | None, str, str]:
        """Resolve a media source — an http(s) URL OR a local/file:// path — to
        (bytes, filename, mime).

        Hermes's gateway hands `send_image`/`send_animation`/`send_image_file` a
        URL (markdown image links the agent emitted) or a local path. For http
        URLs we download + re-upload to BGOS so the asset is owned and
        persistent (source URLs — fal.media, replicate.delivery — expire and
        would 404 once the user scrolls back). Local/`file://` paths are read
        off the event loop. Returns `(None, …)` on any failure (dead link, bad
        path, empty, or over `_MEDIA_MAX_BYTES`) so delivery degrades gracefully
        instead of raising.
        """
        name = filename or ""
        resolved_mime = mime or ""
        try:
            if re.match(r"^https?://", src, re.IGNORECASE):
                async with httpx.AsyncClient(
                    timeout=self._config.request_timeout_seconds,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(src)
                    resp.raise_for_status()
                    data = resp.content
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
                name = name or Path(unquote(urlparse(src).path)).name
                resolved_mime = resolved_mime or ctype
            else:
                parsed = urlparse(src)
                path = Path(
                    unquote(parsed.path if parsed.scheme == "file" else src)
                ).expanduser()
                data = await asyncio.to_thread(path.read_bytes)
                name = name or path.name
        except Exception:
            log.warning("media source fetch failed: %r", src, exc_info=True)
            return None, name or "download", resolved_mime or "application/octet-stream"
        if not data:
            return None, name or "download", resolved_mime or "application/octet-stream"
        if len(data) > _MEDIA_MAX_BYTES:
            log.warning(
                "media source %r is %d bytes (> %d cap) — skipping",
                src, len(data), _MEDIA_MAX_BYTES,
            )
            return None, name or "download", resolved_mime or "application/octet-stream"
        name = name or "download"
        resolved_mime = resolved_mime or _guess_media_mime(name)
        if "." not in name:
            name += mimetypes.guess_extension(resolved_mime) or ""
        return data, name, resolved_mime

    async def send_image(
        self, chat_id: int | str, image_url: str | None = None,
        caption: str | None = None, reply_to: int | str | None = None,
        metadata: dict[str, Any] | None = None, *,
        file_bytes: bytes | None = None, filename: str | None = None,
        mime: str | None = None, **kwargs: Any,
    ) -> SendResult:
        """Send an image. Hermes's gateway calls
        ``send_image(chat_id, image_url, …)`` where `image_url` is an http(s)
        URL (markdown image links the agent emitted) — we download + re-upload
        to BGOS. The byte form (`file_bytes=`, internal callers + tests) and a
        local/file:// path are also accepted. `**kwargs` absorbs any future
        gateway kwarg so this can't regress on an unexpected argument.
        """
        if file_bytes is None and image_url:
            file_bytes, filename, mime = await self._fetch_media_source(
                image_url, filename, mime,
            )
        if not file_bytes:
            log.warning(
                "send_image: no image bytes for chat=%s (src=%r)", chat_id, image_url,
            )
            return _send_result(message_id=None)
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename or "image.png", mime=mime or "image/png",
            caption=caption, reply_to=(int(reply_to) if reply_to is not None else None),
        )

    async def send_image_file(
        self, chat_id: int | str, image_path: str | None = None,
        caption: str | None = None, reply_to: int | str | None = None,
        metadata: dict[str, Any] | None = None, *,
        file_bytes: bytes | None = None, filename: str | None = None,
        mime: str | None = None, **kwargs: Any,
    ) -> SendResult:
        """Send a LOCAL image file. Matches the gateway's
        ``send_image_file(chat_id, image_path, …)`` hook. Same fetch+send
        pipeline as send_image (the source resolver handles plain paths too).
        """
        if file_bytes is None and image_path:
            file_bytes, filename, mime = await self._fetch_media_source(
                image_path, filename, mime,
            )
        if not file_bytes:
            log.warning(
                "send_image_file: no image bytes for chat=%s (path=%r)",
                chat_id, image_path,
            )
            return _send_result(message_id=None)
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename or "image.png", mime=mime or "image/png",
            caption=caption, reply_to=(int(reply_to) if reply_to is not None else None),
        )

    async def send_voice(
        self,
        chat_id: int | str,
        audio_path: str | None = None,
        caption: str | None = None,
        reply_to: int | str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        file_bytes: bytes | None = None,
        filename: str | None = None,
        mime: str | None = None,
        audio_duration: float | int | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send audio as a BGOS voice bubble, not an attachment card.

        Hermes's gateway calls media hooks with the BasePlatformAdapter
        signature (`audio_path`, `reply_to`, `metadata`). Older unit tests and
        direct plugin callers used the pre-Hermes BGOS signature
        (`file_bytes`, `filename`, `mime`). Accept both, then post the
        message-level audio fields that BGOS's `VoiceMessagePlayer` reads.
        """
        if audio_path is not None:
            path = Path(audio_path).expanduser()
            file_bytes = await asyncio.to_thread(path.read_bytes)
            filename = filename or path.name
            mime = mime or _guess_media_mime(path.name)
        if not file_bytes:
            log.warning("send_voice: no audio bytes supplied for chat=%s", chat_id)
            return _send_result(message_id=None)
        filename = filename or "voice-message.mp3"
        mime = mime or _guess_media_mime(filename)
        if not mime.startswith("audio/"):
            log.warning(
                "send_voice: refusing non-audio MIME %r for %s", mime, filename,
            )
            return await self._send_media(
                chat_id=chat_id, file_bytes=file_bytes, filename=filename,
                mime=mime, caption=caption, reply_to=(int(reply_to) if reply_to is not None else None),
            )

        try:
            chat_key, session_handle = self._resolve_outbound_target(chat_id)
        except UnknownChatTarget as exc:
            log.warning("send_voice rejected: %s", exc)
            return SendResult(success=False, error="unknown_chat_target")  # type: ignore[call-arg]

        assistant_id = await self._assistant_id_for_chat(chat_key)
        if assistant_id is None:
            log.warning(
                "send_voice: could not resolve assistant_id for chat=%s; "
                "falling back to audio attachment card",
                chat_key,
            )
            return await self._send_media(
                chat_id=chat_key, file_bytes=file_bytes, filename=filename,
                mime=mime, caption=caption,
                reply_to=(int(reply_to) if reply_to is not None else None),
            )

        resp = await self._api.post_send_message(
            chat_id=chat_key,
            assistant_id=assistant_id,
            text=caption or "",
            sender="assistant",
            message_type="standard",
            has_attachment=True,
            is_audio_message=True,
            audio_data=base64.b64encode(file_bytes).decode("ascii"),
            audio_file_name=filename,
            audio_mime_type=mime,
            audio_duration=audio_duration,
            reply_to_id=int(reply_to) if reply_to is not None else None,
            session_handle=session_handle,
            files=[],
        )
        message_obj = resp.get("message") if isinstance(resp, dict) else None
        message_id = (
            message_obj.get("id") if isinstance(message_obj, dict) else resp.get("id") if isinstance(resp, dict) else None
        )
        if isinstance(message_id, str):
            try:
                message_id = int(message_id)
            except ValueError:
                message_id = None
        if isinstance(message_id, int):
            self._state.last_assistant_message_by_chat[chat_key] = message_id
        return _send_result(message_id=message_id)

    async def send_video(
        self,
        chat_id: int | str,
        video_path: str | None = None,
        caption: str | None = None,
        reply_to: int | str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        file_bytes: bytes | None = None,
        filename: str | None = None,
        mime: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send video attachments from either Hermes file-path or raw bytes."""
        if video_path is not None:
            path = Path(video_path).expanduser()
            file_bytes = await asyncio.to_thread(path.read_bytes)
            filename = filename or path.name
            mime = mime or _guess_media_mime(path.name)
        if not file_bytes:
            log.warning("send_video: no video bytes supplied for chat=%s", chat_id)
            return _send_result(message_id=None)
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename or "video.mp4", mime=mime or _guess_media_mime(filename or "video.mp4"),
            caption=caption, reply_to=(int(reply_to) if reply_to is not None else None),
        )

    async def send_document(
        self,
        chat_id: int | str,
        file_path: str | None = None,
        caption: str | None = None,
        reply_to: int | str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        file_bytes: bytes | None = None,
        filename: str | None = None,
        mime: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send document/download-card attachments.

        Hermes core extracts ``MEDIA:/abs/path`` from final replies, strips the
        marker from the text, then calls platform adapters as
        ``send_document(chat_id=..., file_path=..., metadata=...)``. The BGOS
        adapter's older raw-bytes-only signature raised ``TypeError`` on that
        call, so the already-stripped marker produced a text-only message and
        the attachment was silently omitted. Accept both calling conventions.
        """
        if file_path is not None:
            path = Path(file_path).expanduser()
            file_bytes = await asyncio.to_thread(path.read_bytes)
            filename = filename or path.name
            mime = mime or _guess_media_mime(path.name)
        if not file_bytes:
            log.warning("send_document: no file bytes supplied for chat=%s", chat_id)
            return _send_result(message_id=None)
        filename = filename or "attachment.bin"
        mime = mime or _guess_media_mime(filename)
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename, mime=mime, caption=caption,
            reply_to=(int(reply_to) if reply_to is not None else None),
        )

    async def send_animation(
        self,
        chat_id: int | str,
        animation_path: str | None = None,
        caption: str | None = None,
        reply_to: int | str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        animation_url: str | None = None,
        file_bytes: bytes | None = None,
        filename: str | None = None,
        mime: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send GIF/animation from a URL, a file-path, or raw bytes.

        Hermes's base contract is ``send_animation(chat_id, animation_url, …)``
        — but the 2nd positional may be either an http(s) URL or a local path,
        so we route it through `_fetch_media_source` (which detects and handles
        both). The `animation_url` kwarg is accepted as an explicit alias.
        """
        src = animation_path or animation_url
        if file_bytes is None and src:
            file_bytes, filename, mime = await self._fetch_media_source(
                src, filename, mime,
            )
        if not file_bytes:
            log.warning("send_animation: no animation bytes supplied for chat=%s", chat_id)
            return _send_result(message_id=None)
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename or "animation.gif", mime=mime or _guess_media_mime(filename or "animation.gif"),
            caption=caption, reply_to=(int(reply_to) if reply_to is not None else None),
        )

    async def send_multiple_images(
        self,
        chat_id: int | str,
        images: list[tuple[bytes, str, str]],
        *,
        caption: str | None = None,
        reply_to: int | None = None,
        metadata: dict[str, Any] | None = None,
        human_delay: float | int | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send up to 10 images as a single carousel-rendered message.

        `images` is a list of `(file_bytes, filename, mime)` tuples. Backend
        renders 2+ images as a carousel; 1 image renders normally. Caps at
        10 to match Telegram's sendMediaGroup limit — extras silently dropped
        with a log warning so a runaway agent doesn't flood backends.

        Each entry goes through the same _upload_and_attach pipeline as
        send_image: inline base64 below S3_THRESHOLD, presigned S3 PUT above.
        """
        if not images:
            return _send_result(message_id=None)
        if len(images) > 10:
            log.warning(
                "send_multiple_images received %d images; sending first 10",
                len(images),
            )
            images = images[:10]
        attachments: list[dict] = []
        for item in images:
            # Internal tests/direct callers pass (bytes, filename, mime). Hermes
            # core passes ("file:///abs/path", caption) after extracting image
            # MEDIA markers. Support both so images do not vanish after the core
            # strips the marker from the visible response text.
            if len(item) >= 3 and isinstance(item[0], (bytes, bytearray)):
                blob = bytes(item[0])
                filename = str(item[1])
                mime = str(item[2])
            else:
                raw_url = str(item[0])
                from urllib.parse import unquote, urlparse

                parsed = urlparse(raw_url)
                path = Path(unquote(parsed.path if parsed.scheme == "file" else raw_url)).expanduser()
                blob = await asyncio.to_thread(path.read_bytes)
                filename = path.name
                mime = _guess_media_mime(filename)
            attachments.append(
                await self._upload_and_attach(
                    file_bytes=blob, filename=filename, mime=mime,
                )
            )
        resp = await self._api.post_message(
            chat_id=int(chat_id),
            text=caption or "",
            sender="assistant",
            message_type="standard",
            files=attachments,
            reply_to_id=int(reply_to) if reply_to is not None else None,
        )
        message_id = resp.get("id") if isinstance(resp, dict) else None
        if isinstance(chat_id, int) and isinstance(message_id, int):
            self._state.last_assistant_message_by_chat[chat_id] = message_id
        return _send_result(message_id=message_id)

    # -------------------------------------------------------------------------
    # send_exec_approval (Task 7) — optional duck-typed hook. Gateway calls
    # us with a 15s hard deadline when the agent requests a dangerous-command
    # approval. We render BGOS's approval_request bubble with four Telegram-
    # parity buttons (Allow once / for session / always / Deny) and stash
    # approval_id → session_key for Task 8's callback router.
    # -------------------------------------------------------------------------

    async def send_exec_approval(
        self,
        chat_id: int | str,
        command: str,
        session_key: str,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        approval_id = next(self._approval_id_counter)

        # Option shape matches backend CreateMessageOptionDto (camelCase
        # `text` + `callbackData`) + OpenClaw's extended shape (`style`).
        # `style` and `row_index` are dropped by the backend's whitelist
        # today (Phase F schema extension will add them); we still send
        # them for forward-compat.
        options = [
            {"text": "Allow once",         "callbackData": f"ea:once:{approval_id}",
             "style": "success", "row_index": 0},
            {"text": "Allow for session",  "callbackData": f"ea:session:{approval_id}",
             "style": "success", "row_index": 0},
            {"text": "Always allow",       "callbackData": f"ea:always:{approval_id}",
             "style": "default", "row_index": 1},
            {"text": "Deny",               "callbackData": f"ea:deny:{approval_id}",
             "style": "danger",  "row_index": 1},
        ]
        # Resolve the agent route for this approval (best-effort): prefer an
        # explicit assistant_id in the gateway-provided metadata, else fall
        # back to the single bound assistant. Mirrors the resolution used by
        # `send_typing`.
        assistant_id: int | None = None
        if metadata and isinstance(metadata, dict):
            cand = metadata.get("assistant_id")
            if isinstance(cand, int):
                assistant_id = cand
        if assistant_id is None:
            assistant_id = next(iter(self._state.assistant_route), None)
        agent_route = (
            self._state.get_route(assistant_id) if assistant_id is not None else None
        ) or "hermes"

        # The backend's ApprovalMetaDto (@ValidateNested) REQUIRES `tool`,
        # `agent_route`, `risk`, and `request_id` (all non-optional). Omitting
        # them 400s the POST — `send_exec_approval` then raises and Hermes'
        # gateway silently falls back to a plain-text approval prompt (no
        # buttons). `request_id` MUST be the string form of `approval_id` so
        # it matches the `ea:<choice>:<approval_id>` callbackData the buttons
        # carry (and the adapter's `_APPROVAL_CALLBACK_RE`). The extra keys
        # below (command/session_key/approval_id/metadata) are retained for
        # local debugging; the backend whitelist drops them today.
        approval_meta = {
            "tool": command,
            "agent_route": agent_route,
            "risk": "high",
            "request_id": str(approval_id),
            "command": command,
            "session_key": session_key,
            "approval_id": approval_id,
            "metadata": metadata or {},
        }

        resp = await self._api.post_message(
            chat_id=int(chat_id),
            text=description,
            sender="assistant",
            message_type="approval_request",
            options=options,
            approval_meta=approval_meta,
        )
        # Stash AFTER the successful POST so a 5xx doesn't leave us with a
        # phantom pending approval the router would later try to resolve.
        self._approval_state[approval_id] = session_key
        message_id = resp.get("id") if isinstance(resp, dict) else None
        return _send_result(message_id=message_id)

    # -------------------------------------------------------------------------
    # send_slash_confirm (Task 2.3) — three-button slash-command confirmation
    # prompt. Mirrors gateway/platforms/telegram.py:2119. Used by Hermes's
    # generic slash-confirm primitive for commands with non-destructive but
    # expensive side effects (current caller: /reload-mcp which invalidates
    # the provider prompt cache).
    # -------------------------------------------------------------------------

    async def send_slash_confirm(
        self,
        chat_id: int | str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Three-option slash-command confirmation prompt (mirrors
        `gateway/platforms/telegram.py:2119`). Used by Hermes's generic
        slash-confirm primitive for commands with non-destructive but
        expensive side effects (current caller: /reload-mcp).

        Buttons: Approve Once / Always Approve / Cancel.
        Callback shape: sc:<choice>:<confirm_id>

        The backend may not recognize messageType=slash_confirm yet — in
        that case it'll likely render as plain text plus the inline option
        chips, which is acceptable graceful degradation.
        """
        options = [
            {"text": "✅ Approve Once",     "callbackData": f"sc:once:{confirm_id}",
             "style": "success", "row_index": 0},
            {"text": "🔒 Always Approve",  "callbackData": f"sc:always:{confirm_id}",
             "style": "success", "row_index": 0},
            {"text": "❌ Cancel",           "callbackData": f"sc:cancel:{confirm_id}",
             "style": "danger",  "row_index": 1},
        ]
        body_text = f"**{title}**\n\n{message}" if title else message
        resp = await self._api.post_message(
            chat_id=int(chat_id),
            text=body_text,
            sender="assistant",
            message_type="slash_confirm",
            options=options,
        )
        self._slash_confirm_state[confirm_id] = session_key
        message_id = resp.get("id") if isinstance(resp, dict) else None
        return _send_result(message_id=message_id)

    # -------------------------------------------------------------------------
    # send_update_prompt (Task 5.1) — yes/no inline confirmation called by
    # Hermes's gateway during stash-restore / config-migration flows. Mirrors
    # gateway/platforms/telegram.py:2006. Callback shape `update_prompt:y` /
    # `update_prompt:n`. Unlike send_exec_approval / send_slash_confirm, we
    # keep no adapter-side resolution state here — Hermes's gateway already
    # handles those callbacks (waits on a future or sends a follow-up).
    # -------------------------------------------------------------------------

    async def send_update_prompt(
        self,
        chat_id: int | str,
        prompt: str,
        default: str = "",
        session_key: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Yes/No inline prompt for Hermes's update flow (stash restore,
        config migration). Mirrors gateway/platforms/telegram.py:2006.

        Hermes's gateway invokes this via duck-typing — `getattr(type(adapter),
        "send_update_prompt", None) is not None` at gateway/run.py:12580 — so
        there is NO `BasePlatformAdapter.send_update_prompt` to override. The
        signature MUST match the gateway's call shape: `chat_id, prompt,
        default, session_key, metadata` (positional/keyword), or the gateway's
        call raises TypeError and silently falls back to plain text.

        Two-button inline UI: Yes / No. Callback shape `update_prompt:y` /
        `update_prompt:n`. The gateway resolves these callbacks itself
        (waits on a future keyed by chat_id / session_key); we don't keep
        adapter-side resolution state.
        """
        text = f"⚕ **Update needs your input:**\n\n{prompt}"
        if default:
            text += f"\n\n_default: {default}_"
        options = [
            {"text": "✓ Yes", "callbackData": "update_prompt:y",
             "style": "success", "row_index": 0},
            {"text": "✗ No",  "callbackData": "update_prompt:n",
             "style": "default", "row_index": 0},
        ]
        resp = await self._api.post_message(
            chat_id=int(chat_id),
            text=text,
            sender="assistant",
            message_type="standard",
            options=options,
        )
        message_id = resp.get("id") if isinstance(resp, dict) else None
        return _send_result(message_id=message_id)

    async def get_chat_info(self, chat_id: int | str) -> dict:
        """BGOS is DM-only — a chat is its own minimal context. If later
        tasks need real chat metadata (title, participants), we extend this
        to call an endpoint on BGOS. For now, return a minimal stub that
        satisfies Hermes's contract.

        Server-authoritative chat addressing: refuse to echo back a chat id
        the adapter never received inbound, so an agent can't probe for or
        invent a chat target through this surface (raises UnknownChatTarget).
        """
        chat_key, _ = self._resolve_outbound_target(chat_id)
        return {"platform": self.platform_name, "chat_id": chat_key}

    # -------------------------------------------------------------------------
    # Introspection (used by tests + later tasks)
    # -------------------------------------------------------------------------

    @property
    def assistant_route_map(self) -> dict[int, str]:
        """A defensive copy so tests (and caller code) can't mutate internals."""
        return dict(self._state.assistant_route)

    # -------------------------------------------------------------------------
    # WS event hooks — placeholders filled in by later tasks
    # -------------------------------------------------------------------------

    async def _handle_inbound(self, data: dict, *, batchable: bool = True) -> None:
        """Translate a WS inbound_message payload into a Hermes MessageEvent
        and hand it to `self.handle_message`. Drops events for unknown
        assistants (e.g. a race where a pairing was just revoked but the WS
        hasn't caught up).

        Accepts both snake_case (REST `/api/v1/integrations/inbound`) and
        camelCase (WS `inbound_message`) key shapes — the BGOS backend
        emits them differently and the adapter has to absorb both. See
        `_normalize_inbound_payload` for the alias table.

        Bridge-local slash commands (`/new`, `/retry`, `/status`) are
        intercepted here and handled adapter-side — they never reach the
        Hermes agent. Everything else flows through `handle_message`.

        `batchable=False` disables the adaptive text-batching path — used by
        backfill replay (history isn't "user typing fast", it's historical)
        and `/retry` replay (already merged into a single canonical text).
        """
        data = _normalize_inbound_payload(data)
        assistant_id = data.get("assistant_id")
        if assistant_id is None:
            log.debug("inbound missing assistant_id: %s", data)
            return
        route = self._state.get_route(assistant_id)
        if route is None:
            # Unknown assistant — almost always the user just exposed a new
            # agent in BGOS after the gateway started. Re-sync the pairing
            # scope from whoami and retry the lookup once before giving up, so
            # the message self-heals instead of being dropped until a manual
            # restart. _refresh_pairing_scope is rate-limited internally.
            log.info(
                "inbound for unknown assistant_id=%s — refreshing pairing scope",
                assistant_id,
            )
            await self._refresh_pairing_scope()
            route = self._state.get_route(assistant_id)
            if route is None:
                log.warning(
                    "assistant_id=%s still unknown after refresh — dropping",
                    assistant_id,
                )
                return

        # Persist the last-seen message id IMMEDIATELY after we confirm the
        # event is for a known assistant. Every inbound path past this point
        # — bridge-local short-circuit, Hermes dispatch, file batching —
        # consumes the message exactly once, so the cursor must advance
        # regardless of which branch handles it. Without this guard, the
        # bridge-local short-circuit below skipped the save and the REST
        # poll loop would keep re-finding /new (or /retry / /status) in
        # `inbound?since_message_id=<stale_cursor>` every 5 seconds and
        # re-dispatching them — producing the live-server "50 conversation
        # reset acks in a row" loop caught 2026-05-15.
        msg_id = data.get("message_id")
        if isinstance(msg_id, int):
            self._save_last_id(msg_id)

        # Server-authoritative chat addressing (2026-05-30 hardening).
        # Remember the chat this inbound targeted so later outbound
        # send/edit/get_chat_info can validate the agent isn't inventing a
        # chat id, and stash the opaque `sessionHandle` the backend mints on
        # every inbound event so replies round-trip it instead of a raw
        # chatId. Both WS (camelCase `sessionHandle`) and REST
        # (snake_case `session_handle`) shapes are accepted; the camel→snake
        # alias table doesn't cover it because the value is passed through
        # verbatim rather than re-read by field name downstream.
        inbound_chat_id = self._int_or_none(data.get("chat_id"))
        if inbound_chat_id is not None:
            session_handle = data.get("sessionHandle") or data.get("session_handle")
            self._state.record_inbound_chat(
                inbound_chat_id,
                session_handle if isinstance(session_handle, str) else None,
            )

        # A blocking peer helper with waitForReply=true has already consumed
        # this reply from the HTTP response. BGOS may still deliver the same
        # side-thread message later over WS or REST reconciliation. Mark it
        # seen, then drop it so two Hermes/BGOS agents do not answer each
        # other's already-consumed replies forever.
        if self._is_consumed_peer_wait_reply(data):
            log.info(
                "dropping already-consumed peer wait reply chat=%s msg=%s reply_to=%s conv=%s",
                data.get("chat_id"), msg_id, data.get("reply_to_id"),
                data.get("peer_conversation_id"),
            )
            return
        if await self._wait_for_consumed_peer_wait_reply_race(data):
            log.info(
                "dropping peer wait reply after race-window recheck chat=%s msg=%s reply_to=%s conv=%s",
                data.get("chat_id"), msg_id, data.get("reply_to_id"),
                data.get("peer_conversation_id"),
            )
            return

        # Bridge-local intercept (Task 9): slash_command messages whose
        # command name is in BRIDGE_LOCAL_COMMANDS are handled by the adapter.
        if data.get("message_type") == "slash_command":
            command_name = (data.get("command_name") or "").lower()
            if command_name in BRIDGE_LOCAL_COMMANDS:
                await self._handle_bridge_local(command_name, data)
                return

        event = MessageEvent.from_ws(data, agent_route=route)
        # Do NOT cache event.assistant_id as the chat owner here. For a2a
        # peer messages, assistant_id is the addressed Hermes assistant, while
        # chat.assistantId may be the peer-side thread owner. send() resolves
        # the owner via GET /chats/:id so /send-message can run the peer bridge.
        # Surface user-attached files into the agent's view of the message.
        # Hermes's MessageEvent only carries `text` to the agent's prompt —
        # there's no first-class files/attachments slot downstream — so the
        # only way the model learns about an inbound image or document is by
        # finding it in the text. Images become markdown image syntax for
        # vision-capable models; docs become labeled link lines.
        agent_visible_text = _format_inbound_files(event.text, event.files)

        # BGOS can emit empty reconciliation/backfill events after gateway
        # restarts. They carry no user-visible text and no attachments, but if
        # dispatched into Hermes they resume stale sessions and can resend the
        # previous assistant response. We already advanced the cursor above, so
        # dropping them here preserves delivery semantics without replay loops.
        if (
            not agent_visible_text.strip()
            and not event.files
            and event.message_type == "standard"
        ):
            log.info(
                "dropping empty BGOS inbound event chat=%s msg=%s",
                event.chat_id, event.message_id,
            )
            return

        # Track the most-recent inbound user_id per chat. The PATCH
        # /api/v1/messages/{id} endpoint requires `userId` (backend's
        # DTO validation; caught 2026-05-13) and the right value for
        # editing the assistant's reply is the user who prompted it.
        # Stash BEFORE the batching short-circuit so the user_id lands
        # even on the first fragment of a soon-to-merge burst.
        if event.user_id:
            self._state.last_user_id_by_chat[event.chat_id] = str(event.user_id)

        # Batching window: rapid successive plain-text messages from the
        # same chat get coalesced into one agent dispatch. Files / slash
        # commands go through immediately (no batching) since order with
        # text matters for context. Place this BEFORE the live retry-cache
        # save below so the deferred flush gets to set the merged text
        # without the live save overwriting it with the last fragment.
        # `batchable=False` opts out (backfill replay, /retry replay).
        if batchable and self._should_batch_event(event):
            self._enqueue_text_batch(event=event, text=agent_visible_text)
            return

        # Keep the retry cache populated so the /retry bridge-local can
        # resend the last user text in this chat. Use the agent-visible text
        # so attachment context replays on /retry.
        if agent_visible_text and event.message_type != "slash_command":
            self._state.last_user_text_by_chat[event.chat_id] = agent_visible_text

        # Hermes's handle_message expects a gateway-native MessageEvent with
        # a SessionSource `source` attribute (BasePlatformAdapter inspects
        # event.source immediately for auth + routing). We wrap our flat
        # vendor event into the gateway shape when Hermes is installed;
        # fall through to the vendor event otherwise (tests).
        if _GatewayMessageEvent is not None and _GatewayMessageType is not None:
            user_id = str(event.user_id) if event.user_id else None
            # REST backfill entries don't carry user_id — mark them internal
            # so the fork's auth gate (which requires source.user_id) lets
            # them through instead of dropping.
            is_backfill = user_id is None
            try:
                source = self.build_source(  # type: ignore[attr-defined]
                    chat_id=str(event.chat_id), user_id=user_id,
                )
            except AttributeError:
                # Older fork revision without build_source — skip the wrap
                # and hope the base class's default is forgiving. Use the
                # agent-visible text so attachments still surface.
                event.text = agent_visible_text
                await self.handle_message(event)
                return
            msg_type = (
                _GatewayMessageType.COMMAND  # type: ignore[attr-defined]
                if event.message_type == "slash_command"
                else _GatewayMessageType.TEXT  # type: ignore[attr-defined]
            )
            gateway_event = _GatewayMessageEvent(
                text=agent_visible_text,
                message_type=msg_type,
                source=source,
                message_id=str(event.message_id),
                raw_message=event,
                internal=is_backfill,
            )
            await self.handle_message(gateway_event)
        else:
            event.text = agent_visible_text
            await self.handle_message(event)

    def _should_batch_event(self, event: "MessageEvent") -> bool:
        """Eligible for batching: standard plain-text only (no slash
        commands, no attachments). Slash commands must flush immediately
        so order with /new and /retry is preserved; attachments must
        interleave correctly with the text that contextualizes them."""
        return (
            event.message_type == "standard"
            and bool(event.text)
            and not event.files
        )

    def _enqueue_text_batch(
        self,
        *,
        event: "MessageEvent",
        text: str,
    ) -> None:
        """Append `text` to the per-chat batch buffer and (re)schedule a
        deferred flush. Window adapts to the LAST chunk's length:
          ≤320 chars → ≤0.24s flush (user is still typing)
          ≤1024 chars → ≤0.4s flush (mid-paragraph)
          ≥4000 chars → 1.0s (split incoming from mobile client)
          else → configured _text_batch_window (default 0.6s)
        Mirrors gateway/platforms/telegram.py:3803-3859.
        """
        chat_key = event.chat_id
        batch = self._pending_text_batches.get(chat_key)
        if batch is None:
            batch = {
                "event": event,  # latest event metadata wins
                "texts": [text],
            }
            self._pending_text_batches[chat_key] = batch
        else:
            batch["texts"].append(text)
            batch["event"] = event  # latest message_id / user_id wins
        log.debug(
            "bgos batch.enqueue chat=%d msg=%d fragments_now=%d "
            "last_chunk_len=%d total_len=%d",
            event.chat_id, event.message_id, len(batch["texts"]),
            len(text), sum(len(t) for t in batch["texts"]),
        )

        # Cancel any pending flush — we'll schedule a fresh one with the
        # adapted window.
        existing = self._pending_text_tasks.get(chat_key)
        if existing is not None and not existing.done():
            existing.cancel()

        last_chunk = batch["texts"][-1]
        n = len(last_chunk)
        if n >= 4000:
            window = 1.0
        elif n <= 320:
            window = min(self._text_batch_window, 0.24)
        elif n <= 1024:
            window = min(self._text_batch_window, 0.4)
        else:
            window = self._text_batch_window

        self._pending_text_tasks[chat_key] = asyncio.create_task(
            self._flush_text_batch(chat_key, window)
        )

    async def _flush_text_batch(self, chat_key: int, window: float) -> None:
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            return
        batch = self._pending_text_batches.pop(chat_key, None)
        if batch is None:
            return
        self._pending_text_tasks.pop(chat_key, None)
        event = batch["event"]
        merged = "".join(batch["texts"])
        log.debug(
            "bgos batch.flush chat=%d msg=%d fragments=%d merged_len=%d "
            "window=%.3f",
            event.chat_id, event.message_id, len(batch["texts"]),
            len(merged), window,
        )

        # Final suppression gate: a waitForReply helper can persist the
        # concrete consumed reply receipt after _handle_inbound's short race
        # window but before this text batch flushes. Do not dispatch a queued
        # side-thread reply that has become consumed while waiting in the batch.
        if self._is_consumed_peer_wait_reply(self._peer_wait_data_from_event(event)):
            log.info(
                "dropping peer wait reply at batch flush chat=%s msg=%s reply_to=%s conv=%s",
                event.chat_id,
                event.message_id,
                getattr(event, "reply_to_id", None),
                getattr(event, "peer_conversation_id", None),
            )
            return
        peer_wait_data = self._peer_wait_data_from_event(event)
        if self._has_pending_peer_wait_marker(peer_wait_data):
            if await self._wait_for_consumed_peer_wait_reply_race(peer_wait_data):
                log.info(
                    "dropping peer wait reply at batch flush after pending wait chat=%s msg=%s reply_to=%s conv=%s",
                    event.chat_id,
                    event.message_id,
                    getattr(event, "reply_to_id", None),
                    getattr(event, "peer_conversation_id", None),
                )
                return

        # Update the retry cache with the merged text so /retry replays
        # the full message rather than just the last fragment.
        self._state.last_user_text_by_chat[event.chat_id] = merged
        # Now dispatch through the same gateway-event wrapping path as the
        # synchronous case. Wrap in try/except so a downstream handle_message
        # failure doesn't leave an unretrieved-exception on the task — matches
        # _deferred_edit_flush's pattern.
        try:
            if _GatewayMessageEvent is not None and _GatewayMessageType is not None:
                try:
                    source = self.build_source(  # type: ignore[attr-defined]
                        chat_id=str(event.chat_id),
                        user_id=str(event.user_id) if event.user_id else None,
                    )
                except AttributeError:
                    event.text = merged
                    await self.handle_message(event)
                    return
                user_id = str(event.user_id) if event.user_id else None
                is_backfill = user_id is None
                gateway_event = _GatewayMessageEvent(
                    text=merged,
                    message_type=_GatewayMessageType.TEXT,  # type: ignore[attr-defined]
                    source=source,
                    message_id=str(event.message_id),
                    raw_message=event,
                    internal=is_backfill,
                )
                await self.handle_message(gateway_event)
            else:
                event.text = merged
                await self.handle_message(event)
        except Exception:
            log.exception(
                "deferred text-batch dispatch failed chat=%s msg=%s",
                event.chat_id, event.message_id,
            )

    async def _handle_bridge_local(self, command: str, data: dict) -> None:
        """Handle a `/new`, `/retry`, or `/status` slash command locally —
        no round-trip to Hermes. Posts an ack/result back to BGOS so the
        user sees a response."""
        chat_id = data.get("chat_id")
        if not isinstance(chat_id, int):
            log.warning("bridge-local slash missing chat_id: %s", data)
            return

        if command == "new":
            self._state.reset_conversation(chat_id)
            await self._api.post_message(
                chat_id=chat_id,
                text="Conversation reset. Next message starts fresh.",
                sender="assistant",
                message_type="standard",
            )
        elif command == "retry":
            last = self._state.last_user_text_by_chat.get(chat_id)
            if not last:
                await self._api.post_message(
                    chat_id=chat_id,
                    text="Nothing to retry yet — send a message first.",
                    sender="assistant",
                    message_type="standard",
                )
                return
            # Replay the last user text as a normal inbound, forwarded to the
            # agent this time (recurse with message_type=standard so the
            # bridge-local check doesn't fire again). Bypass batching —
            # the replay is already the canonical merged text, deferring it
            # again would just confuse `/retry` semantics.
            replay = {
                **data,
                "text": last,
                "message_type": "standard",
                "command_name": None,
                "command_args": None,
            }
            await self._handle_inbound(replay, batchable=False)
        elif command == "status":
            lines = [
                "**BGOS adapter status**",
                f"- Pairing: {self.pairing_id}",
                f"- Assistants bound: {len(self._state.assistant_route)}",
                f"- Last message id seen: {self._ws.last_message_id if self._ws else 0}",
                f"- Pending approvals: {len(self._approval_state)}",
            ]
            await self._api.post_message(
                chat_id=chat_id,
                text="\n".join(lines),
                sender="assistant",
                message_type="standard",
            )

    async def sync_commands_for(self, assistant_id: int) -> None:
        """Build the merged manifest (Hermes native + bridge-locals) and PUT
        it to BGOS. Fails open — logs but doesn't raise so a catalog hiccup
        doesn't break the adapter."""
        route = self._state.get_route(assistant_id)
        if route is None:
            log.debug("sync_commands_for: unknown assistant_id=%d", assistant_id)
            return
        try:
            native = fetch_hermes_native_commands(route)
            manifest = build_manifest(native)
            await self._api.put_commands(
                assistant_id=assistant_id, commands=manifest,
            )
        except Exception:
            log.exception(
                "command manifest sync failed for assistant_id=%d (route=%s)",
                assistant_id, route,
            )

    def _is_callback_user_authorized(self, user_id: str | None) -> bool:
        """Mirrors the inbound auth gate (BGOS_ALLOW_ALL_USERS /
        BGOS_ALLOWED_USERS) for callback events. Without this check, a
        malicious user who could trigger backend callback_result delivery
        could resolve approvals targeted at someone else.

        Fail-closed by default (same posture as Hermes-side inbound)."""
        if os.environ.get("BGOS_ALLOW_ALL_USERS", "false").lower() == "true":
            return True
        allowed = os.environ.get("BGOS_ALLOWED_USERS", "").strip()
        if not allowed:
            return False
        allowed_set = {u.strip() for u in allowed.split(",") if u.strip()}
        return user_id is not None and str(user_id) in allowed_set

    async def _handle_callback(self, data: dict) -> None:
        """Route a callback_result WS event.

        `ea:{choice}:{approval_id}` → look up session_key in self._approval_state,
        call `resolve_gateway_approval(session_key, choice)` from tools.approval
        (synchronous, thread-safe — safe from this async handler). After
        resolving, edit the original approval bubble in-place to show the
        choice + user (matches Telegram's UX at
        gateway/platforms/telegram.py:2533-2537 — buttons vanish, the
        bubble shows the resolution).

        Anything else → defer to `self.handle_button_press(data)` so the
        Hermes agent sees the press naturally. If no such handler exists,
        log and drop.

        Stale approval clicks (approval_id missing from _approval_state —
        e.g. timed out, already resolved, or the adapter restarted) log and
        no-op. Telegram answers these via `"already resolved"`; BGOS's
        equivalent would need a separate API call and isn't worth the
        complexity for Phase 1.

        Authz: per-user gate (Task 2.2) — mirrors inbound message auth at
        gateway/platforms/telegram.py:405. A leaked Clerk user ID
        shouldn't be enough to resolve someone else's approval, so we
        drop callback events from users not in BGOS_ALLOWED_USERS unless
        BGOS_ALLOW_ALL_USERS=true. Dropped events leave _approval_state
        intact so the authorized user can still resolve.
        """
        # BGOS has delivered callback payloads in both snake_case
        # (`callback_data`, older webhook/REST shape) and camelCase
        # (`callbackData`, live WS / inbound_click shape). Approval buttons
        # must honor both; otherwise the tap falls through as a normal
        # "Always allow" chat message and the blocking approval times out.
        cb = data.get("callback_data") or data.get("callbackData") or ""
        user_id_for_authz = data.get("user_id") or data.get("userId")
        if not self._is_callback_user_authorized(user_id_for_authz):
            log.info(
                "dropping unauthorized callback from user_id=%s callback_data=%s",
                user_id_for_authz, cb,
            )
            return

        # Slash-confirm dispatch — must come BEFORE the approval branch
        # to keep prefix routing order-stable. `sc:*` callbacks are
        # always dispatched through resolve_slash_confirm and never fall
        # through to handle_button_press.
        m_sc = _SLASH_CONFIRM_CALLBACK_RE.match(cb)
        if m_sc is not None:
            choice = m_sc.group(1)
            confirm_id = m_sc.group(2)
            session_key = self._slash_confirm_state.pop(confirm_id, None)
            if session_key is None:
                log.info("stale slash-confirm click confirm_id=%s", confirm_id)
                return
            import hermes_channel_bgos.bgos_adapter as _self_mod
            _self_mod.resolve_slash_confirm(session_key, confirm_id, choice)
            # Edit the bubble in place to show the resolution.
            choice_labels = {
                "once":   "✅ Approved once",
                "always": "🔒 Always approve",
                "cancel": "❌ Cancelled",
            }
            user_id_label = data.get("user_id") or data.get("userId") or ""
            text = choice_labels.get(choice, choice)
            if user_id_label:
                text += f" by {user_id_label}"
            msg_id = data.get("message_id") or data.get("messageId")
            chat_id = data.get("chat_id") or data.get("chatId")
            if isinstance(msg_id, int):
                try:
                    # PATCH requires userId per backend DTO validation.
                    # The clicker IS the user attribution we want here.
                    await self._api.patch_message(
                        msg_id, text=text, options=[],
                        user_id=str(user_id_label) if user_id_label else None,
                    )
                except Exception:
                    log.warning("slash-confirm message edit failed", exc_info=True)
                # Tell the edit throttle we just spoke for this chat so a
                # subsequent edit_message() doesn't double-fire within window.
                if isinstance(chat_id, int):
                    self._last_edit_at[chat_id] = asyncio.get_running_loop().time()
            return

        m = _APPROVAL_CALLBACK_RE.match(cb)
        if m is not None:
            choice = m.group(1)
            approval_id = int(m.group(2))
            session_key = self._approval_state.pop(approval_id, None)
            if session_key is None:
                log.info(
                    "stale approval click approval_id=%d — already resolved or timed out",
                    approval_id,
                )
                return
            # Route via the module-level binding so tests can monkeypatch it.
            import hermes_channel_bgos.bgos_adapter as _self_mod
            _self_mod.resolve_gateway_approval(session_key, choice)

            # Edit the original bubble in-place to show the resolution.
            # Mirrors gateway/platforms/telegram.py:2533-2537. We bypass
            # the 1.5s edit throttle (calling self._api.patch_message
            # directly rather than self.edit_message) since this is a
            # one-shot event per approval — no edit-storm risk and the
            # UX wants the resolution to land immediately. Backend
            # callback payloads may use snake_case OR camelCase keys,
            # same as inbound_click — honor both.
            choice_labels = {
                "once":    "✅ Approved once",
                "session": "✅ Approved for session",
                "always":  "🔒 Approved permanently",
                "deny":    "❌ Denied",
            }
            user_id_label = data.get("user_id") or data.get("userId") or ""
            text = choice_labels.get(choice, choice)
            if user_id_label:
                text += f" by {user_id_label}"
            msg_id = data.get("message_id") or data.get("messageId")
            chat_id = data.get("chat_id") or data.get("chatId")
            if isinstance(msg_id, int):
                try:
                    # PATCH requires userId per backend DTO validation.
                    # The clicker IS the user attribution we want here.
                    await self._api.patch_message(
                        msg_id, text=text, options=[],
                        user_id=str(user_id_label) if user_id_label else None,
                    )
                except Exception:
                    # Cosmetic — approval already resolved by the time we get
                    # here. Swallow (e.g. message was deleted) and log.
                    log.warning(
                        "approval message edit failed message_id=%d",
                        msg_id, exc_info=True,
                    )
                # Tell the edit throttle we just spoke for this chat so a
                # subsequent edit_message() doesn't double-fire within window.
                if isinstance(chat_id, int):
                    self._last_edit_at[chat_id] = asyncio.get_running_loop().time()
            return

        handler = getattr(self, "handle_button_press", None)
        if handler is None:
            log.debug("no handle_button_press; dropping callback_data=%s", cb)
            return
        result = handler(data)
        if asyncio.iscoroutine(result):
            await result

    async def _handle_inbound_click(self, data: dict) -> None:
        """Translate a backend `inbound_click` event into a synthetic user
        MessageEvent and hand it to `self.handle_message`.

        The backend emits this event on the `assistant:<id>` room whenever a
        user taps an inline option chip on a message belonging to a paired
        assistant. Sentinel clicks (`__skip__`, `__custom__`) flow through the
        same path — the agent sees them as regular user text, matching
        Telegram/Grix behavior where a click is a pseudo-message.

        Approval clicks use a different event (`callback_result` with
        `ea:*` callback_data) — those are handled in `_handle_callback` and
        never reach here.

        Payload (camelCase — see backend websocket-outcome-event.service.ts
        `emitInboundClick`):
          { assistantId, userId, chatId, messageId, optionId, callbackData,
            buttonText, customText? }
        """
        assistant_id = data.get("assistantId")
        if assistant_id is None:
            log.debug("inbound_click missing assistantId: %s", data)
            return
        route = self._state.get_route(assistant_id)
        if route is None:
            log.warning(
                "inbound_click for unknown assistant_id=%s — dropping",
                assistant_id,
            )
            return

        chat_id = data.get("chatId")
        message_id = data.get("messageId")
        user_id = data.get("userId") or ""
        button_text = data.get("buttonText") or ""
        callback_data = data.get("callbackData") or ""
        custom_text = data.get("customText")

        # Approval/slash-confirm taps may arrive as `inbound_click` instead of
        # `callback_result` on some BGOS deployments. Route those through the
        # same resolver before creating a synthetic user message, otherwise the
        # agent merely sees button text like "Always allow" while the dangerous
        # command approval remains blocked until timeout.
        if _APPROVAL_CALLBACK_RE.match(callback_data) or _SLASH_CONFIRM_CALLBACK_RE.match(callback_data):
            await self._handle_callback({
                "callbackData": callback_data,
                "messageId": message_id,
                "chatId": chat_id,
                "userId": user_id,
                "buttonText": button_text,
            })
            return

        # The agent's natural view: the user's reply is the button's visible
        # label. `__custom__` sentinels carry the user's typed text — prefer
        # it so the agent sees the actual response.
        if callback_data == "__custom__" and custom_text:
            text = custom_text
        else:
            text = button_text

        # Persist the last-seen id so the poll loop doesn't replay the same
        # click later via REST backfill (which shouldn't happen for clicks
        # today — backfill only returns sender='user' messages — but cheap
        # insurance against future changes).
        if isinstance(message_id, int):
            self._save_last_id(message_id)

        # Server-authoritative chat addressing: a button tap is itself an
        # inbound interaction on this chat, so record it as received. Without
        # this, if a click is the first thing the adapter sees on a chat after
        # a restart (no prior text inbound), the agent's reply would be wrongly
        # rejected as targeting an un-received chat. inbound_click carries no
        # sessionHandle today, so the raw chatId is used on reply.
        click_chat_id = self._int_or_none(chat_id)
        if click_chat_id is not None:
            session_handle = data.get("sessionHandle") or data.get("session_handle")
            self._state.record_inbound_chat(
                click_chat_id,
                session_handle if isinstance(session_handle, str) else None,
            )

        # Wrap into a gateway-native MessageEvent when Hermes is installed.
        if _GatewayMessageEvent is not None and _GatewayMessageType is not None:
            try:
                source = self.build_source(  # type: ignore[attr-defined]
                    chat_id=str(chat_id),
                    user_id=str(user_id) if user_id else None,
                )
            except AttributeError:
                log.warning(
                    "inbound_click: gateway build_source unavailable — "
                    "dropping click for chat=%s", chat_id,
                )
                return
            gateway_event = _GatewayMessageEvent(
                text=text,
                message_type=_GatewayMessageType.TEXT,  # type: ignore[attr-defined]
                source=source,
                message_id=str(message_id) if message_id is not None else None,
                raw_message=data,
                internal=False,
            )
            await self.handle_message(gateway_event)
            return

        # Test path (no gateway) — pass a flat MessageEvent with the bits
        # tests usually inspect.
        event = MessageEvent(
            platform="bgos",
            chat_id=int(chat_id) if isinstance(chat_id, int) else 0,
            message_id=int(message_id) if isinstance(message_id, int) else 0,
            user_id=str(user_id),
            assistant_id=int(assistant_id),
            agent_route=route,
            text=text,
            files=[],
            message_type="standard",
            command_name=None,
            command_args=None,
        )
        await self.handle_message(event)

    def _on_reconnect(self, last_message_id: int) -> None:
        """Called by BgosWs after a successful reconnect. Schedules a REST
        backfill so messages that arrived while the socket was down are
        replayed through the same translation pipeline. Fire-and-forget — we
        don't want to block the WS connect handler."""
        asyncio.create_task(self._run_backfill(last_message_id))

    async def _run_backfill(self, last_message_id: int) -> None:
        """Fetch `GET /integrations/inbound?since_message_id=<last>` and
        replay each message through `_handle_inbound`. Exposed as a
        public-ish method so tests can invoke it directly without forcing
        a real WS disconnect.

        Field-name adaptation: backend REST responses use the primary-key
        name `id` for message rows, while the WS event uses `message_id`.
        MessageEvent.from_ws reads `message_id`, so we normalize here.
        """
        try:
            resp = await self._api.fetch_inbound_since(last_message_id)
        except Exception:
            log.exception("backfill fetch failed for since_message_id=%d",
                          last_message_id)
            return
        messages = resp.get("messages") if isinstance(resp, dict) else None
        if not messages:
            return
        for msg in messages:
            # Normalize REST → WS field name so _handle_inbound's assumption
            # (data["message_id"]) holds regardless of source.
            if "message_id" not in msg and "id" in msg:
                msg = {**msg, "message_id": msg["id"]}
            try:
                # Backfill is historical replay, not "user typing fast" —
                # bypass batching so each missed message lands as its own
                # dispatch with its own message_id.
                await self._handle_inbound(msg, batchable=False)
            except Exception:
                log.exception("backfill replay failed for message=%s", msg)
