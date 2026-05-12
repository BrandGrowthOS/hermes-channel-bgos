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

from .bgos_api import BgosApi, BgosApiError
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


try:  # pragma: no cover - exercised only when Hermes is installed
    # Phase 1 stub; replace import target when Hermes ships its slash_confirm
    # helper. Telegram has its own override at gateway/platforms/telegram.py:2119
    # but the fork hasn't surfaced a shared resolver path yet — tests monkeypatch
    # this at the module level (hermes_channel_bgos.bgos_adapter.resolve_slash_confirm).
    from gateway.slash_confirm import resolve as resolve_slash_confirm  # type: ignore
except ImportError:
    def resolve_slash_confirm(session_key: str, confirm_id: str, choice: str) -> None:  # type: ignore[no-redef]
        raise RuntimeError(
            "gateway.slash_confirm.resolve is not importable — "
            "Hermes not installed. Tests should monkeypatch this at the "
            "module level (hermes_channel_bgos.bgos_adapter)."
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

# Backend rejects inline messages with >6 options (see PR #62 + memory
# `inline_buttons_shipped.md`). Agents that emit more get truncated; we log
# a warning so this isn't silent.
_INLINE_OPTION_LIMIT = 6

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
        return _send_result(message_id=message_id)

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
        cleaned_text, options, render_mode = _parse_buttons_block(content)
        chat_key = int(chat_id)
        mid_int = int(message_id)
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
            result = await self._do_patch(mid_int, cleaned_text, options, render_mode)
            self._last_edit_at[chat_key] = now
            return result

        # Within throttle window — stash latest content (coalesces with
        # any earlier-stashed write for the same chat), schedule deferred
        # flush if one isn't already pending.
        self._pending_edit_content[chat_key] = (
            mid_int, cleaned_text, options, render_mode,
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
        mid_int, text, options, render_mode = pending
        try:
            await self._do_patch(mid_int, text, options, render_mode)
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

        Returns False on any HTTP error (404 = already deleted; 501 =
        backend doesn't implement DELETE yet) so the caller falls back to
        leaving the message visible. Re-raises 5xx other than 501 so real
        backend incidents surface.
        """
        try:
            await self._api.delete_message(int(message_id))
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
        cb = data.get("callback_data", "")
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
            if isinstance(msg_id, int):
                try:
                    await self._api.patch_message(msg_id, text=text, options=[])
                except Exception:
                    log.warning("slash-confirm message edit failed", exc_info=True)
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
            if isinstance(msg_id, int):
                try:
                    await self._api.patch_message(msg_id, text=text, options=[])
                except Exception:
                    # Cosmetic — approval already resolved by the time we get
                    # here. Swallow (e.g. message was deleted) and log.
                    log.warning(
                        "approval message edit failed message_id=%d",
                        msg_id, exc_info=True,
                    )
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
