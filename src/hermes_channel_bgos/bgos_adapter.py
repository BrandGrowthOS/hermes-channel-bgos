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
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .bgos_api import BgosApi
from .bgos_ws import BgosWs
from .commands_sync import (
    BRIDGE_LOCAL_COMMANDS,
    build_manifest,
    fetch_hermes_native_commands,
)
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


# Matches Telegram's callback_data format at gateway/platforms/telegram.py:1275.
# Four choice values: once, session, always, deny — mapped 1:1 from Hermes's
# approval vocabulary.
_APPROVAL_CALLBACK_RE = re.compile(r"^ea:(once|session|always|deny):(\d+)$")

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

# Backend rejects inline messages with >6 options (see PR #62 + memory
# `inline_buttons_shipped.md`). Agents that emit more get truncated; we log
# a warning so this isn't silent.
_INLINE_OPTION_LIMIT = 6

# Agent-emitted peer-collaboration markers. The agent embeds these in its
# reply text to drive a2a side-conversations without needing to know the
# REST endpoints. The adapter extracts each marker, strips it from the
# user-visible text, and dispatches the corresponding peer call.
#
# `[[BGOS_PEER_SEND name="Hades" text="..." wait=false]]` — send to peer.
# `[[BGOS_PEER_COMPLETE summary="..."]]` — close the most recent open peer
# conversation in this chat. Optional summary attribute.
#
# Attribute values are quoted with `"` and may contain escaped `\"`. Order
# of attributes is free. See bgos-agent-capabilities.md §11.
_PEER_SEND_BLOCK_RE = re.compile(
    r"\[\[BGOS_PEER_SEND(?P<attrs>[^\]]*)\]\]",
    re.IGNORECASE,
)
_PEER_COMPLETE_BLOCK_RE = re.compile(
    r"\[\[BGOS_PEER_COMPLETE(?P<attrs>[^\]]*)\]\]",
    re.IGNORECASE,
)
_PEER_ATTR_RE = re.compile(r'(\w+)\s*=\s*"((?:\\"|[^"])*)"')

# Threshold below which we inline base64 into POST /messages; at or above,
# we upload via a presigned S3 PUT and reference by s3_key. Mirrors
# openclaw-channel-bgos's policy + the memory note `bug_base64_body_limit.md`.
S3_THRESHOLD = 500 * 1024


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
        )

try:  # pragma: no cover - exercised only when Hermes is installed
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


def _parse_peer_attrs(raw: str) -> dict[str, str]:
    """Parse `key="value"` pairs from a marker's attribute string.
    Tolerates whitespace and quoted backslash-escapes. Unknown keys are
    kept as-is so the dispatcher can complain with a clear message.
    """
    out: dict[str, str] = {}
    for match in _PEER_ATTR_RE.finditer(raw):
        key = match.group(1).lower()
        value = match.group(2).replace('\\"', '"')
        out[key] = value
    return out


def _extract_peer_directives(content: str) -> tuple[str, list[dict], list[dict]]:
    """Extract `[[BGOS_PEER_SEND]]` / `[[BGOS_PEER_COMPLETE]]` markers from
    agent text. Returns `(cleaned_text, sends, completes)` where each
    `sends`/`completes` entry is the parsed attribute dict. Markers are
    stripped from the text. Surrounding blank lines are collapsed.
    """
    sends: list[dict] = []
    completes: list[dict] = []
    cleaned = content
    for match in _PEER_SEND_BLOCK_RE.finditer(content):
        sends.append(_parse_peer_attrs(match.group("attrs") or ""))
    for match in _PEER_COMPLETE_BLOCK_RE.finditer(content):
        completes.append(_parse_peer_attrs(match.group("attrs") or ""))
    if sends or completes:
        cleaned = _PEER_SEND_BLOCK_RE.sub("", cleaned)
        cleaned = _PEER_COMPLETE_BLOCK_RE.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, sends, completes


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
        if _HERMES_BGOS_PLATFORM is not None:
            super().__init__(config, _HERMES_BGOS_PLATFORM)
        else:
            # Mock path — accepts whatever we pass.
            super().__init__()
        bgos_config = self._resolve_config(config)
        self._config = bgos_config
        self._api = BgosApi(bgos_config)
        self._state = StateStore()
        self._ws: BgosWs | None = None
        self.pairing_id: int | None = None
        # Approval bookkeeping — approval_id → session_key. Populated by
        # send_exec_approval, drained by Task 8's callback router.
        self._approval_state: dict[int, str] = {}
        self._approval_id_counter = itertools.count(1)
        # REST poll loop — fallback for server-side WS push gap (see
        # _poll_loop for details). Started in connect(), cancelled in
        # disconnect().
        self._poll_task: asyncio.Task | None = None

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
            log.info("pushed agent catalog: %d entries", len(agents))
        except Exception:
            log.exception("agent catalog push failed (non-fatal)")

    def _enumerate_agents(self) -> list[dict]:
        """Discover Hermes's configured agents for the agent-catalog push.

        Checked in order; first non-empty source wins:

        1. `BGOS_AGENTS_JSON` env var — a JSON list of
           `{"agent_route": str, "name": str, "description"?: str,
            "avatar_url"?: str}` objects. Use for rich descriptions or when
           names contain commas/colons.
        2. `BGOS_AGENTS` env var — comma-separated `route:Display Name` pairs
           (e.g. `"hades:Hades,ramy:Ramy"`). If a bare route is given without
           a colon, it's used as both route and display name.
        3. (TODO — Phase 4) Hermes's runtime agent registry. When the adapter
           can introspect the gateway's configured agents directly, this
           env-var indirection goes away.

        Returns `[]` when nothing is configured. Callers should treat an
        empty list as a warn-but-continue condition, not an error.
        """
        raw_json = os.environ.get("BGOS_AGENTS_JSON", "").strip()
        if raw_json:
            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError:
                log.warning("BGOS_AGENTS_JSON is not valid JSON — ignoring")
            else:
                if isinstance(data, list):
                    out = []
                    for entry in data:
                        if isinstance(entry, dict) and entry.get("agent_route"):
                            out.append(entry)
                    if out:
                        return out

        raw = os.environ.get("BGOS_AGENTS", "").strip()
        if raw:
            out: list[dict] = []
            for piece in raw.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                if ":" in piece:
                    route, name = piece.split(":", 1)
                    route = route.strip()
                    name = name.strip() or route
                else:
                    route = piece
                    name = piece
                if route:
                    out.append({"agent_route": route, "name": name})
            return out

        return []

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
        """Idempotent — safe to call multiple times (e.g. from a signal handler)."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._ws is not None:
            try:
                await self._ws.stop()
            finally:
                self._ws = None
        await self._api.close()

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

        Media variants (`send_image`, `send_voice`, …) are optional
        class-level overrides the gateway duck-types. `metadata` is accepted
        for Hermes interface compatibility but not currently plumbed through.
        `reply_to` IS plumbed: it maps to backend `replyToId`, which is what
        agent-to-agent (a2a) side-thread chats use to correlate the target
        assistant's reply with the inbound peer message — without it, the
        originator's pollForReply() falls back to positional matching, which
        works for 1:1 side threads but not for any future fan-in patterns.
        """
        cleaned_text, options, render_mode = _parse_buttons_block(content)
        # Strip peer-collaboration markers BEFORE we POST so the user-visible
        # text is clean. We dispatch the markers AFTER the agent's user-
        # visible reply lands (so the SideConversationCard has a parent
        # message to anchor to).
        cleaned_text, peer_sends, peer_completes = _extract_peer_directives(
            cleaned_text,
        )
        resp = await self._api.post_message(
            chat_id=int(chat_id),
            text=cleaned_text,
            sender="assistant",
            message_type="standard",
            options=options or None,
            render_mode=render_mode,
            reply_to_id=int(reply_to) if reply_to is not None else None,
        )
        message_id = resp.get("id") if isinstance(resp, dict) else None
        if isinstance(chat_id, int) and isinstance(message_id, int):
            self._state.last_assistant_message_by_chat[chat_id] = message_id
            assistant_id_int = self._infer_assistant_for_chat(int(chat_id))
            if assistant_id_int is not None and (peer_sends or peer_completes):
                await self._dispatch_peer_directives(
                    chat_id=int(chat_id),
                    assistant_id=assistant_id_int,
                    parent_message_id=message_id,
                    sends=peer_sends,
                    completes=peer_completes,
                )
        return _send_result(message_id=message_id)

    def _infer_assistant_for_chat(self, chat_id: int) -> int | None:
        """Best-effort lookup of the assistant id bound to a chat.

        We don't keep a chat→assistant index — but every recent inbound on
        this chat carried `assistant_id`, so the most recently dispatched
        message id (which we DO track per chat) is enough to reverse-lookup.
        For the marker dispatcher we just need ANY assistant the agent owns
        in this pairing — a single-assistant pairing is the common case.
        """
        if len(self._state.assistant_route) == 1:
            return next(iter(self._state.assistant_route.keys()))
        # Multi-assistant pairings: we'd need a chat→assistant index. Until
        # we maintain one, return None and the dispatcher logs a warning so
        # the directive doesn't silently drop.
        return None

    async def _dispatch_peer_directives(
        self,
        *,
        chat_id: int,
        assistant_id: int,
        parent_message_id: int,
        sends: list[dict],
        completes: list[dict],
    ) -> None:
        """Dispatch parsed peer markers AFTER the agent's user-visible
        reply has been posted. Failures are surfaced as a follow-up
        assistant message rather than raised — never crash the send path.
        """
        for attrs in sends:
            try:
                await self._dispatch_peer_send_marker(
                    chat_id=chat_id,
                    assistant_id=assistant_id,
                    parent_message_id=parent_message_id,
                    attrs=attrs,
                )
            except Exception as err:  # noqa: BLE001
                log.exception("peer_send marker dispatch failed")
                await self._api.post_message(
                    chat_id=chat_id,
                    text=f"**Peer send failed:** {err}",
                    sender="assistant",
                    message_type="standard",
                )
        for attrs in completes:
            try:
                summary = (attrs.get("summary") or "").strip() or None
                tracked = self._state.peer_conversation_by_chat.get(chat_id)
                if tracked is None:
                    log.info(
                        "peer_complete marker without an open conversation in chat %d",
                        chat_id,
                    )
                    continue
                peer_id, _conv_id = tracked
                await self._api.complete_peer_thread(
                    caller_assistant_id=assistant_id,
                    peer_assistant_id=peer_id,
                    summary=summary,
                )
                self._state.peer_conversation_by_chat.pop(chat_id, None)
            except Exception as err:  # noqa: BLE001
                log.exception("peer_complete marker dispatch failed")
                await self._api.post_message(
                    chat_id=chat_id,
                    text=f"**Peer complete failed:** {err}",
                    sender="assistant",
                    message_type="standard",
                )

    async def _dispatch_peer_send_marker(
        self,
        *,
        chat_id: int,
        assistant_id: int,
        parent_message_id: int,
        attrs: dict,
    ) -> None:
        text = attrs.get("text") or ""
        name_or_id = attrs.get("name") or attrs.get("id") or ""
        wait = (attrs.get("wait") or "false").lower() == "true"
        turn_state = attrs.get("turn") or attrs.get("turn_state") or None
        if not text or not name_or_id:
            log.warning(
                "BGOS_PEER_SEND missing required attrs: %s", attrs,
            )
            return
        peer_id = await self._resolve_peer_arg(assistant_id, name_or_id)
        if peer_id is None:
            await self._api.post_message(
                chat_id=chat_id,
                text=f"**Peer send failed:** no peer matches `{name_or_id}`.",
                sender="assistant",
                message_type="standard",
            )
            return
        result = await self._api.send_to_peer(
            caller_assistant_id=assistant_id,
            target_assistant_id=peer_id,
            text=text,
            parent_message_id=parent_message_id,
            wait_for_reply=wait,
            turn_state=turn_state,
        )
        if result.get("status") == "requires_introduction":
            await self._api.post_message(
                chat_id=chat_id,
                text=(
                    f"**Cannot send to peer #{peer_id}** — the user has not "
                    f"enabled this direction in the Agent Permissions matrix."
                ),
                sender="assistant",
                message_type="standard",
            )
            return
        conv_id = result.get("conversationId")
        if isinstance(conv_id, int):
            self._state.peer_conversation_by_chat[chat_id] = (peer_id, conv_id)

    # -------------------------------------------------------------------------
    # Optional media overrides (Task 6) — Hermes duck-types these at send time.
    # The gateway's fallback is to call `send()` with a caption, so NOT
    # defining these is "fine" — it just degrades media to a text notice.
    # -------------------------------------------------------------------------

    async def _upload_and_attach(
        self, *, file_bytes: bytes, filename: str, mime: str,
    ) -> dict:
        """Return a files[] entry ready for POST /messages.

        Policy: inline base64 below `S3_THRESHOLD`, presigned S3 PUT above.
        Keys are camelCase on the wire to match OpenClaw's shape
        (openclaw-channel-bgos/src/types.ts OutboundMessagePayload.files):
        `fileName`, `fileMimeType`, `fileData` (inline), `s3Key` (presigned).
        """
        size = len(file_bytes)
        if size < S3_THRESHOLD:
            return {
                "fileName": filename,
                "fileMimeType": mime,
                "size": size,
                "fileData": base64.b64encode(file_bytes).decode("ascii"),
            }
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
        return {
            "fileName": filename,
            "fileMimeType": mime,
            "size": size,
            "s3Key": presigned["s3_key"],
        }

    async def _send_media(
        self, *, chat_id: int | str, file_bytes: bytes, filename: str,
        mime: str, caption: str | None, reply_to: int | None = None,
    ) -> SendResult:
        attach = await self._upload_and_attach(
            file_bytes=file_bytes, filename=filename, mime=mime,
        )
        resp = await self._api.post_message(
            chat_id=int(chat_id),
            text=caption or "",
            sender="assistant",
            message_type="standard",
            files=[attach],
            reply_to_id=int(reply_to) if reply_to is not None else None,
        )
        message_id = resp.get("id") if isinstance(resp, dict) else None
        if isinstance(chat_id, (int, str)) and isinstance(message_id, int):
            self._state.last_assistant_message_by_chat[int(chat_id)] = message_id
        return _send_result(message_id=message_id)

    async def send_image(
        self, *, chat_id: int | str, file_bytes: bytes, filename: str,
        mime: str, caption: str | None = None,
    ) -> SendResult:
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename, mime=mime, caption=caption,
        )

    async def send_voice(
        self, *, chat_id: int | str, file_bytes: bytes, filename: str,
        mime: str, caption: str | None = None,
    ) -> SendResult:
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename, mime=mime, caption=caption,
        )

    async def send_video(
        self, *, chat_id: int | str, file_bytes: bytes, filename: str,
        mime: str, caption: str | None = None,
    ) -> SendResult:
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename, mime=mime, caption=caption,
        )

    async def send_document(
        self, *, chat_id: int | str, file_bytes: bytes, filename: str,
        mime: str, caption: str | None = None,
    ) -> SendResult:
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename, mime=mime, caption=caption,
        )

    async def send_animation(
        self, *, chat_id: int | str, file_bytes: bytes, filename: str,
        mime: str, caption: str | None = None,
    ) -> SendResult:
        return await self._send_media(
            chat_id=chat_id, file_bytes=file_bytes,
            filename=filename, mime=mime, caption=caption,
        )

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
        approval_meta = {
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

    async def get_chat_info(self, chat_id: int | str) -> dict:
        """BGOS is DM-only — a chat is its own minimal context. If later
        tasks need real chat metadata (title, participants), we extend this
        to call an endpoint on BGOS. For now, return a minimal stub that
        satisfies Hermes's contract."""
        return {"platform": self.platform_name, "chat_id": int(chat_id)}

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

    async def _handle_inbound(self, data: dict) -> None:
        """Translate a WS inbound_message payload into a Hermes MessageEvent
        and hand it to `self.handle_message`. Drops events for unknown
        assistants (e.g. a race where a pairing was just revoked but the WS
        hasn't caught up).

        Bridge-local slash commands (`/new`, `/retry`, `/status`) are
        intercepted here and handled adapter-side — they never reach the
        Hermes agent. Everything else flows through `handle_message`.
        """
        assistant_id = data.get("assistant_id")
        if assistant_id is None:
            log.debug("inbound missing assistant_id: %s", data)
            return
        route = self._state.get_route(assistant_id)
        if route is None:
            log.warning(
                "inbound for unknown assistant_id=%s — dropping", assistant_id,
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
        # Surface user-attached files into the agent's view of the message.
        # Hermes's MessageEvent only carries `text` to the agent's prompt —
        # there's no first-class files/attachments slot downstream — so the
        # only way the model learns about an inbound image or document is by
        # finding it in the text. Images become markdown image syntax for
        # vision-capable models; docs become labeled link lines.
        agent_visible_text = _format_inbound_files(event.text, event.files)
        # Persist the last-seen message id BEFORE dispatch, so even if
        # handle_message crashes we don't infinite-loop on restart.
        self._save_last_id(event.message_id)
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

    async def _handle_bridge_local(self, command: str, data: dict) -> None:
        """Handle a `/new`, `/retry`, `/status`, `/peers`, `/peer-status`,
        `/peer-send`, or `/peer-complete` slash command locally — no
        round-trip to Hermes. Posts an ack/result back to BGOS so the user
        sees a response."""
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
            # bridge-local check doesn't fire again).
            replay = {
                **data,
                "text": last,
                "message_type": "standard",
                "command_name": None,
                "command_args": None,
            }
            await self._handle_inbound(replay)
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
        elif command in ("peers", "peer-status", "peer-send", "peer-complete"):
            await self._handle_peer_bridge_local(command, data)

    # -------------------------------------------------------------------------
    # Peer (a2a) bridge-local handlers — see bgos-agent-capabilities.md §11
    # -------------------------------------------------------------------------

    async def _handle_peer_bridge_local(self, command: str, data: dict) -> None:
        """Dispatcher for the four peer slash commands. Each posts a
        markdown reply into the user's chat so the result is visible
        immediately. Errors are reported as `**Error:** <reason>` rather
        than raised — bridge-locals must never crash the adapter loop.
        """
        chat_id = data.get("chat_id")
        assistant_id = data.get("assistant_id")
        args = (data.get("command_args") or "").strip()
        if not isinstance(chat_id, int) or not isinstance(assistant_id, int):
            log.warning(
                "peer bridge-local missing chat_id/assistant_id: %s", data,
            )
            return

        try:
            if command == "peers":
                await self._peer_cmd_list(chat_id, assistant_id)
            elif command == "peer-status":
                await self._peer_cmd_status(chat_id, assistant_id, args)
            elif command == "peer-send":
                await self._peer_cmd_send(chat_id, assistant_id, args)
            elif command == "peer-complete":
                await self._peer_cmd_complete(chat_id, assistant_id, args)
        except Exception as err:  # noqa: BLE001 — bridge-locals must not crash
            log.exception("peer bridge-local %s failed", command)
            await self._api.post_message(
                chat_id=chat_id,
                text=f"**/{command} failed:** {err}",
                sender="assistant",
                message_type="standard",
            )

    async def _peer_cmd_list(self, chat_id: int, assistant_id: int) -> None:
        peers = await self._api.list_peers(caller_assistant_id=assistant_id)
        if not peers:
            await self._api.post_message(
                chat_id=chat_id,
                text="No peer assistants on this account.",
                sender="assistant",
                message_type="standard",
            )
            return
        lines = ["**Peer assistants:**", ""]
        for p in peers:
            mark = "✓" if p.get("introduced") else "✗"
            name = p.get("name") or f"#{p.get('assistantId')}"
            lines.append(
                f"- {mark} **{name}** (id `{p.get('assistantId')}`)",
            )
        lines.append("")
        lines.append(
            "_✓ = user has enabled this direction. ✗ = needs intro from the_"
            "_BGOS Agent Permissions matrix before send works._",
        )
        await self._api.post_message(
            chat_id=chat_id,
            text="\n".join(lines),
            sender="assistant",
            message_type="standard",
        )

    async def _peer_cmd_status(
        self, chat_id: int, assistant_id: int, args: str,
    ) -> None:
        peer_id = await self._resolve_peer_arg(assistant_id, args)
        if peer_id is None:
            await self._api.post_message(
                chat_id=chat_id,
                text=(
                    "Usage: `/peer-status <name|id>`. "
                    "Run `/peers` to see what's discoverable."
                ),
                sender="assistant",
                message_type="standard",
            )
            return
        result = await self._api.peer_status(
            caller_assistant_id=assistant_id, peer_assistant_id=peer_id,
        )
        online = "🟢 online" if result.get("online") else "⚪ offline"
        last_seen = result.get("lastSeenAt") or "never"
        has_open = "yes" if result.get("hasOpenConversation") else "no"
        await self._api.post_message(
            chat_id=chat_id,
            text=(
                f"**Peer #{peer_id} status:** {online}\n"
                f"- Last seen: {last_seen}\n"
                f"- Open conversation: {has_open}"
            ),
            sender="assistant",
            message_type="standard",
        )

    async def _peer_cmd_send(
        self, chat_id: int, assistant_id: int, args: str,
    ) -> None:
        # Parse `<name|id> <text>` plus optional `--wait` flag. Keep simple
        # to avoid a CLI dep — this is a bridge-local, the agent has more
        # ergonomic surfaces (markers + Python API).
        wait_for_reply = False
        if args.startswith("--wait "):
            wait_for_reply = True
            args = args[len("--wait "):].strip()
        elif args.endswith(" --wait"):
            wait_for_reply = True
            args = args[: -len(" --wait")].strip()
        if " " not in args:
            await self._api.post_message(
                chat_id=chat_id,
                text=(
                    "Usage: `/peer-send <name|id> <text>` "
                    "(append `--wait` to block on reply)."
                ),
                sender="assistant",
                message_type="standard",
            )
            return
        name_or_id, _, text = args.partition(" ")
        peer_id = await self._resolve_peer_arg(assistant_id, name_or_id.strip())
        if peer_id is None:
            await self._api.post_message(
                chat_id=chat_id,
                text=(
                    f"No peer matches `{name_or_id}`. "
                    f"Run `/peers` to see what's discoverable."
                ),
                sender="assistant",
                message_type="standard",
            )
            return
        # Anchor the SideConversationCard to the most recent assistant
        # message in this chat. If we have none yet, post a "Looping in"
        # message first so the card has something to attach to.
        parent_message_id = self._state.last_assistant_message_by_chat.get(chat_id)
        if parent_message_id is None:
            anchor = await self._api.post_message(
                chat_id=chat_id,
                text=f"Looping in peer #{peer_id}…",
                sender="assistant",
                message_type="standard",
            )
            parent_message_id = anchor.get("id") if isinstance(anchor, dict) else None
            if isinstance(parent_message_id, int):
                self._state.last_assistant_message_by_chat[chat_id] = parent_message_id
        if not isinstance(parent_message_id, int):
            await self._api.post_message(
                chat_id=chat_id,
                text="Could not anchor the side-thread — try sending a normal reply first.",
                sender="assistant",
                message_type="standard",
            )
            return
        result = await self._api.send_to_peer(
            caller_assistant_id=assistant_id,
            target_assistant_id=peer_id,
            text=text.strip(),
            parent_message_id=parent_message_id,
            wait_for_reply=wait_for_reply,
        )
        if result.get("status") == "requires_introduction":
            await self._api.post_message(
                chat_id=chat_id,
                text=(
                    f"**Cannot send to peer #{peer_id}** — the user has not "
                    f"enabled this direction in the Agent Permissions matrix. "
                    f"Open BGOS settings → Agent permissions to introduce them."
                ),
                sender="assistant",
                message_type="standard",
            )
            return
        # Track the open peer conversation so /peer-complete can find it.
        msg_id = result.get("messageId")
        conv_id = result.get("conversationId")
        if isinstance(conv_id, int):
            self._state.peer_conversation_by_chat[chat_id] = (peer_id, conv_id)
        reply = result.get("reply")
        ack = (
            f"Sent to peer #{peer_id} (message `{msg_id}`)."
            if not reply
            else f"Peer replied: {reply.get('text', '<no text>')}"
        )
        await self._api.post_message(
            chat_id=chat_id,
            text=ack,
            sender="assistant",
            message_type="standard",
        )

    async def _peer_cmd_complete(
        self, chat_id: int, assistant_id: int, args: str,
    ) -> None:
        tracked = self._state.peer_conversation_by_chat.get(chat_id)
        if tracked is None:
            await self._api.post_message(
                chat_id=chat_id,
                text=(
                    "No open peer conversation in this chat. "
                    "Send to a peer first via `/peer-send`."
                ),
                sender="assistant",
                message_type="standard",
            )
            return
        peer_id, _conv_id = tracked
        summary = args.strip() if args else None
        result = await self._api.complete_peer_thread(
            caller_assistant_id=assistant_id,
            peer_assistant_id=peer_id,
            summary=summary,
        )
        self._state.peer_conversation_by_chat.pop(chat_id, None)
        await self._api.post_message(
            chat_id=chat_id,
            text=(
                f"Closed conversation with peer #{peer_id}"
                + (f": {summary}" if summary else ".")
            ),
            sender="assistant",
            message_type="standard",
        )
        return result  # for tests

    async def _resolve_peer_arg(
        self, caller_assistant_id: int, arg: str,
    ) -> int | None:
        """Resolve `<name|id>` to an assistantId via the live peer list. Case-
        insensitive name match. Returns None when no peer matches.
        """
        if not arg:
            return None
        if arg.isdigit():
            return int(arg)
        try:
            peers = await self._api.list_peers(
                caller_assistant_id=caller_assistant_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("list_peers failed during resolve")
            return None
        target = arg.lower()
        for p in peers:
            if (p.get("name") or "").lower() == target:
                pid = p.get("assistantId")
                if isinstance(pid, int):
                    return pid
        return None

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

    async def _handle_callback(self, data: dict) -> None:
        """Route a callback_result WS event.

        `ea:{choice}:{approval_id}` → look up session_key in self._approval_state,
        call `resolve_gateway_approval(session_key, choice)` from tools.approval
        (synchronous, thread-safe — safe from this async handler).

        Anything else → defer to `self.handle_button_press(data)` so the
        Hermes agent sees the press naturally. If no such handler exists,
        log and drop.

        Stale approval clicks (approval_id missing from _approval_state —
        e.g. timed out, already resolved, or the adapter restarted) log and
        no-op. Telegram answers these via `"already resolved"`; BGOS's
        equivalent would need a separate API call and isn't worth the
        complexity for Phase 1.
        """
        cb = data.get("callback_data", "")
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
                await self._handle_inbound(msg)
            except Exception:
                log.exception("backfill replay failed for message=%s", msg)
