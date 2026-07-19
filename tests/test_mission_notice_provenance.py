"""Goal notice grammar and outbound provenance coverage."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.missions_bridge import MissionLane, classify_goal_line


_SET_INSTRUCTIONS = (
    "I'll keep working until the goal is done, you pause/clear it, or the "
    "budget is exhausted.\n"
    "Controls: /goal status · /goal pause · /goal resume · "
    "/goal clear"
)
_PARSE_FAILURE_NOTICE = (
    "\u23f8 Goal paused \u2014 the judge model (3 turns) isn't returning the "
    "required JSON verdict. Route the judge to a stricter model in "
    "~/.hermes/config.yaml:\n"
    "  auxiliary:\n"
    "    goal_judge:\n"
    "      provider: openrouter\n"
    "      model: google/gemini-3-flash-preview\n"
    "Then /goal resume to continue."
)


def _snapshot(
    *,
    status: str = "active",
    updated_at: str = "2026-07-19T00:00:00.000Z",
) -> dict:
    return {
        "id": "mission-1",
        "assistantId": 7,
        "status": status,
        "origin": "derived",
        "title": "Ship the report",
        "updatedAt": updated_at,
    }


def _adapter_with_mock_lane() -> tuple[BGOSAdapter, SimpleNamespace]:
    adapter = BGOSAdapter(BgosConfig(
        base_url="https://bgos.test",
        pairing_token="pair_xyz",
    ))
    adapter._state.record_inbound_chat(42)
    adapter._state.addressed_assistant_id_by_chat[42] = 7
    lane = SimpleNamespace(
        handle_goal_line=AsyncMock(),
        handle_mission_event=AsyncMock(),
        close=AsyncMock(),
    )
    adapter._mission_lane = lane
    adapter._assistant_id_for_chat = AsyncMock(return_value=7)
    adapter._api.post_send_message = AsyncMock(
        return_value={"message": {"id": 801}},
    )
    return adapter, lane


@pytest.mark.parametrize(
    "tail",
    [
        _SET_INSTRUCTIONS,
        _SET_INSTRUCTIONS + "\nCompletion contract:\n- Verification: tests pass",
        (
            _SET_INSTRUCTIONS
            + "\n(Couldn't draft a contract \u2014 running as a free-form "
            "goal.)"
        ),
    ],
    ids=["plain", "contract", "draft-failure"],
)
def test_classify_canonical_multiline_set_notices(tail: str) -> None:
    result = classify_goal_line(
        "⊙ Goal set (12-turn budget): Ship the report\n" + tail
    )

    assert result is not None
    assert result.kind == "set"
    assert result.goal_text == "Ship the report"
    assert result.max_turns == 12
    assert result.provenance == "direct"


def test_classify_rejects_trailing_prose_after_contract() -> None:
    notice = (
        "⊙ Goal set (12-turn budget): Ship the report\n"
        + _SET_INSTRUCTIONS
        + "\nCompletion contract:\n- Verification: tests pass\n"
        + "Here is unrelated prose."
    )

    assert classify_goal_line(notice) is None


@pytest.mark.parametrize(
    "notice",
    [
        "✓ Goal achieved: tests pass\nHere is an ordinary reply.",
        "↻ Continuing toward goal (2/12): checking\nExtra prose.",
        "✓ Goal cleared.\nExtra prose.",
        "⏸ Goal paused \u2014 4/12 turns used.\nExtra prose.",
        (
            "▶ Goal resumed: Ship the report\n"
            "Send any message to continue, or wait \u2014 I'll take the next "
            "step on the next turn.\nExtra prose."
        ),
        (
            "⊙ Goal set (12-turn budget): Ship the report\n"
            "This is unrelated prose."
        ),
    ],
)
def test_classify_rejects_notice_prefix_with_extra_prose(notice: str) -> None:
    assert classify_goal_line(notice) is None


def test_classify_exact_multiline_parse_failure_pause() -> None:
    result = classify_goal_line(_PARSE_FAILURE_NOTICE)

    assert result is not None
    assert result.kind == "paused"
    assert result.reason == (
        "judge model returned unparseable output 3 turns in a row"
    )
    assert classify_goal_line(_PARSE_FAILURE_NOTICE + "\nExtra prose.") is None


@pytest.mark.asyncio
async def test_notify_exact_done_is_not_treated_as_post_turn_notice() -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = "✓ Goal achieved: all checks pass"

    try:
        await adapter.send(42, notice, metadata={"notify": True})

        lane.handle_goal_line.assert_not_awaited()
        assert adapter._api.post_send_message.await_args.kwargs["text"] == notice
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_notify_exact_continue_is_not_treated_as_post_turn_notice() -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = "\u21bb Continuing toward goal (2/12): checking"

    try:
        await adapter.send(42, notice, metadata={"notify": True})

        lane.handle_goal_line.assert_not_awaited()
        assert adapter._api.post_send_message.await_args.kwargs["text"] == notice
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_done_without_notify_is_treated_as_post_turn_notice() -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = "✓ Goal achieved: all checks pass"

    try:
        await adapter.send(42, notice)

        lane.handle_goal_line.assert_awaited_once_with(
            notice,
            chat_id=42,
            assistant_id=7,
        )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_direct_goal_wait_with_notify_is_allowed() -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = (
        "⏳ Goal parked on pid 99 (tests running). "
        "Loop pauses until it exits."
    )

    try:
        await adapter.send(42, notice, metadata={"notify": True})

        lane.handle_goal_line.assert_awaited_once_with(
            notice,
            chat_id=42,
            assistant_id=7,
        )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_direct_pause_title_with_turn_shaped_text_is_allowed() -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = "⏸ Goal paused: Ship 4/20 reports"

    try:
        await adapter.send(42, notice, metadata={"notify": True})

        lane.handle_goal_line.assert_awaited_once_with(
            notice,
            chat_id=42,
            assistant_id=7,
        )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "notice",
    [
        "⊙ Goal set (12-turn budget): Ship the report\n" + _SET_INSTRUCTIONS,
        "\u2713 Goal cleared.",
        (
            "\u25b6 Goal resumed: Ship the report\n"
            "Send any message to continue, or wait \u2014 I'll take the next "
            "step on the next turn."
        ),
        "\u23f8 Goal paused: Ship the report",
        (
            "\u23f3 Goal parked on pid 99 (tests running). "
            "Loop pauses until it exits."
        ),
    ],
    ids=["set", "clear", "resume", "pause", "wait"],
)
async def test_direct_notices_require_notify_metadata(notice: str) -> None:
    adapter, lane = _adapter_with_mock_lane()

    try:
        await adapter.send(42, notice)

        lane.handle_goal_line.assert_not_awaited()
        assert adapter._api.post_send_message.await_args.kwargs["text"] == notice
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_automatic_pause_with_notify_is_rejected() -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = (
        "⏸ Goal paused \u2014 4/20 turns used. "
        "Use /goal resume to keep going, or /goal clear to stop."
    )

    try:
        await adapter.send(42, notice, metadata={"notify": True})

        lane.handle_goal_line.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_automatic_park_with_notify_is_rejected() -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = (
        "\u23f3 Goal parked \u2014 waiting on pid 99: tests are running"
    )

    try:
        await adapter.send(42, notice, metadata={"notify": True})

        lane.handle_goal_line.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker",
    [
        "[[BGOS_REPLY_TO]]12[[/BGOS_REPLY_TO]]",
        "[[BGOS_BUTTONS]]Yes | yes[[/BGOS_BUTTONS]]",
        (
            "[[BGOS_EVENT]]"
            '{"source":"test","title":"Test"}'
            "[[/BGOS_EVENT]]"
        ),
    ],
    ids=["reply", "buttons", "event"],
)
async def test_transformed_direct_notice_is_not_classified(marker: str) -> None:
    adapter, lane = _adapter_with_mock_lane()
    notice = (
        f"⊙ Goal set (12-turn budget): Ship the report {marker}\n"
        + _SET_INSTRUCTIONS
    )

    try:
        await adapter.send(42, notice, metadata={"notify": True})

        lane.handle_goal_line.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_multiline_parse_failure_pause_without_notify_is_allowed() -> None:
    adapter, lane = _adapter_with_mock_lane()

    try:
        await adapter.send(42, _PARSE_FAILURE_NOTICE)

        lane.handle_goal_line.assert_awaited_once_with(
            _PARSE_FAILURE_NOTICE,
            chat_id=42,
            assistant_id=7,
        )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_streaming_preview_is_never_treated_as_goal_notice() -> None:
    adapter, lane = _adapter_with_mock_lane()
    preview = "\u2713 Goal achieved: this sentence is still streaming"

    try:
        await adapter.send(42, preview, metadata={"expect_edits": True})

        lane.handle_goal_line.assert_not_awaited()
        assert adapter._api.post_send_message.await_args.kwargs["text"] == preview
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_direct_resume_notice_updates_bound_mission() -> None:
    adapter = BGOSAdapter(BgosConfig(
        base_url="https://bgos.test",
        pairing_token="pair_xyz",
    ))
    adapter._state.record_inbound_chat(42)
    adapter._state.addressed_assistant_id_by_chat[42] = 7
    adapter._assistant_id_for_chat = AsyncMock(return_value=7)
    adapter._api.get_active_mission = AsyncMock(
        return_value={"mission": None},
    )
    adapter._api.create_mission = AsyncMock(
        return_value={"ok": True, "mission": _snapshot()},
    )
    adapter._api.resume_mission = AsyncMock(
        return_value={
            "ok": True,
            "mission": _snapshot(updated_at="2026-07-19T00:00:01.000Z"),
        },
    )
    adapter._api.post_send_message = AsyncMock(
        return_value={"message": {"id": 801}},
    )
    adapter._mission_lane = MissionLane(adapter._api)
    set_notice = (
        "⊙ Goal set (20-turn budget): Ship the report\n"
        + _SET_INSTRUCTIONS
    )
    resume_notice = (
        "▶ Goal resumed: Ship the report\n"
        "Send any message to continue, or wait \u2014 I'll take the next step "
        "on the next turn."
    )

    try:
        await adapter.send(42, set_notice, metadata={"notify": True})
        await adapter.send(42, resume_notice, metadata={"notify": True})

        adapter._api.resume_mission.assert_awaited_once_with(
            assistant_id=7,
            mission_id="mission-1",
        )
    finally:
        await adapter.disconnect()
