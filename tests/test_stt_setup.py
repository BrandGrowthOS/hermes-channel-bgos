"""Tests for the background STT model setup card."""
from __future__ import annotations

import asyncio
import base64
import copy
from pathlib import Path
from typing import Any

import pytest

import hermes_channel_bgos.bgos_adapter as bgos_adapter_module
import hermes_channel_bgos.stt_setup as stt_setup
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.stt_setup import (
    SttSetupNotifier,
    downloaded_bytes,
    format_progress,
    hf_hub_root,
    is_model_cached,
    model_dir,
    stt_setup_card_enabled,
)
from tests.mocks.mock_hermes import (
    MessageEvent as GatewayMessageEvent,
    MessageType as GatewayMessageType,
)


MB = 1024 * 1024
INITIAL_TEXT = (
    "Getting ready to listen. Your agent is downloading a small speech model "
    "(about 150 MB) so it can understand voice notes. Typing works normally "
    "in the meantime, and the note you just sent will be heard once this "
    "finishes."
)
READY_TEXT = "Voice is ready. Your voice notes are understood from now on."
STALLED_TEXT = (
    "The download paused. It will pick up again with your next voice note."
)


@pytest.fixture(autouse=True)
def _isolate_stt_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HUGGINGFACE_HUB_CACHE",
        "HF_HOME",
        "BGOS_STT_MODEL",
        "BGOS_VOICE_SETUP_CARD",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_available(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    monkeypatch.setattr(
        stt_setup,
        "faster_whisper_available",
        lambda: available,
    )


def _write_sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def _expected_meta(
    stage: str,
    downloaded: int,
    total: int | None,
) -> dict[str, Any]:
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


class EventRecorder:
    def __init__(self, message_id: int | None = 701) -> None:
        self.message_id = message_id
        self.posts: list[tuple[str, str, dict[str, Any]]] = []
        self.edits: list[tuple[str, int, str, dict[str, Any]]] = []

    async def post(
        self,
        chat_id: str,
        text: str,
        event_meta: dict[str, Any],
    ) -> int | None:
        self.posts.append((chat_id, text, copy.deepcopy(event_meta)))
        return self.message_id

    async def edit(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        event_meta: dict[str, Any],
    ) -> None:
        self.edits.append(
            (chat_id, message_id, text, copy.deepcopy(event_meta))
        )


def test_hf_hub_root_respects_environment_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = Path("/test/home")
    monkeypatch.setattr(
        stt_setup.Path,
        "home",
        classmethod(lambda cls: fake_home),
    )
    monkeypatch.setenv("HF_HOME", "/test/hf-home")
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", "/test/hub-cache")
    assert hf_hub_root() == Path("/test/hub-cache")

    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE")
    assert hf_hub_root() == Path("/test/hf-home/hub")

    monkeypatch.delenv("HF_HOME")
    assert hf_hub_root() == fake_home / ".cache" / "huggingface" / "hub"


def test_model_dir_uses_faster_whisper_repo_name(tmp_path: Path) -> None:
    assert model_dir(tmp_path, "small") == (
        tmp_path / "models--Systran--faster-whisper-small"
    )


def test_is_model_cached_requires_snapshot_model_bin(tmp_path: Path) -> None:
    directory = model_dir(tmp_path, "base")
    snapshot = directory / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    assert is_model_cached(directory) is False

    (snapshot / "model.bin").write_bytes(b"model")
    assert is_model_cached(directory) is True


def test_downloaded_bytes_sums_only_direct_incomplete_blobs(
    tmp_path: Path,
) -> None:
    directory = model_dir(tmp_path, "base")
    blobs = directory / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "one.incomplete").write_bytes(b"123")
    (blobs / "two.incomplete").write_bytes(b"45678")
    (blobs / "complete").write_bytes(b"ignored")
    (blobs / "directory.incomplete").mkdir()
    nested = blobs / "nested"
    nested.mkdir()
    (nested / "three.incomplete").write_bytes(b"ignored")

    assert downloaded_bytes(directory) == 8


def test_format_progress_uses_exact_observed_values() -> None:
    assert format_progress(74 * MB, 148 * MB) == (
        "Downloading the speech model: 50% (74 of 148 MB)."
    )
    assert format_progress(74 * MB, None) == (
        "Downloading the speech model: 74 MB so far."
    )
    assert format_progress(296 * MB, 148 * MB) == (
        "Downloading the speech model: 100% (296 of 148 MB)."
    )


def test_setup_card_switch_defaults_on_and_honors_false_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert stt_setup_card_enabled() is True
    for value in ("0", "off", "false", " OFF "):
        monkeypatch.setenv("BGOS_VOICE_SETUP_CARD", value)
        assert stt_setup_card_enabled() is False
    monkeypatch.setenv("BGOS_VOICE_SETUP_CARD", "1")
    assert stt_setup_card_enabled() is True


@pytest.mark.asyncio
async def test_cached_model_posts_no_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available(monkeypatch, True)
    cached = model_dir(tmp_path, "base") / "snapshots" / "hash" / "model.bin"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"model")

    async def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected notifier dependency call")

    notifier = SttSetupNotifier(
        post_event=unexpected,
        edit_event=unexpected,
        head_total=unexpected,
        sleep=unexpected,
        clock=lambda: 0.0,
        hub_root=tmp_path,
    )
    await notifier.maybe_notify("42")


@pytest.mark.asyncio
async def test_disabled_setup_card_posts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BGOS_VOICE_SETUP_CARD", "false")
    _set_available(monkeypatch, True)

    async def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected notifier dependency call")

    notifier = SttSetupNotifier(
        post_event=unexpected,
        edit_event=unexpected,
        head_total=unexpected,
        sleep=unexpected,
        clock=lambda: 0.0,
        hub_root=tmp_path,
    )
    await notifier.maybe_notify("42")


@pytest.mark.asyncio
async def test_missing_faster_whisper_posts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available(monkeypatch, False)

    async def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected notifier dependency call")

    notifier = SttSetupNotifier(
        post_event=unexpected,
        edit_event=unexpected,
        head_total=unexpected,
        sleep=unexpected,
        clock=lambda: 0.0,
        hub_root=tmp_path,
    )
    await notifier.maybe_notify("42")


@pytest.mark.asyncio
async def test_happy_path_reports_real_growth_then_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available(monkeypatch, True)
    total = 148 * MB
    directory = model_dir(tmp_path, "base")
    incomplete = directory / "blobs" / "model.incomplete"
    now = 0.0
    polls = 0
    recorder = EventRecorder()
    edit_times: list[float] = []

    async def head_total(model: str) -> int:
        assert model == "base"
        return total

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now, polls
        assert seconds == 1.0
        now += seconds
        polls += 1
        if polls == 1:
            _write_sparse(incomplete, 10 * MB)
        elif polls == 4:
            _write_sparse(incomplete, 74 * MB)
        elif polls == 7:
            ready = directory / "snapshots" / "hash" / "model.bin"
            ready.parent.mkdir(parents=True)
            ready.write_bytes(b"model")

    async def edit(
        chat_id: str,
        message_id: int,
        text: str,
        event_meta: dict[str, Any],
    ) -> None:
        edit_times.append(now)
        await recorder.edit(chat_id, message_id, text, event_meta)

    notifier = SttSetupNotifier(
        post_event=recorder.post,
        edit_event=edit,
        head_total=head_total,
        sleep=sleep,
        clock=clock,
        hub_root=tmp_path,
    )
    await notifier.maybe_notify("42")

    assert recorder.posts == [
        ("42", INITIAL_TEXT, _expected_meta("downloading", 0, total)),
    ]
    progress_edits = [
        edit
        for edit in recorder.edits
        if edit[3]["payload"]["progress"]["stage"] == "downloading"
    ]
    assert [
        edit[3]["payload"]["progress"]["downloadedBytes"]
        for edit in progress_edits
    ] == [10 * MB, 74 * MB]
    assert progress_edits[0][2] == (
        "Downloading the speech model: 7% (10 of 148 MB)."
    )
    assert progress_edits[1][2] == (
        "Downloading the speech model: 50% (74 of 148 MB)."
    )
    assert recorder.edits[-1] == (
        "42",
        701,
        READY_TEXT,
        _expected_meta("ready", total, total),
    )
    assert all(
        later - earlier >= 2.5
        for earlier, later in zip(edit_times, edit_times[1:])
    )
    assert polls == 9


@pytest.mark.asyncio
async def test_unknown_total_uses_so_far_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available(monkeypatch, True)
    directory = model_dir(tmp_path, "base")
    incomplete = directory / "blobs" / "model.incomplete"
    now = 0.0
    polls = 0
    recorder = EventRecorder()

    async def head_total(model: str) -> None:
        return None

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now, polls
        now += seconds
        polls += 1
        if polls == 1:
            _write_sparse(incomplete, 74 * MB)
        elif polls == 4:
            ready = directory / "snapshots" / "hash" / "model.bin"
            ready.parent.mkdir(parents=True)
            ready.write_bytes(b"model")

    notifier = SttSetupNotifier(
        post_event=recorder.post,
        edit_event=recorder.edit,
        head_total=head_total,
        sleep=sleep,
        clock=clock,
        hub_root=tmp_path,
    )
    await notifier.maybe_notify("42")

    progress_edits = [
        edit
        for edit in recorder.edits
        if edit[3]["payload"]["progress"]["stage"] == "downloading"
    ]
    assert len(progress_edits) == 1
    assert progress_edits[0][2] == (
        "Downloading the speech model: 74 MB so far."
    )
    assert progress_edits[0][3] == _expected_meta(
        "downloading",
        74 * MB,
        None,
    )
    assert recorder.edits[-1] == (
        "42",
        701,
        READY_TEXT,
        _expected_meta("ready", 74 * MB, None),
    )
    assert all(
        call[2]["payload"]["progress"]["totalBytes"] is None
        for call in recorder.posts
    )
    assert all(
        call[3]["payload"]["progress"]["totalBytes"] is None
        for call in recorder.edits
    )


@pytest.mark.asyncio
async def test_stall_can_retry_on_a_later_voice_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available(monkeypatch, True)
    directory = model_dir(tmp_path, "base")
    now = 0.0
    sleep_calls = 0
    recorder = EventRecorder()

    async def head_total(model: str) -> None:
        return None

    def clock() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now, sleep_calls
        assert seconds == 1.0
        now += 30.0
        sleep_calls += 1
        if sleep_calls == 4:
            ready = directory / "snapshots" / "hash" / "model.bin"
            ready.parent.mkdir(parents=True)
            ready.write_bytes(b"model")

    notifier = SttSetupNotifier(
        post_event=recorder.post,
        edit_event=recorder.edit,
        head_total=head_total,
        sleep=sleep,
        clock=clock,
        hub_root=tmp_path,
    )
    await notifier.maybe_notify("42")

    assert recorder.edits == [
        (
            "42",
            701,
            STALLED_TEXT,
            _expected_meta("stalled", 0, None),
        ),
    ]
    assert notifier._completed is False

    await notifier.maybe_notify("42")
    assert len(recorder.posts) == 2
    assert recorder.edits[-1][2] == READY_TEXT
    assert recorder.edits[-1][3] == _expected_meta("ready", 0, None)
    assert notifier._completed is True


@pytest.mark.asyncio
async def test_two_concurrent_calls_post_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available(monkeypatch, True)
    directory = model_dir(tmp_path, "base")
    entered_sleep = asyncio.Event()
    release_sleep = asyncio.Event()
    recorder = EventRecorder()

    async def head_total(model: str) -> None:
        return None

    async def sleep(seconds: float) -> None:
        entered_sleep.set()
        await release_sleep.wait()

    notifier = SttSetupNotifier(
        post_event=recorder.post,
        edit_event=recorder.edit,
        head_total=head_total,
        sleep=sleep,
        clock=lambda: 0.0,
        hub_root=tmp_path,
    )
    first = asyncio.create_task(notifier.maybe_notify("42"))
    await entered_sleep.wait()

    await asyncio.wait_for(notifier.maybe_notify("42"), timeout=0.1)
    assert len(recorder.posts) == 1

    ready = directory / "snapshots" / "hash" / "model.bin"
    ready.parent.mkdir(parents=True)
    ready.write_bytes(b"model")
    release_sleep.set()
    await first
    assert len(recorder.posts) == 1


@pytest.mark.asyncio
async def test_post_exception_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_available(monkeypatch, True)

    async def fail_post(
        chat_id: str,
        text: str,
        event_meta: dict[str, Any],
    ) -> None:
        raise RuntimeError("post failed")

    async def head_total(model: str) -> None:
        return None

    async def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("watcher should not start")

    notifier = SttSetupNotifier(
        post_event=fail_post,
        edit_event=unexpected,
        head_total=head_total,
        sleep=unexpected,
        clock=lambda: 0.0,
        hub_root=tmp_path,
    )

    await notifier.maybe_notify("42")
    assert notifier._active is False
    assert notifier._completed is False


@pytest.mark.asyncio
async def test_adapter_setup_post_uses_chat_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = BGOSAdapter(
        BgosConfig(base_url="http://x", pairing_token="pair_xyz")
    )
    adapter._state.record_inbound_chat(42, "session-42")
    adapter._state.addressed_assistant_id_by_chat[42] = 7
    posted: list[dict[str, Any]] = []

    async def owner_for_chat(chat_id: int) -> int:
        assert chat_id == 42
        return 9

    async def post_send_message(**kwargs: Any) -> dict[str, Any]:
        posted.append(kwargs)
        return {"message": {"id": 701}}

    monkeypatch.setattr(adapter, "_assistant_id_for_chat", owner_for_chat)
    monkeypatch.setattr(adapter._api, "post_send_message", post_send_message)
    try:
        message_id = await adapter._post_stt_setup_event(
            "42",
            INITIAL_TEXT,
            _expected_meta("downloading", 0, None),
        )
    finally:
        await adapter._api.close()

    assert message_id == 701
    assert len(posted) == 1
    assert posted[0]["chat_id"] == 42
    assert posted[0]["assistant_id"] == 9
    assert posted[0]["message_type"] == "event"
    assert posted[0]["session_handle"] == "session-42"


@pytest.mark.asyncio
async def test_adapter_setup_edit_uses_patch_message(
    mock_bgos_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = BGOSAdapter(
        BgosConfig(
            base_url=mock_bgos_server.url,
            pairing_token="pair_xyz",
        )
    )
    adapter._state.last_user_id_by_chat[42] = "user-42"
    mock_bgos_server.on("PATCH", "/api/v1/messages/701").respond(
        200,
        {"id": 701},
    )
    patched: list[tuple[int, dict[str, Any]]] = []
    event_meta = _expected_meta("ready", 148 * MB, 148 * MB)
    original_patch_message = adapter._api.patch_message

    async def patch_message(
        message_id: int,
        **kwargs: Any,
    ) -> dict[str, int]:
        patched.append((message_id, kwargs))
        return await original_patch_message(message_id, **kwargs)

    monkeypatch.setattr(adapter._api, "patch_message", patch_message)
    try:
        await adapter._edit_stt_setup_event(
            "42",
            "701",
            READY_TEXT,
            event_meta,
        )
    finally:
        await adapter._api.close()

    assert patched == [
        (
            701,
            {
                "text": READY_TEXT,
                "event_meta": event_meta,
                "user_id": "user-42",
            },
        ),
    ]
    request = mock_bgos_server.last_request(
        "PATCH",
        "/api/v1/messages/701",
    )
    assert request.json_body == {
        "text": READY_TEXT,
        "eventMeta": event_meta,
        "userId": "user-42",
    }


@pytest.mark.asyncio
async def test_disconnect_cancels_active_setup_task() -> None:
    adapter = BGOSAdapter(
        BgosConfig(base_url="http://x", pairing_token="pair_xyz")
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    disconnected = False

    class FakeSetup:
        async def maybe_notify(self, chat_id: str) -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()

    adapter._stt_setup = FakeSetup()
    adapter._schedule_stt_setup(42)
    await asyncio.wait_for(started.wait(), timeout=0.5)
    try:
        assert len(adapter._stt_setup_tasks) == 1
        await adapter.disconnect()
        disconnected = True
        assert cancelled.is_set()
        assert adapter._stt_setup_tasks == set()
    finally:
        release.set()
        await asyncio.sleep(0)
        if not disconnected:
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_inbound_voice_schedules_setup_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = BGOSAdapter(
        BgosConfig(base_url="http://x", pairing_token="pair_xyz")
    )
    adapter._state.set_route(7, "default")
    monkeypatch.setattr(
        bgos_adapter_module,
        "_GatewayMessageEvent",
        GatewayMessageEvent,
    )
    monkeypatch.setattr(
        bgos_adapter_module,
        "_GatewayMessageType",
        GatewayMessageType,
    )
    monkeypatch.setattr(
        adapter,
        "build_source",
        lambda *, chat_id, user_id: {
            "chat_id": chat_id,
            "user_id": user_id,
        },
        raising=False,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    notified_chats: list[str] = []
    handled: list[GatewayMessageEvent] = []

    class FakeSetup:
        async def maybe_notify(self, chat_id: str) -> None:
            notified_chats.append(chat_id)
            entered.set()
            await release.wait()

    async def handle_message(event: GatewayMessageEvent) -> None:
        handled.append(event)

    adapter._stt_setup = FakeSetup()
    adapter.handle_message = handle_message
    data_uri = "data:audio/ogg;base64," + base64.b64encode(
        b"OggS\x00voice"
    ).decode("ascii")

    await asyncio.wait_for(
        adapter._handle_inbound(
            {
                "assistant_id": 7,
                "user_id": "u",
                "chat_id": 42,
                "message_id": 120,
                "text": "listen",
                "files": [
                    {
                        "filename": "voice.ogg",
                        "mime": "audio/ogg",
                        "dataUri": data_uri,
                    },
                ],
                "message_type": "standard",
            }
        ),
        timeout=0.5,
    )
    await asyncio.wait_for(entered.wait(), timeout=0.5)

    assert notified_chats == ["42"]
    assert len(handled) == 1
    assert handled[0].message_type == GatewayMessageType.VOICE
    release.set()
    await asyncio.sleep(0)
