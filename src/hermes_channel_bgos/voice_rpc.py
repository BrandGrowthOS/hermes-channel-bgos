"""voice_rpc frame handling — the Hermes-adapter side of BGOS's native
in-app WebRTC voice control plane (spec §6.2, the "Hermes broker").

The BGOS backend pushes `voice_rpc {rpcId, op, assistantId, agentRoute,
chatId, payload}` frames into the `pairing:<id>` room this adapter's
Socket.IO client already joined. We ACK immediately (suppresses the
backend's 1.5 s retry re-emit), run the op, and POST the outcome back
with the pairing token:

    POST /api/v1/integrations/voice-rpc/:rpcId/result
        {ok:true, payload} | {ok:false, error:{code, message}}

Ops (all whitelisted in `normalize_voice_rpc` — the OpenClaw G2 lesson:
every wire shape gets an explicit outcome, never silence):

    mint     → POST https://api.openai.com/v1/realtime/client_secrets
               directly (key from BGOS_OPENAI_API_KEY / OPENAI_API_KEY in
               the gateway env). We own the mint, so the agent persona +
               recent chat context are baked into the session
               `instructions` → contextInjected:true (the app then skips
               client-side injection). The session bakes EXACTLY the
               hermes_agent_consult tool — the app only registers its
               client-side dispatch/roundtable tools when the mint
               returned ≥1 baked tool (verified frontend gotcha). 8 s
               inner cap, under the backend's 10 s mint deadline so our
               descriptive error always beats the generic timeout.
    consult  → run a real agent turn through the adapter's normal message
               pipeline on the SAME session as the BGOS text chat
               (`bgos:<chat_id>`), so the agent's next text turn remembers
               the call. The turn's reply is captured off the adapter's
               outbound send/edit path and returned as `{text}`. 38 s
               inner cap < the backend's 45 s.
    dispatch → ACCEPT fast (the backend only waits 10 s for the accept),
               then run the same brain-turn machinery DETACHED with a
               10-minute cap and report the outcome to
               POST /api/v1/integrations/voice-tasks/:taskId/result
               (retried once — a failed POST must never masquerade as a
               failed RUN).

Deadline discipline (ported from openclaw-channel-bgos/voice-rpc-handler
and bgos-claude-plugin/lib/voice-rpc): the daemon's inner cap must stay
UNDER the backend's, because the backend drops results that arrive after
its own timeout — a descriptive error that arrives in time always beats
a better answer that arrives late.

Known v1 limitation (documented in docs/bgos-agent-capabilities.md §11):
reply capture is per-chat, so when a long detached dispatch and a consult
overlap on the SAME chat, both resolve off the same turn-chain end and
the dispatch outcome text can echo the consult answer. The chat itself
always holds the ground truth (replies are never suppressed).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)


# The SDP-exchange endpoint for a direct-OpenAI mint (wire contract).
OFFER_URL = "https://api.openai.com/v1/realtime/calls"
CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

# Must not collide with the app's client-registered tool names
# (agent_dispatch / get_task_status / check_agent_status / roundtable_*):
# the app's tool router relays every OTHER name to the consult endpoint.
CONSULT_TOOL_NAME = "hermes_agent_consult"

_VALID_OPS = ("mint", "consult", "dispatch")


@dataclass(frozen=True)
class VoiceRpcFrame:
    rpc_id: str
    op: str
    assistant_id: int | str
    agent_route: str
    chat_id: int | str | None
    payload: dict[str, Any]


def normalize_voice_rpc(raw: Any) -> VoiceRpcFrame | None:
    """Validate a voice_rpc control frame. Backend emits camelCase:
    {rpcId, op, assistantId, agentRoute, chatId, payload}. Ops are
    WHITELISTED — anything else is dropped here (the backend's own
    timeout surfaces the failure to the app, so silence for a malformed
    frame is safe; a well-formed frame with an op we don't serve gets a
    descriptive error in VoiceRpcHandler.handle instead). Port of the
    OpenClaw bgos-ws.ts normalizer.
    """
    if not isinstance(raw, dict):
        return None
    rpc_id = raw.get("rpcId")
    op = raw.get("op")
    if not isinstance(rpc_id, str) or not rpc_id:
        return None
    if op not in _VALID_OPS:
        return None
    assistant_id = raw.get("assistantId")
    if not isinstance(assistant_id, (int, str)):
        assistant_id = ""
    agent_route = raw.get("agentRoute")
    if not isinstance(agent_route, str):
        agent_route = ""
    chat_id = raw.get("chatId")
    if not isinstance(chat_id, (int, str)):
        chat_id = None
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return VoiceRpcFrame(
        rpc_id=rpc_id,
        op=op,
        assistant_id=assistant_id,
        agent_route=agent_route,
        chat_id=chat_id,
        payload=payload,
    )


# Envelope fields that are safe to echo into a log line. `payload` is NOT
# among them: a mint payload carries the caller's raw OpenAI key
# (payload.openaiApiKey), and normalize_voice_rpc() rejects a frame on a bad
# rpcId/op BEFORE it ever inspects payload, so the drop path would otherwise
# serialize a live credential straight into the gateway log.
_LOGGABLE_FRAME_FIELDS = ("rpcId", "op", "assistantId", "agentRoute", "chatId")


def redact_voice_rpc_for_log(raw: Any) -> str:
    """A log-safe rendering of a raw voice_rpc frame.

    Allowlist, not denylist: payload VALUES are never emitted, only the sorted
    payload key NAMES, which is what a dropped-frame diagnosis actually needs.
    A denylist of secret-looking names would silently leak the next field
    somebody adds to the payload.
    """
    if not isinstance(raw, dict):
        return f"<non-dict {type(raw).__name__}>"
    safe: dict[str, Any] = {
        field: raw[field] for field in _LOGGABLE_FRAME_FIELDS if field in raw
    }
    payload = raw.get("payload")
    if isinstance(payload, dict):
        safe["payloadKeys"] = sorted(str(key) for key in payload)
    elif payload is not None:
        safe["payloadKeys"] = f"<non-dict {type(payload).__name__}>"
    return repr(safe)


class VoiceRpcError(Exception):
    """An op failure with a machine-readable code + speakable message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VoiceRpcTimeout(VoiceRpcError):
    """Raised by the adapter's run_brain_turn when the turn blows its
    deadline. Mapped to op-specific descriptive errors by the handler."""

    def __init__(self, message: str = "agent turn timed out") -> None:
        super().__init__("BRAIN_TIMEOUT", message)


@dataclass(frozen=True)
class VoiceRpcTiming:
    """Inner-deadline discipline. Every cap stays UNDER the backend's
    corresponding deadline so our descriptive error wins the race
    (backend: mint 10 s, consult 45 s, dispatch-accept 10 s, dispatch
    reaper 15 min)."""

    mint_timeout: float = 8.0
    consult_timeout: float = 38.0
    dispatch_run_timeout: float = 600.0
    dispatch_result_retry_delay: float = 3.0


@dataclass(frozen=True)
class VoiceConfig:
    """Mint-time session configuration, resolved from the gateway env by
    the adapter (see BGOSAdapter._voice_config)."""

    openai_api_key: str
    model: str
    voice: str
    persona: str
    agent_name: str
    # Confirm gate belt (Iris G5): reject dispatch frames lacking
    # confirmed:true. Default off; the backend-side gate is the primary
    # enforcement and now sends confirmed:true on every forwarded dispatch.
    require_confirmed_dispatch: bool = False


def load_voice_env() -> tuple[str, str, str]:
    """(api_key, model, voice) from the gateway process env. The key is
    the ONE deployment requirement — no key ⇒ mint replies with a clear
    "voice not configured on this host" error."""
    key = (
        os.environ.get("BGOS_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    model = (os.environ.get("BGOS_VOICE_MODEL") or "gpt-realtime-2.1").strip()
    voice = (os.environ.get("BGOS_VOICE_VOICE") or "marin").strip()
    return key, model, voice


def load_require_confirmed_dispatch() -> bool:
    """The BGOS_REQUIRE_CONFIRMED_DISPATCH belt (Iris G5). Exact-string
    'true' like the other cross-plugin boolean gates; default off so an
    unset env preserves current accept-all behavior."""
    return (os.environ.get("BGOS_REQUIRE_CONFIRMED_DISPATCH") or "") == "true"


def load_persona() -> str:
    """Voice persona text: explicit BGOS_VOICE_PERSONA env wins; otherwise
    the head of the agent's own SOUL.md ($HERMES_HOME/SOUL.md — the Hermes
    persona artifact), truncated so mint instructions stay bounded."""
    explicit = (os.environ.get("BGOS_VOICE_PERSONA") or "").strip()
    if explicit:
        return explicit
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    soul = hermes_home / "SOUL.md"
    try:
        if soul.is_file():
            return soul.read_text(encoding="utf-8", errors="replace")[:4000].strip()
    except OSError:
        pass
    return ""


# ONE shared total-instructions budget (Iris G4): persona + recent context +
# the owner memory head fit in ~14k chars. When over, memory is trimmed FIRST
# (then recent context); the fixed voice contract is never trimmed.
AGGREGATE_INSTRUCTIONS_BUDGET = 14_000
# Per-source cap on the owner memory head before the aggregate trim.
VOICE_MEMORY_MAX = 8_000
# Hermes home memory files read into the voice memory head, in order.
_VOICE_MEMORY_FILES = ("USER.md", "MEMORY.md")


def load_voice_memory() -> str:
    """Owner memory head (Iris G4): the head of the agent's own USER.md +
    MEMORY.md ($HERMES_HOME), or an explicit BGOS_VOICE_MEMORY_FILE. Owner-only
    by construction (the backend refuses non-owner mints). Best-effort: a
    missing file contributes nothing, so a home with no memory is
    byte-identical to the pre-feature mint. Set BGOS_VOICE_MEMORY=off to
    disable entirely."""
    if (os.environ.get("BGOS_VOICE_MEMORY") or "").strip().lower() == "off":
        return ""
    explicit = (os.environ.get("BGOS_VOICE_MEMORY_FILE") or "").strip()
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    paths = (
        [Path(explicit)]
        if explicit
        else [hermes_home / name for name in _VOICE_MEMORY_FILES]
    )
    chunks: list[str] = []
    for path in paths:
        try:
            if path.is_file():
                body = path.read_text(encoding="utf-8", errors="replace").strip()
                if body:
                    chunks.append(body)
        except OSError:
            continue
        if len("\n\n".join(chunks)) >= VOICE_MEMORY_MAX:
            break
    return "\n\n".join(chunks)[:VOICE_MEMORY_MAX]


# Per-assistant voice settings from the app (BGOS assistant voice menu),
# riding the mint frame as payload.voiceConfig. Everything optional — the
# host env config (BGOS_VOICE_VOICE / BGOS_VOICE_PERSONA / SOUL.md) is the
# fallback ONLY. Bounds mirror the backend coercion
# (backend/src/services/voice-settings.ts) and OpenAI's GA limits:
# speed 0.25–1.5 (session.audio.output.speed).
VOICE_SPEED_MIN = 0.25
VOICE_SPEED_MAX = 1.5
VOICE_INSTRUCTIONS_MAX = 2000
_VOICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)


def normalize_voice_config(raw: Any) -> dict[str, Any]:
    """Sanitize payload.voiceConfig from the wire. Defensive twin of the
    backend's buildMintVoiceConfig — the backend already coerces, but the
    daemon must never trust the wire (junk voice → dropped, out-of-range
    speed → clamped, oversized instructions → capped). Returns {} when
    nothing usable is present."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    voice = raw.get("voice")
    if isinstance(voice, str) and _VOICE_ID_RE.match(voice.strip()):
        out["voice"] = voice.strip().lower()
    speed = raw.get("speed")
    if isinstance(speed, str):
        try:
            speed = float(speed)
        except ValueError:
            speed = None
    if isinstance(speed, (int, float)) and speed == speed:  # NaN guard
        out["speed"] = round(
            min(VOICE_SPEED_MAX, max(VOICE_SPEED_MIN, float(speed))), 2
        )
    instructions = raw.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        out["instructions"] = instructions.strip()[:VOICE_INSTRUCTIONS_MAX]
    # Coerce defensively (never trust the wire): boolean True or string 'true'.
    if raw.get("requireDispatchConfirm") in (True, "true"):
        out["requireDispatchConfirm"] = True
    return out


def normalize_expires_at_seconds(value: Any) -> int | None:
    """The backend stores `new Date(Number(expiresAt) * 1000)` — the wire
    unit is epoch SECONDS. OpenAI's client_secrets returns seconds today,
    but normalize defensively (the OpenClaw lesson: providers have
    emitted both units historically)."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or value != value:  # NaN guard
        return None
    n = float(value)
    # ~2286-11-20 in epoch seconds; anything bigger is epoch milliseconds.
    return int(n / 1000) if n > 10_000_000_000 else int(n)


# Continuation brief (Iris 514): consult/dispatch turns land in the agent's
# OWN session, which may already hold earlier voice-run results. Telling the
# brain so makes repeat asks dramatically faster (reuse, don't redo).
CONTINUATION_BRIEF = (
    "This session may contain your earlier runs of similar work from this "
    "call. Reuse those results where they still apply and re-check only "
    "what changed instead of starting over."
)


def build_mint_instructions(
    *,
    agent_name: str,
    persona: str,
    recent_context: str,
    require_dispatch_confirm: bool = False,
    memory: str = "",
) -> str:
    """Realtime session instructions: persona + the dumb-mouth contract.
    Hermes turns are fast (typically 2–15 s), so hermes_agent_consult is
    the PRIMARY brain tool; long multi-step work is biased to
    agent_dispatch (registered client-side by the app)."""
    name = agent_name.strip() or "the agent"
    parts: list[str] = [
        f"You are {name}, speaking with your user on a live voice call.",
    ]
    if persona.strip():
        parts.append(persona.strip())
    parts.append(
        "Personality: warm, capable, concise. Answer in one to three short "
        "sentences unless asked for more. Never mention being an AI model "
        f'or a "realtime voice"; you ARE {name}.'
    )
    parts.append(
        "Welcome-back ceremony: open your FIRST greeting this call with a "
        "warm, brief welcome (greet the user by name if the recent "
        "conversation below reveals it), never a robotic identical hello. If "
        "the recent conversation shows you are resuming an earlier thread, "
        "skip the greeting ceremony and pick up naturally where you left "
        "off. Do not invent status you do not actually have."
    )
    parts.append(
        "You are the VOICE of the agent, not its brain. The real agent — a "
        "Hermes session with your user's full memory, files, and tools — is "
        "reachable through your tools:\n"
        "- Handle greetings, chit-chat, and anything answerable from this "
        "conversation DIRECTLY. No tools for small talk.\n"
        f"- Use {CONSULT_TOOL_NAME} for anything that needs the agent's real "
        "memory, knowledge, files, or tools. Verbally acknowledge first — "
        "it takes a few seconds — then relay the answer naturally.\n"
        "- For long-running or multi-step work (research, builds, anything "
        "that changes state and takes minutes), PREFER agent_dispatch: "
        "verbally acknowledge what you are kicking off, dispatch it, and "
        "the result is announced when ready.\n"
        "- If a consult fails or times out, say the agent is still working "
        "on it and will follow up in the chat — never leave silence.\n"
        "- Speak results naturally; keep technical detail light unless asked."
    )
    parts.append(
        "Truthfulness contract: NEVER invent, guess, or embellish the "
        "results of the agent's work. Only report an outcome you actually "
        "received from a tool result or an announcement on this call. If "
        "you do not have the result yet, say the work is still in progress "
        "and check its status before speaking about it."
    )
    parts.append(
        "When you consult or dispatch, phrase the brief as the user's "
        "intent and desired outcome, in their own words. Never include "
        "mechanics from earlier runs (tool names, file paths, step-by-step "
        "how-to); the agent owns its tools and stale mechanics mislead it."
    )
    if require_dispatch_confirm:
        parts.append(
            "Dispatch confirmation is ON for this agent: agent_dispatch "
            "STAGES a proposal instead of starting work. Read the staged "
            "brief back to the user in one short sentence and ask for their "
            "go-ahead. Only after the user clearly confirms on their next "
            "turn, call confirm_dispatch with the task id from the ack. If "
            "they decline, call confirm_dispatch with approve:false. Never "
            "invent a confirmation the user did not give."
        )
    # Owner memory head + recent conversation share the remaining aggregate
    # budget (G4), memory trimmed FIRST so the live conversation wins.
    core = "\n\n".join(parts)
    mem_label = "Owner memory (profile, active projects, shorthand):\n"
    ctx_label = "Recent conversation with your user (for continuity):\n"
    mem = (memory or "").strip()[:VOICE_MEMORY_MAX]
    # recent_context is built most-recent-LAST, so keep its TAIL when trimming.
    ctx = recent_context.strip()[-20_000:]
    sep = 2  # len("\n\n")
    # Safe default: with NO memory head, leave recent context at its pre-feature
    # 20k slice and skip the aggregate trim, so a memory-less agent mints
    # byte-identically to before this feature.
    if mem:
        core_cost = len(core)
        ctx_block_cost = sep + len(ctx_label) + len(ctx) if ctx else 0
        if core_cost + ctx_block_cost > AGGREGATE_INSTRUCTIONS_BUDGET:
            mem = ""
            if ctx:
                room = (
                    AGGREGATE_INSTRUCTIONS_BUDGET - core_cost - sep - len(ctx_label)
                )
                ctx = ctx[-room:] if room > 0 else ""
        else:
            mem_room = (
                AGGREGATE_INSTRUCTIONS_BUDGET
                - core_cost
                - ctx_block_cost
                - sep
                - len(mem_label)
            )
            mem = mem[:mem_room] if mem_room > 0 else ""
    out = [core]
    if mem:
        out.append(mem_label + mem)
    if ctx:
        out.append(ctx_label + ctx)
    return "\n\n".join(out)


def build_consult_tool_definition() -> dict[str, Any]:
    """The consult tool baked into the realtime session at mint. Mirrors
    the backend's VoiceToolCallDto args shape. The mint MUST bake ≥1 tool:
    the app only registers its client-side dispatch/roundtable tools when
    the daemon-baked tools array is non-empty."""
    return {
        "type": "function",
        "name": CONSULT_TOOL_NAME,
        "description": (
            "Ask the agent's real brain (the live Hermes session) anything "
            "needing its memory, knowledge, files, or tools. Takes several "
            "seconds; verbally acknowledge first. For long multi-step work "
            "use agent_dispatch instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, self-contained and specific.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional call context that helps answer.",
                },
                "responseStyle": {
                    "type": "string",
                    "description": 'Optional style hint, e.g. "one sentence".',
                },
            },
            "required": ["question"],
        },
    }


def build_consult_turn_text(
    *, question: str, context: str, response_style: str
) -> str:
    """The `[voice consult]` message dispatched into the agent's own
    session (spec §6.2: consult messages are prefixed so the agent knows
    the modality). The reply is captured off the outbound path AND lands
    in the chat as a normal message — nothing is ever lost."""
    parts = [
        "[voice consult] Your user is asking you LIVE on a voice call:\n"
        f'"{question}"'
    ]
    if context:
        parts.append(f"Call context: {context}")
    if response_style:
        parts.append(f"Answer style: {response_style}")
    parts.append(CONTINUATION_BRIEF)
    parts.append(
        "Your reply will be SPOKEN to them on the call. Answer in 1-3 short, "
        "speakable sentences — plain prose, no markdown, no headers, no code "
        "blocks, no links. Do NOT use tools unless the question strictly "
        "requires them; you have ~30 seconds."
    )
    return "\n\n".join(parts)


def build_dispatch_turn_text(*, question: str, context: str, task_id: str) -> str:
    """The `[voice dispatch]` message for an async task kicked off from a
    live call. The run is detached — the call is NOT waiting on it — so
    the agent can take its time; the outcome summary is announced on the
    call (if still open) and posted to the user's Work Stream."""
    parts = [
        "[voice dispatch] While on a voice call, your user asked you to do "
        f"this task (voice task {task_id}):\n"
        f'"{question}"'
    ]
    if context:
        parts.append(f"Call context: {context}")
    parts.append(CONTINUATION_BRIEF)
    parts.append(
        "Do the work now. When you are done, reply with a short spoken-style "
        "summary of the outcome (1-6 sentences, plain prose — it will be "
        "read aloud to your user and posted to their Work Stream)."
    )
    return "\n\n".join(parts)


# ── handler ─────────────────────────────────────────────────────────────────


@dataclass
class VoiceRpcDeps:
    """Injected adapter surface. Keeps this module unit-testable without a
    BGOSAdapter (mirrors the DI design of bgos-claude-plugin/lib/voice-rpc).

    post_ack / post_result hit the pairing-authed voice-rpc REST routes;
    post_voice_task_result hits the voice-tasks result route (dual-auth on
    the backend; we use the pairing lane). run_brain_turn dispatches one
    real agent turn through the adapter's message pipeline and returns the
    reply text (raises VoiceRpcError on timeout/failure). voice_config
    resolves the mint config for one assistant.
    """

    post_ack: Callable[[str], Awaitable[Any]]
    post_result: Callable[[str, dict], Awaitable[Any]]
    post_voice_task_result: Callable[[str, dict], Awaitable[Any]]
    run_brain_turn: Callable[[int | str, str, float], Awaitable[str]]
    voice_config: Callable[[int | str], VoiceConfig]
    # Injectable HTTP POST for tests: (url, headers, json, timeout) →
    # (status_code, decoded_json_or_text). Defaults to httpx.
    http_post_json: (
        Callable[[str, dict, dict, float], Awaitable[tuple[int, Any]]] | None
    ) = None
    timing: VoiceRpcTiming = field(default_factory=VoiceRpcTiming)


async def _default_http_post_json(
    url: str, headers: dict, json_body: dict, timeout: float
) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=json_body)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text


class VoiceRpcHandler:
    """Runs voice_rpc frames against the local Hermes brain. One instance
    per adapter; all state is in-process (the backend's re-emit/timeout
    machinery is the durability layer)."""

    def __init__(self, deps: VoiceRpcDeps) -> None:
        self._deps = deps
        self._timing = deps.timing
        # Guards against duplicate frames for the same rpcId — the backend
        # re-emits once when its ACK doesn't land within 1.5 s, and a
        # consult dispatched twice would run two real agent turns.
        self._in_flight: set[str] = set()
        # Dedupe detached dispatch runs by taskId (a re-emitted frame
        # carries a new rpcId only on backend restart; the taskId is the
        # durable key).
        self._dispatch_in_flight: set[str] = set()

    async def handle(self, frame: VoiceRpcFrame) -> None:
        if not frame or not frame.rpc_id:
            return
        if frame.rpc_id in self._in_flight:
            log.info("voice_rpc duplicate frame ignored rpc=%s", frame.rpc_id)
            return
        self._in_flight.add(frame.rpc_id)
        try:
            # ACK is best-effort: a failed ACK only costs one retry-emit
            # (which the in-flight guard absorbs); it must not abort the op.
            try:
                await self._deps.post_ack(frame.rpc_id)
            except Exception as exc:
                log.warning(
                    "voice_rpc ack failed (non-fatal) rpc=%s: %s",
                    frame.rpc_id, exc,
                )

            if frame.op == "mint":
                payload = await self._mint(frame)
            elif frame.op == "consult":
                payload = await self._consult(frame)
            elif frame.op == "dispatch":
                # ACCEPT fast — the backend only waits 10 s for this result
                # — and only START the detached run once the accept has
                # actually been delivered, so a failed accept (the backend
                # flips the row to error + tells the user) never leaves a
                # ghost run executing real side effects behind a visible
                # failure. (OpenClaw G2 semantics.)
                task_id = frame.payload.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    raise VoiceRpcError(
                        "BAD_DISPATCH", "dispatch payload missing taskId"
                    )
                # Confirm gate belt (Iris G5): pre-accept, so the backend
                # gets an explicit descriptive rejection on the rpc result
                # route. Coerce defensively (True or 'true').
                cfg = self._deps.voice_config(frame.assistant_id)
                if cfg.require_confirmed_dispatch and frame.payload.get(
                    "confirmed"
                ) not in (True, "true"):
                    raise VoiceRpcError(
                        "DISPATCH_UNCONFIRMED",
                        f"unconfirmed dispatch rejected (task={task_id}): "
                        "BGOS_REQUIRE_CONFIRMED_DISPATCH is on and the "
                        "payload lacks confirmed:true",
                    )
                await self._post_result(
                    frame.rpc_id,
                    {"ok": True, "payload": {"accepted": True, "taskId": task_id}},
                )
                asyncio.get_running_loop().create_task(
                    self._run_dispatch(frame, task_id)
                )
                return
            else:  # pragma: no cover - normalizer whitelists; defensive
                raise VoiceRpcError(
                    "UNSUPPORTED_OP", f"unsupported voice_rpc op: {frame.op}"
                )
            await self._post_result(frame.rpc_id, {"ok": True, "payload": payload})
        except Exception as exc:
            code = exc.code if isinstance(exc, VoiceRpcError) else "ADAPTER_ERROR"
            await self._post_result(
                frame.rpc_id,
                {"ok": False, "error": {"code": code, "message": str(exc)}},
            )
        finally:
            self._in_flight.discard(frame.rpc_id)

    # ── mint ────────────────────────────────────────────────────────────────

    async def _mint(self, frame: VoiceRpcFrame) -> dict[str, Any]:
        cfg = self._deps.voice_config(frame.assistant_id)
        # Per-user key (BGOS billing/security): the BGOS backend rides the
        # CALLER's own OpenAI key on the mint frame (payload.openaiApiKey) so
        # the call spends THEIR credits, never this host's owner key. Prefer
        # it; the host env key (cfg.openai_api_key) is a fallback ONLY for a
        # standalone host not driven by the BGOS backend. The BGOS backend
        # refuses a mint for a user with no key of their own, so a BGOS-driven
        # mint always arrives with a user key here. The raw key stays
        # server-side (it arrived over the authed pairing WS room) and never
        # reaches the app.
        user_openai_api_key = frame.payload.get("openaiApiKey")
        if not isinstance(user_openai_api_key, str):
            user_openai_api_key = ""
        user_openai_api_key = user_openai_api_key.strip()
        openai_api_key = user_openai_api_key or cfg.openai_api_key
        if not openai_api_key:
            raise VoiceRpcError(
                "VOICE_NOT_CONFIGURED",
                "voice is not configured: the caller has not set an OpenAI "
                "API key in their Home of Agents settings, and this Hermes "
                "gateway has no BGOS_OPENAI_API_KEY fallback",
            )
        recent_context = frame.payload.get("recentContext")
        if not isinstance(recent_context, str):
            recent_context = ""
        # Per-assistant voice settings from the app (v0.16.0): voice + speed
        # + persona instructions override the host env / SOUL.md config; the
        # env is the fallback ONLY when the app sent nothing.
        voice_config = normalize_voice_config(frame.payload.get("voiceConfig"))
        voice = voice_config.get("voice") or cfg.voice
        persona = voice_config.get("instructions") or cfg.persona
        instructions = build_mint_instructions(
            agent_name=cfg.agent_name,
            persona=persona,
            recent_context=recent_context,
            require_dispatch_confirm=voice_config.get("requireDispatchConfirm")
            is True,
            # Owner memory head (G4): read the Hermes home memory files.
            # Owner-only by construction (backend refuses non-owner mints).
            memory=load_voice_memory(),
        )
        body = {
            "expires_after": {"anchor": "created_at", "seconds": 600},
            "session": {
                "type": "realtime",
                "model": cfg.model,
                "instructions": instructions,
                "tools": [build_consult_tool_definition()],
                "audio": {
                    # Input transcription is REQUIRED: the app builds the
                    # call transcript (posted back into the chat) from
                    # realtime transcription events. Server VAD gives
                    # natural turn-taking. (Both live-verified against the
                    # GA client_secrets contract, 2026-07-05.)
                    "input": {
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {"type": "server_vad"},
                    },
                    # Only carries speed when the app configured it —
                    # omitting preserves the exact pre-feature request
                    # shape (OpenAI default 1.0).
                    "output": {
                        "voice": voice,
                        **(
                            {"speed": voice_config["speed"]}
                            if "speed" in voice_config
                            else {}
                        ),
                    },
                },
            },
        }
        post = self._deps.http_post_json or _default_http_post_json
        try:
            status, data = await asyncio.wait_for(
                post(
                    CLIENT_SECRETS_URL,
                    {
                        "Authorization": f"Bearer {openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    body,
                    self._timing.mint_timeout,
                ),
                timeout=self._timing.mint_timeout,
            )
        except asyncio.TimeoutError:
            raise VoiceRpcError("MINT_FAILED", "OpenAI mint timed out") from None
        except VoiceRpcError:
            raise
        except Exception as exc:
            raise VoiceRpcError("MINT_FAILED", f"OpenAI mint failed: {exc}") from exc
        if status < 200 or status >= 300:
            raise VoiceRpcError(
                "MINT_FAILED",
                f"OpenAI client_secrets HTTP {status}: {str(data)[:200]}",
            )
        client_secret = data.get("value") if isinstance(data, dict) else None
        if not isinstance(client_secret, str) or not client_secret:
            raise VoiceRpcError(
                "MINT_FAILED", "OpenAI client_secrets returned no secret value"
            )
        expires_at = normalize_expires_at_seconds(
            data.get("expires_at") if isinstance(data, dict) else None
        )
        return {
            "provider": "openai",
            "transport": "webrtc",
            "clientSecret": client_secret,
            "offerUrl": OFFER_URL,
            "model": cfg.model,
            # Echo what was APPLIED (app settings win over env) — the app's
            # in-call gear shows this as the active voice.
            "voice": voice,
            **({"speed": voice_config["speed"]} if "speed" in voice_config else {}),
            "expiresAt": expires_at
            if expires_at is not None
            else int(time.time()) + 600,
            # Context + persona ride the session instructions above — the
            # app must NOT inject recentContext again client-side.
            "contextInjected": True,
        }

    # ── consult ─────────────────────────────────────────────────────────────

    async def _consult(self, frame: VoiceRpcFrame) -> dict[str, Any]:
        payload = frame.payload
        call_id = payload.get("callId")
        name = payload.get("name")
        if not call_id or not name:
            raise VoiceRpcError("BAD_CONSULT", "consult payload missing callId/name")
        args = payload.get("args")
        args = args if isinstance(args, dict) else {}
        question = args.get("question")
        question = question.strip() if isinstance(question, str) else ""
        if not question:
            raise VoiceRpcError("BAD_CONSULT", "consult args missing question")
        context = args.get("context")
        context = context.strip() if isinstance(context, str) else ""
        response_style = args.get("responseStyle")
        response_style = (
            response_style.strip() if isinstance(response_style, str) else ""
        )
        if frame.chat_id is None:
            raise VoiceRpcError("BAD_CONSULT", "consult frame carries no chatId")

        cfg = self._deps.voice_config(frame.assistant_id)
        turn_text = build_consult_turn_text(
            question=question, context=context, response_style=response_style
        )
        try:
            # Belt + braces: run_brain_turn enforces the same deadline
            # internally; wait_for guarantees the wall-clock cap even if a
            # deps implementation misbehaves.
            text = await asyncio.wait_for(
                self._deps.run_brain_turn(
                    frame.chat_id, turn_text, self._timing.consult_timeout
                ),
                timeout=self._timing.consult_timeout,
            )
        except (asyncio.TimeoutError, VoiceRpcTimeout):
            raise VoiceRpcError(
                "CONSULT_TIMEOUT",
                f"{cfg.agent_name} is still working on it — the answer "
                "will arrive in the chat.",
            ) from None
        text = (text or "").strip()
        if not text:
            raise VoiceRpcError(
                "EMPTY_REPLY",
                f"{cfg.agent_name} finished the turn without a speakable reply",
            )
        return {"text": text}

    # ── dispatch (detached) ─────────────────────────────────────────────────

    async def _run_dispatch(self, frame: VoiceRpcFrame, task_id: str) -> None:
        if task_id in self._dispatch_in_flight:
            log.info("voice dispatch duplicate ignored task=%s", task_id)
            return
        self._dispatch_in_flight.add(task_id)
        try:
            # Phase 1 — the run. Its failure is a genuine DISPATCH_FAILED.
            result_payload: dict[str, Any] | None = None
            run_message = ""
            try:
                args = frame.payload.get("args")
                args = args if isinstance(args, dict) else {}
                question = args.get("question")
                question = question.strip() if isinstance(question, str) else ""
                if not question:
                    raise VoiceRpcError(
                        "BAD_DISPATCH", "dispatch args missing question"
                    )
                context = args.get("context")
                context = context.strip() if isinstance(context, str) else ""
                if frame.chat_id is None:
                    raise VoiceRpcError(
                        "BAD_DISPATCH", "dispatch frame carries no chatId"
                    )
                turn_text = build_dispatch_turn_text(
                    question=question, context=context, task_id=task_id
                )
                text = await asyncio.wait_for(
                    self._deps.run_brain_turn(
                        frame.chat_id,
                        turn_text,
                        self._timing.dispatch_run_timeout,
                    ),
                    timeout=self._timing.dispatch_run_timeout,
                )
                text = (text or "").strip()
                if not text:
                    raise VoiceRpcError(
                        "EMPTY_REPLY", "the task run produced no summary"
                    )
                result_payload = {"text": text}
            except Exception as exc:
                run_message = str(exc) or exc.__class__.__name__
            # Phase 2 — the report. A failed POST must never masquerade as
            # a failed RUN (that would discard a genuine result forever,
            # since the backend row only flips once); retry once, then give
            # up loudly and let the backend's stale-running reaper close
            # the row.
            body: dict[str, Any] = (
                {"ok": True, "payload": result_payload}
                if result_payload is not None
                else {
                    "ok": False,
                    "error": {"code": "DISPATCH_FAILED", "message": run_message},
                }
            )
            try:
                await self._deps.post_voice_task_result(task_id, body)
            except Exception:
                await asyncio.sleep(self._timing.dispatch_result_retry_delay)
                try:
                    await self._deps.post_voice_task_result(task_id, body)
                except Exception as post_exc:
                    log.error(
                        "voice dispatch result post failed twice — giving up "
                        "task=%s: %s", task_id, post_exc,
                    )
                    return
            log.info(
                "voice dispatch %s task=%s",
                "completed" if result_payload is not None else "failed",
                task_id,
            )
        finally:
            self._dispatch_in_flight.discard(task_id)

    # ── result post ─────────────────────────────────────────────────────────

    async def _post_result(self, rpc_id: str, body: dict[str, Any]) -> None:
        try:
            await self._deps.post_result(rpc_id, body)
        except Exception as exc:
            # Nothing else we can do — the backend's own timeout surfaces
            # the failure to the app.
            log.error("voice_rpc result post failed rpc=%s: %s", rpc_id, exc)
