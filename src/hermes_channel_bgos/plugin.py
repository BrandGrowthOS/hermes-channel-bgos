"""Hermes plugin registration hooks for the BGOS platform.

The plugin directory (`plugins/platforms/bgos/adapter.py`) is a thin shim that
imports these from the installed pip package and passes them to
`ctx.register_platform(...)`. Keeping the hooks here (not in the plugin dir)
means they're versioned and unit-tested with the rest of the package.

A single `ctx.register_platform()` call replaces the entire fork patch on
plugin-capable Hermes; the patch stays as a fallback for older installs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .bgos_api import BgosApi, BgosApiError
from .config import BgosConfig

_PROD_BASE_URL = "https://api.brandgrowthos.ai"


def _secrets_path() -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "secrets" / "bgos.json"


def resolve_pairing() -> tuple[str | None, str]:
    """Resolve (pairing_token, base_url) from env + the secrets file — the same
    precedence the adapter uses (`BGOS_API_KEY` → secrets token; `BGOS_BACKEND_URL`
    → secrets base_url → prod default). Returns token=None when not paired.
    Standalone (no adapter import) so it works in the cron path and in tests."""
    secrets: dict = {}
    sp = _secrets_path()
    if sp.is_file():
        try:
            secrets = json.loads(sp.read_text())
        except (OSError, ValueError):
            secrets = {}
    token = os.environ.get("BGOS_API_KEY") or secrets.get("pairing_token")
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

    `thread_id` / `media_files` / `force_document` are accepted for signature
    parity with the registry's sender protocol; BGOS delivers text here.
    """
    token, base_url = resolve_pairing()
    if not token:
        return {"error": "bgos standalone send: not paired (no token)"}
    api = BgosApi(BgosConfig(base_url=base_url, pairing_token=token))
    try:
        chat_key = int(chat_id)
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
                text=message,
                sender="assistant",
                message_type="standard",
            )
            msg = resp.get("message") if isinstance(resp, dict) else None
            msg_id = msg.get("id") if isinstance(msg, dict) else None
        else:
            # Legacy fallback. This saves a visible BGOS message, but cannot run
            # the A2A bridge because /messages has no assistantId context.
            resp = await api.post_message(chat_id=chat_key, text=message)
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
- Audio / voice — OGG, MP3, M4A. Renders as a voice bubble with scrubber.
- Documents — PDF, TXT, DOCX, XLSX, ZIP, etc. Shows as a download card with filename.
25 MB per file cap. You can mix normal text and multiple MEDIA: lines in one reply.

## Receiving files from the user
When the user attaches files, the adapter appends an `## Attachments from user` block to the inbound message text:
- Images render as markdown image syntax (`![filename](url)`) so vision-capable models pick them up automatically.
- Other files render as labeled links (`- [filename](url) (mime)`).
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

## ask_user_input modal (NOT YET WIRED)
BGOS also supports a full-screen multi-question modal (`ask_user_input` in the n8n BGOSAction node). Hermes-side support is scheduled but NOT YET shipped — for multi-question flows today, use sequential inline-button messages instead.

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

## Conversation context
Each BGOS chat maps to a single Hermes conversation. DMs only — no group threads, no forum topics. The user can wipe context via `/new`. Typing indicators, stickers, reactions, and message editing by the user are not supported."""
