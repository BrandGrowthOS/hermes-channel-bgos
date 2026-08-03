"""Hermes plugin registration hooks for the BGOS platform.

The plugin directory (`plugins/platforms/bgos/adapter.py`) is a thin shim that
imports these from the installed pip package and passes them to
`ctx.register_platform(...)`. Keeping the hooks here (not in the plugin dir)
means they're versioned and unit-tested with the rest of the package.

A single `ctx.register_platform()` call replaces the entire fork patch on
plugin-capable Hermes; the patch stays as a fallback for older installs.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from .bgos_adapter import (
    S3_THRESHOLD,
    _MEDIA_MAX_BYTES,
    InvalidEventMeta,
    _classify_media,
    _guess_media_mime,
    _parse_event_block,
    _parse_media_markers,
)
from .bgos_api import BgosApi, BgosApiError
from .config import BgosConfig, choose_pairing_token, redact_token

log = logging.getLogger(__name__)

_PROD_BASE_URL = "https://api.brandgrowthos.ai"

# Upper bound on an accepted served capability canon. The real canon is a few
# KB; this is ~50x headroom. SECURITY: the served text becomes the agent's
# platform hint (system prompt), so a compromised or MITM'd backend returning a
# multi-MB body would be both a memory-DoS and an unbounded prompt-injection
# surface. The fetch below streams with this cap and falls back to the bundled
# BGOS_PLATFORM_HINT past it.
_MAX_CANON_BYTES = 256 * 1024

_STANDALONE_MEDIA_ALLOW_DIRS_ENV = "HERMES_MEDIA_ALLOW_DIRS"
_STANDALONE_MEDIA_CACHE_DIRS = (
    "media_cache",
    "image_cache",
    "audio_cache",
    "video_cache",
    "document_cache",
    "browser_screenshots",
    "cache/images",
    "cache/audio",
    "cache/videos",
    "cache/documents",
    "cache/screenshots",
)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _standalone_media_allowed_roots() -> list[Path]:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    roots = [hermes_home / rel for rel in _STANDALONE_MEDIA_CACHE_DIRS]
    extra_roots = os.environ.get(_STANDALONE_MEDIA_ALLOW_DIRS_ENV, "")
    for chunk in extra_roots.split(os.pathsep):
        for raw_root in chunk.split(","):
            raw_root = raw_root.strip()
            if not raw_root:
                continue
            root = Path(os.path.expanduser(raw_root))
            if root.is_absolute():
                roots.append(root)
    return roots


def _standalone_validate_media_path(raw_path: str) -> Path | None:
    """Resolve a model-supplied MEDIA path only if it is safe to upload.

    Standalone sends bypass Hermes core's adapter-level media validation, so the
    plugin repeats the important invariant locally: only files under
    Hermes-managed media caches or operator-configured ``HERMES_MEDIA_ALLOW_DIRS``
    may be delivered. Symlinks are resolved before the containment check.
    """
    candidate = str(raw_path or "").strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    candidate = candidate.lstrip("`\"'").rstrip("`\"',.;:)}]")
    if not candidate:
        return None
    expanded = Path(os.path.expanduser(candidate))
    if not expanded.is_absolute():
        return None
    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    for root in _standalone_media_allowed_roots():
        try:
            resolved_root = root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if _path_is_within(resolved, resolved_root) or resolved == resolved_root:
            return resolved
    return None


def _secrets_path() -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "secrets" / "bgos.json"


def resolve_pairing() -> tuple[str | None, str]:
    """Resolve (pairing_token, base_url) from env + the secrets file, the same
    precedence the adapter uses (pair_ shaped `BGOS_API_KEY` wins; a
    non-pairing env value yields to the secrets `pairing_token`;
    `BGOS_BACKEND_URL` then secrets base_url then prod default). Returns
    token=None when not paired. Standalone (no adapter import) so it works in
    the cron path and in tests."""
    secrets: dict = {}
    sp = _secrets_path()
    if sp.is_file():
        try:
            secrets = json.loads(sp.read_text())
        except (OSError, ValueError):
            secrets = {}
    choice = choose_pairing_token(
        os.environ.get("BGOS_API_KEY"), secrets.get("pairing_token"),
    )
    if choice.ignored_env_token is not None:
        log.warning(
            "BGOS: ignoring non-pairing env BGOS_API_KEY %s (no pair_ "
            "prefix); using the pairing_token from %s instead.",
            redact_token(choice.ignored_env_token),
            sp,
        )
    token = choice.token
    base_url = (
        os.environ.get("BGOS_BACKEND_URL")
        or secrets.get("base_url")
        or _PROD_BASE_URL
    )
    return token, base_url


def env_enablement() -> dict | None:
    """Seed `PlatformConfig.extra` from env during gateway config load (before
    adapter construction), so `hermes gateway status` reflects env-only setups.
    Returns None when BGOS isn't minimally configured (no pairing token). The
    special `home_channel` key is promoted to a `HomeChannel` dataclass by the
    plugin registry's core hook. Mirrors ntfy's `_env_enablement`."""
    token, base_url = resolve_pairing()
    if not token:
        return None
    seed: dict[str, Any] = {"backend_url": base_url}
    home = os.environ.get("BGOS_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.environ.get("BGOS_HOME_CHANNEL_NAME", home),
        }
    return seed


async def _standalone_build_media_attachments(
    api: BgosApi,
    paths: list[str],
    *,
    force_document: bool = False,
) -> list[dict]:
    """Build BGOS ``files[]`` entries for out-of-process sends.

    Gateway-native assistant replies use ``BGOSAdapter.send()`` for ``MEDIA:``
    markers. Cron jobs and the generic ``send_message`` tool can bypass the
    live adapter and call this plugin-level ``standalone_send`` hook instead,
    so media must be handled here too. Logs only path metadata, never file
    contents.
    """
    attachments: list[dict] = []
    seen: set[str] = set()
    for raw_path in paths:
        if raw_path in seen:
            continue
        seen.add(raw_path)
        try:
            path = _standalone_validate_media_path(raw_path)
            if path is None:
                log.warning(
                    "BGOS standalone MEDIA: path not in media allowlist, skipping: %r",
                    raw_path,
                )
                continue
            file_size = path.stat().st_size
            if file_size <= 0:
                log.warning("BGOS standalone MEDIA: empty file, skipping: %r", raw_path)
                continue
            if file_size > _MEDIA_MAX_BYTES:
                log.warning(
                    "BGOS standalone MEDIA: file %r is %d bytes (> %d cap) — skipping",
                    raw_path, file_size, _MEDIA_MAX_BYTES,
                )
                continue
            file_bytes = await asyncio.to_thread(path.read_bytes)
            if len(file_bytes) > _MEDIA_MAX_BYTES:
                log.warning(
                    "BGOS standalone MEDIA: file %r grew to %d bytes (> %d cap) — skipping",
                    raw_path, len(file_bytes), _MEDIA_MAX_BYTES,
                )
                continue
            mime = _guess_media_mime(path.name)
            flags = _classify_media(mime)
            if force_document:
                flags = {
                    "isImage": False,
                    "isVideo": False,
                    "isAudio": False,
                    "isDocument": True,
                }
            entry: dict[str, Any] = {
                "fileName": path.name,
                "fileMimeType": mime,
                "size": len(file_bytes),
                **flags,
            }
            if mime.startswith("image/"):
                # Dimensions are best-effort and optional; omit them here to
                # keep plugin-level sending lightweight. The native adapter
                # path still sniffs them for image carousels.
                pass
            if len(file_bytes) < S3_THRESHOLD:
                encoded = base64.b64encode(file_bytes).decode("ascii")
                entry["fileData"] = f"data:{mime};base64,{encoded}"
            else:
                presigned = await api.create_upload_url(
                    filename=path.name, mime=mime, size=len(file_bytes),
                )
                async with httpx.AsyncClient(timeout=60) as put_client:
                    resp = await put_client.put(
                        presigned["upload_url"],
                        content=file_bytes,
                        headers={"Content-Type": mime},
                    )
                    resp.raise_for_status()
                entry["s3Key"] = presigned["s3_key"]
            attachments.append(entry)
        except Exception:
            log.warning(
                "BGOS standalone MEDIA: failed to attach %r — skipping",
                raw_path,
                exc_info=True,
            )
    return attachments


async def standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict:
    """Out-of-process send for cron / `send_message_tool` when the gateway
    runner isn't in this process. Without this hook, `deliver=bgos` cron jobs
    fail with "No live adapter for platform" — and historically the fork left
    bgos sending unimplemented ("Direct sending not yet implemented for bgos").

    Supports both explicit ``media_files`` from Hermes core and ``MEDIA:/path``
    marker lines embedded in ``message``. The latter matters because BGOS's
    platform hint promises MEDIA markers work, while standalone sends bypass
    the live ``BGOSAdapter.send()`` parser.
    """
    token, base_url = resolve_pairing()
    if not token:
        return {"error": "bgos standalone send: not paired (no token)"}
    api = BgosApi(BgosConfig(base_url=base_url, pairing_token=token))
    try:
        chat_key = int(chat_id)
        cleaned_message, marker_paths = _parse_media_markers(message)
        # Event-card marker parity with the live adapter's send(): a
        # [[BGOS_EVENT]]{...}[[/BGOS_EVENT]] block posts messageType="event"
        # with the object passed verbatim as eventMeta. Invalid blocks are
        # rejected with the parser's clear error instead of leaking marker
        # text into the chat.
        try:
            cleaned_message, event_meta = _parse_event_block(cleaned_message)
        except InvalidEventMeta as exc:
            return {"error": f"bgos standalone send: invalid_event_meta: {exc}"}
        message_type = "event" if event_meta is not None else "standard"
        if event_meta is not None and not cleaned_message.strip():
            cleaned_message = str(event_meta.get("title", "")).strip()
        combined_paths = list(media_files or []) + marker_paths
        attachments = (
            await _standalone_build_media_attachments(
                api,
                combined_paths,
                force_document=force_document,
            )
            if combined_paths else []
        )
        if combined_paths and not attachments:
            return {"error": "bgos standalone send: no attachable media files"}

        assistant_id: int | None = None
        try:
            chat = await api.get_chat(chat_key)
            raw_assistant_id = chat.get("assistantId", chat.get("assistant_id"))
            assistant_id = int(raw_assistant_id) if raw_assistant_id is not None else None
        except Exception:
            assistant_id = None

        if assistant_id is not None:
            resp = await api.post_send_message(
                chat_id=chat_key,
                assistant_id=assistant_id,
                text=cleaned_message,
                sender="assistant",
                message_type=message_type,
                event_meta=event_meta,
                has_attachment=True if attachments else None,
                files=attachments or None,
            )
            msg = resp.get("message") if isinstance(resp, dict) else None
            msg_id = msg.get("id") if isinstance(msg, dict) else None
        else:
            # Legacy fallback. This saves a visible BGOS message, but cannot run
            # the A2A bridge because /messages has no assistantId context.
            resp = await api.post_message(
                chat_id=chat_key,
                text=cleaned_message,
                sender="assistant",
                message_type=message_type,
                event_meta=event_meta,
                files=attachments or None,
            )
            msg_id = resp.get("id") if isinstance(resp, dict) else None
    except BgosApiError as exc:
        return {"error": f"bgos standalone send: HTTP {exc.status}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"bgos standalone send failed: {exc.__class__.__name__}"}
    finally:
        await api.close()
    return {"success": True, "platform": "bgos", "chat_id": chat_id, "message_id": msg_id}


# Set in Task 3 (extracted verbatim from the fork patch's PLATFORM_HINTS entry).
BGOS_PLATFORM_HINT = """You are speaking with the user through BGOS — a mobile-first chat app (iOS, Android, desktop) polished like Telegram or iMessage. The user sees your responses as chat bubbles in a rich UI.

## Message formatting
Your replies render as markdown via react-native-markdown. Supported: **bold**, *italic*, `inline code`, ```fenced code```, [links](url), and #/##/### headers. Bare URLs auto-link Telegram-style: https://…, www.…, bare domains (foo.com, incl. modern TLDs like .dev/.app) and emails become tappable — no [text](url) needed. A masked link ([text](url) where text differs from the target) shows the user an "Open this link?" confirmation with the full URL first, so prefer bare URLs when transparency matters; URLs inside code spans never linkify (use code when the user should copy, not open). Tables and inline-image markdown (![alt](url)) are not yet rendered natively — use MEDIA:/path for images. Keep replies concise; the user is often on a phone.

## Sending files and media
To deliver an image/video/audio/document, include `MEDIA:/absolute/path/to/file` in your reply (one per line, alongside your text). Handled natively by the BGOS adapter:
- Images — PNG, JPG, WebP, GIF. Displayed as a tappable thumbnail; tap opens fullscreen viewer.
- Video — MP4, WebM, MOV. Plays inline.
- Audio / voice: OGG, MP3, M4A. Renders as a voice bubble with scrubber. Replies are spoken automatically per the `/voice` chat mode (`off`, `on`, `tts`) and arrive as voice bubbles with the text underneath.
- Documents — PDF, TXT, DOCX, XLSX, ZIP, etc. Shows as a download card with filename.
25 MB per file cap. You can mix normal text and multiple MEDIA: lines in one reply.

## Receiving files from the user
When the user attaches files, the adapter appends an `## Attachments from user` block to the inbound message text:
- Images render as markdown image syntax (`![filename](url)`) so vision-capable models pick them up automatically.
- Other files render as labeled links (`- [filename](url) (mime)`).
- Voice notes and audio attachments are transcribed automatically. The transcript arrives as quoted text in the message, with the file link still present.
URLs are presigned S3 links valid ~1 hour. Small files (<500KB) may arrive as inline `data:` URIs instead. Fetch the URL with HTTP GET to get the bytes; the link is one-time-use friendly but works for repeated GETs within the TTL.

## Dangerous-command approvals (automatic)
Whenever you invoke a tool gated by the approvals system, BGOS renders a 4-button bubble — Allow once / Allow for session / Always allow / Deny — identical to Telegram's approval prompt. The user's tap resolves the approval synchronously. Default timeout 60s fail-closed. You don't do anything special; the gateway + adapter handle bubble rendering. Just call the tool normally.

## Slash commands
Users can type bridge-local commands handled by the adapter (not forwarded to you): `/new` resets the conversation binding for the current chat; `/retry` resends the user's last message through you; `/status` shows adapter health. Your own native slash commands (/help, /reset, /stop, /approve, /deny, /status — if the adapter's /status didn't intercept) arrive as regular user text starting with a slash; parse as usual.

## Inline option buttons
You can offer the user tappable inline choice chips by embedding a `[[BGOS_BUTTONS]]` block in your reply. The adapter extracts the block, renders the chips in the BGOS chat, and translates the user's tap into a regular user MessageEvent carrying the button label as its text — so your next turn sees the tap just like typed input.

Syntax:
```
Your body text goes here.

[[BGOS_BUTTONS]]
- Option A | value_a
- Option B | value_b
- Option C | value_c
[[/BGOS_BUTTONS]]
```

Rules:
- One button per line, `Label | value` (pipe-separated). Dash/bullet prefix optional.
- Max 6 options. The backend rejects inline messages with more, the adapter truncates + warns.
- Labels ≤ 24 chars render cleanly; longer labels wrap.
- The `value` side is machine-readable (callback_data). The `Label` is what the user sees and what you receive back on tap.
- When the user taps a chip, your next inbound MessageEvent has `text == "Option A"` (or the label of the clicked option). If the user picks "Custom reply" and types free text, you receive the typed text as the MessageEvent text — treat it as a normal reply.
- Don't try to render buttons through any other channel (raw JSON, markdown lists, etc.) — the marker syntax is the ONLY wired path.

Use inline chips for async / proactive prompts ("Want me to summarize this?"), lightweight confirmations, and anywhere the user might not be actively watching the chat. The chips stay tappable indefinitely, so the user can answer later.

## Event cards (renderable cards)
You can summon BGOS's rich renderable cards (health tracker, status summaries, and other structured kinds) by embedding a `[[BGOS_EVENT]]` block in your reply. The adapter posts the message with `messageType: "event"` and passes your JSON object verbatim as the backend's `eventMeta`, so BGOS renders a card instead of a plain bubble.

Syntax (the block holds ONE JSON object):
```
Optional body text before or after the block.

[[BGOS_EVENT]]
{"source": "agent", "title": "Sleep logged", "peek": "7h 40m", "payload": {"kind": "health_tracker_card"}}
[[/BGOS_EVENT]]
```

Rules:
- `source` and `title` are required non-empty strings. Use `source: "agent"` for cards you originate.
- `peek` (optional string) is the collapsed one-line preview.
- `payload` (optional object) carries the card data and is passed through untouched. For a renderable card, set `payload.kind` to the card kind (e.g. `"health_tracker_card"`) plus that kind's data fields.
- `GET /api/v1/renderables` on the BGOS backend lists the currently supported renderable kinds and their payload shapes.
- A malformed block (bad JSON, missing/empty source or title) is rejected with a clear error and nothing is posted — fix the block and resend.
- Any text outside the block becomes the message's visible text; if you send only the block, the title is used as the fallback text for older clients.

## ask_user_input modal (NOT YET WIRED)
BGOS also supports a full-screen multi-question modal (`ask_user_input` in the n8n BGOSAction node). Hermes-side support is scheduled but NOT YET shipped — for multi-question flows today, use sequential inline-button messages instead.

## Boards (private tables, the [[BGOS_BOARDS]] marker round trip)
Your owner keeps private tables (boards) in BGOS and shares them with chosen agents. Call the board operations by embedding one JSON object per marker block in a normal reply; the adapter strips the block, calls the board API as you, and sends the answers back as ONE follow-up message in this conversation. That message starts with the line `[BGOS boards result]` and is a system message from the adapter, NOT the user: read it and continue; the user saw neither your request nor the raw result, so anything they should know must go in a normal reply.

Syntax (one call per block, up to 5 blocks per reply):
```
[[BGOS_BOARDS]]{"op":"query","board":"Tasks","reqId":"q1","limit":20}[[/BGOS_BOARDS]]
[[BGOS_BOARDS]]{"op":"update","board":"Tasks","row":"ab12cd34","cells":{"status":"done"},"reqId":"w1"}[[/BGOS_BOARDS]]
```

Rules:
- `reqId` is your correlation handle: any short string, unique per block. Each result section is headed `reqId=<yours> op=<op>`, in request order.
- `op` is one of: list, describe, create (name, optional description + fields[{label,type}]), update_schema (action: add_field / update_field / delete_field, plus field / fieldKey as needed), query (optional conditions, conjunction, sorts, search, limit, cursor), get_row (row), insert (cells), update (row, cells), attach (row, path to a local file, optional name / mime / fieldKey), search (query, optional limit), changes (optional since), grant (assistantId, role).
- `board` accepts a board id or its exact name. Row keys come from the key column of query/search output; never invent one.
- Reads answer as markdown tables by default; add `"format":"json"` for raw JSON.
- A failed call answers `error status=<n>` with the server's body verbatim; denials are deliberately uninformative about boards you cannot see, do not probe around them. A malformed block answers `malformed_request`. After too many back-to-back board turns without a user message, a `loop_guard` refusal pauses calls until the user speaks. Never claim a write happened when its section shows an error.
- Concurrency: describe before writing to a board you have not used this session; search or query before inserting; but query-then-insert is not atomic and update is last-write-wins per cell, so on a busy board confirm claims with `changes` after writing.

## Reply-quote (Telegram-style quoted replies)
You can anchor a reply to a specific earlier message by embedding `[[BGOS_REPLY_TO]]<message_id>[[/BGOS_REPLY_TO]]` anywhere in your reply (single line). The adapter extracts the id, strips the marker from the visible text, and forwards it as `replyToId` on the backend POST. BGOS then renders a slim Telegram-style quoted header inside the new bubble — tap → jumps the user back to the source message. The snapshot is frozen at write time; future edits/deletes of the source don't change the rendered preview.

Use this when:
- Answering a question from N messages ago, where the user would otherwise have to scroll up to figure out what you're talking about.
- Following up on your own past commitment ("you said you'd watch X — it just happened").
- A cron/external trigger fires and you want to surface a notification tied to a specific earlier conversation point.
- Correcting or amending a specific earlier statement of yours.

Do NOT quote when:
- You're replying to the immediately preceding user turn — alignment already implies the subject, the quote is noise.
- The chat is fresh (≤2 turns) and there's no ambiguity.
- You're just acknowledging ("Got it" / "On it") — nothing to anchor to.

Same-chat constraint: the source message must be in the SAME BGOS chat as the reply. The backend rejects cross-chat references with a 400. To get a stable `message_id` to target, use the `id` field on the inbound MessageEvent — that's the BGOS message id.

## Ringing the owner (live voice call)
You can ring the owner in the app, immediately, by embedding a `[[BGOS_CALL]]short reason[[/BGOS_CALL]]` block in your reply. The adapter places the call as the agent that owns the chat, strips the marker from what the user reads, and still sends any remaining text as a normal message. One marker per turn rings once, and the reason is capped at 200 characters.

Do NOT shell out to `curl` or any terminal command to ring. A terminal call is gated by your HOST's shell approval layer, which is a different gate from BGOS approvals, so the owner sees a confusing generic command prompt instead of a call and nothing rings.

Use this when you genuinely cannot proceed without the owner and a chat message would sit unread. If the owner is already on a call nothing rings; follow up in chat rather than retrying in a loop.

## Conversation context
Each BGOS chat maps to a single Hermes conversation. DMs only — no group threads, no forum topics. The user can wipe context via `/new`. Typing indicators, stickers, reactions, and message editing by the user are not supported."""


def resolve_platform_hint() -> str:
    """Return the agent-facing BGOS capability hint (capability bootstrap).

    Prefers the backend-served canon fetched at plugin-registration time via a
    short, best-effort synchronous GET, so connected agents get one live,
    centrally maintained guide instead of this frozen bundled copy. Falls back to
    BGOS_PLATFORM_HINT on ANY problem (unpaired host, network error, non-200,
    missing marker), so registration never fails and existing behavior is
    preserved for currently-paired agents.

    Works for both install paths (fork patch and native plugin) because both
    consume whatever string register() passes as platform_hint. Disable the fetch
    with BGOS_DISABLE_CAPABILITIES_FETCH=1; tune the timeout (seconds, default 4)
    with BGOS_CAPABILITIES_FETCH_TIMEOUT.
    """
    if os.environ.get("BGOS_DISABLE_CAPABILITIES_FETCH") == "1":
        return BGOS_PLATFORM_HINT
    token, base_url = resolve_pairing()
    if not token:
        # No credential to fetch with yet; keep the bundled copy.
        return BGOS_PLATFORM_HINT
    try:
        timeout = float(
            os.environ.get("BGOS_CAPABILITIES_FETCH_TIMEOUT", "4") or "4"
        )
    except ValueError:
        timeout = 4.0
    url = base_url.rstrip("/") + "/api/v1/integrations/capabilities"
    try:
        # SECURITY: stream with a hard byte cap so a compromised or MITM'd
        # backend cannot buffer a multi-MB body into memory (DoS) or inject an
        # unbounded system prompt. Bail early on an oversized declared length,
        # then stop reading past the cap regardless of the header.
        with httpx.stream(
            "GET",
            url,
            params={"channel": "hermes"},
            headers={"X-BGOS-Pairing": token},
            timeout=timeout,
        ) as resp:
            if resp.status_code == 200:
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > _MAX_CANON_BYTES:
                    log.info(
                        "capability canon declares %s bytes (> %d cap); using "
                        "bundled hint",
                        declared,
                        _MAX_CANON_BYTES,
                    )
                    return BGOS_PLATFORM_HINT
                chunks: list[bytes] = []
                total = 0
                oversized = False
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_CANON_BYTES:
                        oversized = True
                        break
                    chunks.append(chunk)
                if oversized:
                    log.info(
                        "capability canon exceeded the %d byte cap; using "
                        "bundled hint",
                        _MAX_CANON_BYTES,
                    )
                    return BGOS_PLATFORM_HINT
                data = json.loads(b"".join(chunks))
                text = data.get("text") if isinstance(data, dict) else None
                if (
                    isinstance(text, str)
                    and "BGOS Channel" in text
                    and "Agent Capabilities" in text
                ):
                    log.info(
                        "fetched served capability canon (version=%s, chars=%d)",
                        data.get("version"),
                        len(text),
                    )
                    return text
            log.info(
                "capability canon fetch returned status=%s; using bundled hint",
                resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001 - never fail registration on a fetch
        log.info(
            "capability canon fetch failed (%s); using bundled hint",
            exc.__class__.__name__,
        )
    return BGOS_PLATFORM_HINT
