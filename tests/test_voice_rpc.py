"""Unit tests for voice_rpc.py — frame normalization (op whitelist,
the OpenClaw G2 pass-through regression), mint payload mapping, consult
deadline/error mapping, dispatch accept-first + detached-run semantics,
and rpcId/taskId dedupe. The handler is exercised against fake deps;
adapter integration lives in test_voice_adapter.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from hermes_channel_bgos.voice_rpc import (
    CLIENT_SECRETS_URL,
    CONSULT_TOOL_NAME,
    CONTINUATION_BRIEF,
    OFFER_URL,
    VoiceConfig,
    VoiceRpcDeps,
    VoiceRpcError,
    VoiceRpcFrame,
    VoiceRpcHandler,
    VoiceRpcTimeout,
    VoiceRpcTiming,
    build_consult_tool_definition,
    build_consult_turn_text,
    build_dispatch_turn_text,
    build_mint_instructions,
    load_voice_env,
    normalize_expires_at_seconds,
    normalize_voice_rpc,
    normalize_voice_config,
)

pytestmark = pytest.mark.asyncio


# ── fixtures ────────────────────────────────────────────────────────────────


@dataclass
class FakeDeps:
    """Recording fake for VoiceRpcDeps."""

    acks: list[str] = field(default_factory=list)
    results: list[tuple[str, dict]] = field(default_factory=list)
    task_results: list[tuple[str, dict]] = field(default_factory=list)
    brain_calls: list[tuple[Any, str, float]] = field(default_factory=list)
    brain_reply: str = "the answer"
    brain_error: Exception | None = None
    brain_delay: float = 0.0
    ack_error: Exception | None = None
    task_result_errors: list[Exception] = field(default_factory=list)
    openai_status: int = 200
    openai_body: Any = None
    openai_requests: list[tuple[str, dict, dict]] = field(default_factory=list)
    api_key: str = "sk-test-not-a-real-key"

    def to_deps(self, timing: VoiceRpcTiming | None = None) -> VoiceRpcDeps:
        async def post_ack(rpc_id: str) -> None:
            if self.ack_error is not None:
                raise self.ack_error
            self.acks.append(rpc_id)

        async def post_result(rpc_id: str, body: dict) -> None:
            self.results.append((rpc_id, body))

        async def post_voice_task_result(task_id: str, body: dict) -> None:
            if self.task_result_errors:
                raise self.task_result_errors.pop(0)
            self.task_results.append((task_id, body))

        async def run_brain_turn(chat_id, text: str, deadline: float) -> str:
            self.brain_calls.append((chat_id, text, deadline))
            if self.brain_delay:
                await asyncio.sleep(self.brain_delay)
            if self.brain_error is not None:
                raise self.brain_error
            return self.brain_reply

        def voice_config(assistant_id) -> VoiceConfig:
            return VoiceConfig(
                openai_api_key=self.api_key,
                model="gpt-realtime-2",
                voice="marin",
                persona="A calm strategist.",
                agent_name="Athena",
            )

        async def http_post_json(url, headers, json_body, timeout):
            self.openai_requests.append((url, headers, json_body))
            body = self.openai_body
            if body is None:
                body = {"value": "ek_test_secret", "expires_at": 1_800_000_000}
            return self.openai_status, body

        return VoiceRpcDeps(
            post_ack=post_ack,
            post_result=post_result,
            post_voice_task_result=post_voice_task_result,
            run_brain_turn=run_brain_turn,
            voice_config=voice_config,
            http_post_json=http_post_json,
            timing=timing or VoiceRpcTiming(),
        )


def frame(op: str, *, rpc_id: str = "rpc-1", payload: dict | None = None) -> VoiceRpcFrame:
    return VoiceRpcFrame(
        rpc_id=rpc_id,
        op=op,
        assistant_id=894,
        agent_route="default",
        chat_id=830,
        payload=payload if payload is not None else {},
    )


def consult_payload(**overrides) -> dict:
    base = {
        "callId": "call-1",
        "name": CONSULT_TOOL_NAME,
        "args": {"question": "What did we decide about the launch?"},
    }
    base.update(overrides)
    return base


# ── normalize_voice_rpc — the G2 pass-through regression ───────────────────


@pytest.mark.parametrize("op", ["mint", "consult", "dispatch"])
async def test_normalize_passes_through_every_supported_op(op: str) -> None:
    """Regression (OpenClaw G2 silent-drop lesson): each of the three wire
    ops MUST survive normalization — a whitelist edit that drops one op
    silently kills that capability with no visible error."""
    raw = {
        "rpcId": "rpc-42",
        "op": op,
        "assistantId": 894,
        "agentRoute": "default",
        "chatId": 830,
        "payload": {"recentContext": "hi"},
    }
    out = normalize_voice_rpc(raw)
    assert out is not None
    assert out.op == op
    assert out.rpc_id == "rpc-42"
    assert out.assistant_id == 894
    assert out.agent_route == "default"
    assert out.chat_id == 830
    assert out.payload == {"recentContext": "hi"}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "nope",
        {},
        {"op": "mint"},  # missing rpcId
        {"rpcId": "", "op": "mint"},  # empty rpcId
        {"rpcId": "r", "op": "shutdown"},  # non-whitelisted op
        {"rpcId": "r", "op": 5},
    ],
)
async def test_normalize_drops_malformed_frames(raw) -> None:
    assert normalize_voice_rpc(raw) is None


async def test_normalize_defaults_optional_fields() -> None:
    out = normalize_voice_rpc({"rpcId": "r", "op": "mint"})
    assert out is not None
    assert out.assistant_id == ""
    assert out.agent_route == ""
    assert out.chat_id is None
    assert out.payload == {}


async def test_normalize_rejects_non_dict_payload() -> None:
    out = normalize_voice_rpc({"rpcId": "r", "op": "mint", "payload": [1, 2]})
    assert out is not None
    assert out.payload == {}


# ── handler plumbing: ack, dedupe, result ───────────────────────────────────


async def test_handle_acks_then_posts_result() -> None:
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("consult", payload=consult_payload()))
    assert fake.acks == ["rpc-1"]
    assert len(fake.results) == 1
    rpc_id, body = fake.results[0]
    assert rpc_id == "rpc-1"
    assert body == {"ok": True, "payload": {"text": "the answer"}}


async def test_handle_dedupes_by_rpc_id_while_in_flight() -> None:
    """The backend re-emits once after 1.5 s without an ACK; a consult
    dispatched twice would run two real agent turns."""
    fake = FakeDeps(brain_delay=0.2)
    handler = VoiceRpcHandler(fake.to_deps())
    f = frame("consult", payload=consult_payload())
    await asyncio.gather(handler.handle(f), handler.handle(f))
    assert len(fake.brain_calls) == 1
    assert len(fake.results) == 1


async def test_ack_failure_is_non_fatal() -> None:
    fake = FakeDeps(ack_error=RuntimeError("backend hiccup"))
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("consult", payload=consult_payload()))
    assert fake.acks == []
    assert fake.results and fake.results[0][1]["ok"] is True


# ── mint ────────────────────────────────────────────────────────────────────


async def test_mint_maps_openai_response_to_wire_contract() -> None:
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(
        frame("mint", payload={"recentContext": "KC: hi\nYou: hello"})
    )
    rpc_id, body = fake.results[0]
    assert body["ok"] is True
    payload = body["payload"]
    assert payload["provider"] == "openai"
    assert payload["transport"] == "webrtc"
    assert payload["clientSecret"] == "ek_test_secret"
    assert payload["offerUrl"] == OFFER_URL
    assert payload["model"] == "gpt-realtime-2"
    assert payload["voice"] == "marin"
    assert payload["expiresAt"] == 1_800_000_000
    # We bake persona + context into the session instructions — the app
    # must skip client-side injection.
    assert payload["contextInjected"] is True

    url, headers, req = fake.openai_requests[0]
    assert url == CLIENT_SECRETS_URL
    assert headers["Authorization"] == "Bearer sk-test-not-a-real-key"
    session = req["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2"
    assert session["audio"]["output"]["voice"] == "marin"
    # Input transcription is REQUIRED (the app builds the transcript from
    # transcription events) and server VAD gives turn-taking.
    assert session["audio"]["input"]["transcription"]["model"]
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    # The mint MUST bake ≥1 tool: the app only registers dispatch/
    # roundtable client-side when the baked tools array is non-empty.
    assert [t["name"] for t in session["tools"]] == [CONSULT_TOOL_NAME]
    assert "Athena" in session["instructions"]
    assert "A calm strategist." in session["instructions"]
    assert "KC: hi" in session["instructions"]


def test_normalize_voice_config_sanitizes_the_wire() -> None:
    assert normalize_voice_config(None) == {}
    assert normalize_voice_config("cedar") == {}
    assert normalize_voice_config([]) == {}
    assert normalize_voice_config(
        {"voice": " Cedar ", "speed": 1.2, "instructions": " hi "}
    ) == {"voice": "cedar", "speed": 1.2, "instructions": "hi"}
    # Junk voice dropped, out-of-range speed clamped to OpenAI's 0.25–1.5.
    assert normalize_voice_config({"voice": "x; DROP", "speed": 99}) == {
        "speed": 1.5
    }
    assert normalize_voice_config({"speed": "0.01"}) == {"speed": 0.25}
    capped = normalize_voice_config({"instructions": "x" * 5000})
    assert len(capped["instructions"]) == 2000


async def test_mint_applies_voice_config_and_echoes_it() -> None:
    """Per-assistant settings (payload.voiceConfig) override the env config;
    the applied voice/speed are echoed so the in-call gear shows the truth."""
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(
        frame(
            "mint",
            payload={
                "recentContext": "KC: hi",
                "voiceConfig": {
                    "voice": "cedar",
                    "speed": 1.25,
                    "instructions": "Dry humor, two sentences max.",
                },
            },
        )
    )
    _, body = fake.results[0]
    assert body["ok"] is True
    assert body["payload"]["voice"] == "cedar"
    assert body["payload"]["speed"] == 1.25
    _, _, req = fake.openai_requests[0]
    session = req["session"]
    assert session["audio"]["output"]["voice"] == "cedar"
    assert session["audio"]["output"]["speed"] == 1.25
    # App persona REPLACES the env/SOUL persona (env is the fallback only).
    assert "Dry humor" in session["instructions"]
    assert "A calm strategist." not in session["instructions"]


async def test_mint_without_voice_config_keeps_pre_feature_shape() -> None:
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("mint", payload={"recentContext": ""}))
    _, body = fake.results[0]
    assert body["payload"]["voice"] == "marin"
    assert "speed" not in body["payload"]
    _, _, req = fake.openai_requests[0]
    assert req["session"]["audio"]["output"] == {"voice": "marin"}


async def test_mint_with_junk_voice_config_degrades_to_env() -> None:
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(
        frame(
            "mint",
            payload={"recentContext": "", "voiceConfig": {"voice": "!!", "speed": "junk"}},
        )
    )
    _, body = fake.results[0]
    assert body["ok"] is True
    _, _, req = fake.openai_requests[0]
    assert req["session"]["audio"]["output"] == {"voice": "marin"}


async def test_mint_without_api_key_is_descriptive_not_silent() -> None:
    fake = FakeDeps(api_key="")
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("mint"))
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "VOICE_NOT_CONFIGURED"
    assert "BGOS_OPENAI_API_KEY" in body["error"]["message"]
    assert fake.openai_requests == []


async def test_mint_maps_openai_http_error() -> None:
    fake = FakeDeps(openai_status=401, openai_body={"error": "bad key"})
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("mint"))
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "MINT_FAILED"
    assert "401" in body["error"]["message"]


async def test_mint_rejects_missing_secret_value() -> None:
    fake = FakeDeps(openai_body={"expires_at": 1_800_000_000})
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("mint"))
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "MINT_FAILED"
    assert "no secret" in body["error"]["message"]


async def test_mint_normalizes_millisecond_expiry_to_seconds() -> None:
    """The backend stores new Date(expiresAt * 1000): the wire unit is
    epoch SECONDS. Providers have emitted both units historically."""
    fake = FakeDeps(
        openai_body={"value": "ek_x", "expires_at": 1_800_000_000_000}
    )
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("mint"))
    _, body = fake.results[0]
    assert body["payload"]["expiresAt"] == 1_800_000_000


async def test_normalize_expires_at_units() -> None:
    assert normalize_expires_at_seconds(1_800_000_000) == 1_800_000_000
    assert normalize_expires_at_seconds(1_800_000_000_000) == 1_800_000_000
    assert normalize_expires_at_seconds("1800000000") == 1_800_000_000
    assert normalize_expires_at_seconds("soon") is None
    assert normalize_expires_at_seconds(None) is None
    assert normalize_expires_at_seconds(float("nan")) is None


# ── consult ─────────────────────────────────────────────────────────────────


async def test_consult_dispatches_brain_turn_with_voice_prefix() -> None:
    fake = FakeDeps(brain_reply="We decided to ship Friday.")
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(
        frame(
            "consult",
            payload=consult_payload(
                args={
                    "question": "What did we decide?",
                    "context": "Sprint planning call",
                    "responseStyle": "one sentence",
                }
            ),
        )
    )
    chat_id, text, deadline = fake.brain_calls[0]
    assert chat_id == 830
    assert text.startswith("[voice consult]")
    assert "What did we decide?" in text
    assert "Sprint planning call" in text
    assert "one sentence" in text
    assert deadline == pytest.approx(38.0)
    _, body = fake.results[0]
    assert body == {"ok": True, "payload": {"text": "We decided to ship Friday."}}


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"name": CONSULT_TOOL_NAME, "args": {"question": "x"}}, "callId"),
        ({"callId": "c", "args": {"question": "x"}}, "callId"),
        (consult_payload(args={}), "question"),
        (consult_payload(args={"question": "   "}), "question"),
    ],
)
async def test_consult_rejects_bad_payloads(payload, fragment) -> None:
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("consult", payload=payload))
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "BAD_CONSULT"
    assert fragment in body["error"]["message"]
    assert fake.brain_calls == []


async def test_consult_timeout_produces_speakable_error() -> None:
    """The daemon's descriptive timeout must beat the backend's generic
    45 s cut-off — and be a sentence the realtime model can just speak."""
    fake = FakeDeps(brain_error=VoiceRpcTimeout())
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("consult", payload=consult_payload()))
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "CONSULT_TIMEOUT"
    assert "Athena" in body["error"]["message"]
    assert "chat" in body["error"]["message"]


async def test_consult_wall_clock_cap_enforced_even_if_brain_hangs() -> None:
    """Belt+braces: even a deps.run_brain_turn that ignores its deadline
    is cut off by the handler's own wait_for at the consult cap."""
    timing = VoiceRpcTiming(consult_timeout=0.1)
    fake = FakeDeps(brain_delay=5.0)
    handler = VoiceRpcHandler(fake.to_deps(timing))
    await asyncio.wait_for(
        handler.handle(frame("consult", payload=consult_payload())), timeout=2.0
    )
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "CONSULT_TIMEOUT"


async def test_consult_empty_reply_is_descriptive() -> None:
    fake = FakeDeps(brain_reply="   ")
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("consult", payload=consult_payload()))
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "EMPTY_REPLY"


async def test_consult_without_chat_id_is_rejected() -> None:
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    f = VoiceRpcFrame(
        rpc_id="rpc-1", op="consult", assistant_id=894, agent_route="default",
        chat_id=None, payload=consult_payload(),
    )
    await handler.handle(f)
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "BAD_CONSULT"


# ── dispatch ────────────────────────────────────────────────────────────────


def dispatch_payload(**overrides) -> dict:
    base = {
        "taskId": "task-9",
        "callId": "call-9",
        "name": "agent_dispatch",
        "args": {"question": "Summarize this week's commits"},
    }
    base.update(overrides)
    return base


async def _drain_tasks() -> None:
    """Let the detached dispatch task run to completion."""
    for _ in range(50):
        await asyncio.sleep(0.01)
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if not pending:
            return


async def test_dispatch_accepts_first_then_reports_via_voice_tasks() -> None:
    """Accept-first semantics (backend waits only 10 s for the accept);
    the run outcome goes to the voice-tasks result route, NOT the
    voice-rpc result route."""
    fake = FakeDeps(brain_reply="Done — 14 commits, mostly voice work.")
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("dispatch", payload=dispatch_payload()))
    # Accept posted synchronously via voice-rpc result:
    assert fake.results == [
        ("rpc-1", {"ok": True, "payload": {"accepted": True, "taskId": "task-9"}})
    ]
    await _drain_tasks()
    assert fake.task_results == [
        (
            "task-9",
            {"ok": True, "payload": {"text": "Done — 14 commits, mostly voice work."}},
        )
    ]
    chat_id, text, deadline = fake.brain_calls[0]
    assert text.startswith("[voice dispatch]")
    assert "task-9" in text
    assert deadline == pytest.approx(600.0)


async def test_dispatch_missing_task_id_errors_on_rpc_result() -> None:
    fake = FakeDeps()
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("dispatch", payload={"args": {"question": "x"}}))
    _, body = fake.results[0]
    assert body["ok"] is False
    assert body["error"]["code"] == "BAD_DISPATCH"
    assert fake.task_results == []


async def test_dispatch_run_failure_reports_dispatch_failed() -> None:
    fake = FakeDeps(brain_error=RuntimeError("gateway exploded"))
    handler = VoiceRpcHandler(fake.to_deps())
    await handler.handle(frame("dispatch", payload=dispatch_payload()))
    await _drain_tasks()
    task_id, body = fake.task_results[0]
    assert task_id == "task-9"
    assert body["ok"] is False
    assert body["error"]["code"] == "DISPATCH_FAILED"
    assert "gateway exploded" in body["error"]["message"]


async def test_dispatch_result_post_is_retried_once() -> None:
    """A failed POST must never masquerade as a failed RUN — the result
    is retried once before giving up to the backend's reaper."""
    timing = VoiceRpcTiming(dispatch_result_retry_delay=0.01)
    fake = FakeDeps(task_result_errors=[RuntimeError("net blip")])
    handler = VoiceRpcHandler(fake.to_deps(timing))
    await handler.handle(frame("dispatch", payload=dispatch_payload()))
    await _drain_tasks()
    assert len(fake.task_results) == 1
    assert fake.task_results[0][1]["ok"] is True


async def test_dispatch_dedupes_by_task_id() -> None:
    """A re-emitted dispatch frame carries a new rpcId only on backend
    restart — the durable dedupe key is the taskId."""
    fake = FakeDeps(brain_delay=0.15)
    handler = VoiceRpcHandler(fake.to_deps())
    await asyncio.gather(
        handler.handle(frame("dispatch", rpc_id="rpc-a", payload=dispatch_payload())),
        handler.handle(frame("dispatch", rpc_id="rpc-b", payload=dispatch_payload())),
    )
    await _drain_tasks()
    assert len(fake.brain_calls) == 1
    assert len(fake.task_results) == 1


# ── builders + env ──────────────────────────────────────────────────────────


async def test_mint_instructions_compose_all_blocks() -> None:
    text = build_mint_instructions(
        agent_name="Athena",
        persona="Sharp, kind, allergic to fluff.",
        recent_context="KC: status?\nYou: shipping tonight.",
    )
    assert text.startswith("You are Athena")
    assert "Sharp, kind, allergic to fluff." in text
    assert CONSULT_TOOL_NAME in text
    assert "agent_dispatch" in text
    assert "KC: status?" in text


async def test_mint_instructions_omit_empty_blocks() -> None:
    text = build_mint_instructions(agent_name="", persona="", recent_context="")
    assert "the agent" in text
    assert "Recent conversation" not in text


async def test_consult_tool_definition_matches_dto_shape() -> None:
    tool = build_consult_tool_definition()
    assert tool["type"] == "function"
    assert tool["name"] == CONSULT_TOOL_NAME
    params = tool["parameters"]
    assert params["required"] == ["question"]
    assert set(params["properties"]) == {"question", "context", "responseStyle"}


async def test_turn_texts_carry_modality_prefixes() -> None:
    consult = build_consult_turn_text(
        question="Q?", context="ctx", response_style="short"
    )
    assert consult.startswith("[voice consult]")
    dispatch = build_dispatch_turn_text(question="Do it", context="", task_id="t-1")
    assert dispatch.startswith("[voice dispatch]")
    assert "t-1" in dispatch


async def test_load_voice_env_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BGOS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BGOS_VOICE_MODEL", raising=False)
    monkeypatch.delenv("BGOS_VOICE_VOICE", raising=False)
    key, model, voice = load_voice_env()
    assert key == ""
    assert model == "gpt-realtime-2"
    assert voice == "marin"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-general")
    assert load_voice_env()[0] == "sk-general"
    # The BGOS-scoped key wins over the general one.
    monkeypatch.setenv("BGOS_OPENAI_API_KEY", "sk-bgos")
    monkeypatch.setenv("BGOS_VOICE_MODEL", "gpt-realtime-3")
    monkeypatch.setenv("BGOS_VOICE_VOICE", "cedar")
    assert load_voice_env() == ("sk-bgos", "gpt-realtime-3", "cedar")


# quick-wins prompt pack (Iris 514)


async def test_mint_instructions_carry_truthfulness_contract() -> None:
    text = build_mint_instructions(agent_name="Jeff", persona="", recent_context="")
    assert "Truthfulness contract: NEVER invent" in text
    assert "still in progress" in text


async def test_mint_instructions_carry_intent_only_brief_rule() -> None:
    text = build_mint_instructions(agent_name="Jeff", persona="", recent_context="")
    assert "intent and desired outcome" in text
    assert "stale mechanics mislead it" in text


async def test_consult_turn_text_carries_continuation_brief() -> None:
    text = build_consult_turn_text(question="q", context="", response_style="")
    assert CONTINUATION_BRIEF in text
    assert "Reuse those results" in text
    assert "re-check only what changed" in text


async def test_dispatch_turn_text_carries_continuation_brief() -> None:
    text = build_dispatch_turn_text(question="q", context="", task_id="t1")
    assert CONTINUATION_BRIEF in text
    assert "Reuse those results" in text
    assert "re-check only what changed" in text
