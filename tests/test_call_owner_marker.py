"""Ring-the-owner marker for Hermes (capability #16).

Hermes was the last channel to get this. The capability canon told every
channel it could ring its owner; the Claude Code delta named `call_owner`,
Gobot has `replyHandle.callOwner`, OpenClaw has `POST /v1/call-owner`, and the
Hermes delta named nothing. A real agent, asked for a test call, invented a
mechanism: it shelled out to curl and tripped its HOST's terminal approval
layer, so the owner saw a generic shell prompt that timed out and was then
recorded as denied, while waiting for a phone that never rang.

These pin the marker's behaviour, not its wording.
"""
import pytest

from hermes_channel_bgos.bgos_adapter import _parse_call_block


class TestParseCallBlock:
    def test_absent_marker_leaves_text_untouched(self):
        # The overwhelmingly common case: every normal reply must be unaffected.
        text = "Just a normal reply with no marker."
        cleaned, reason = _parse_call_block(text)
        assert cleaned == text
        assert reason is None

    def test_extracts_reason_and_strips_the_marker(self):
        cleaned, reason = _parse_call_block(
            "Heads up.\n[[BGOS_CALL]]the build finished[[/BGOS_CALL]]"
        )
        assert reason == "the build finished"
        assert "BGOS_CALL" not in cleaned
        assert cleaned == "Heads up."

    def test_empty_block_still_requests_a_call(self):
        # An agent that wrote the marker meant to ring. Dropping the request
        # because it left the reason blank would be the silent-failure shape
        # this whole change exists to remove.
        cleaned, reason = _parse_call_block("[[BGOS_CALL]][[/BGOS_CALL]]")
        assert reason == ""
        assert reason is not None
        assert cleaned == ""

    def test_reason_is_capped_to_the_backend_limit(self):
        # The backend caps the ring reason at 200 chars. Trimming here means a
        # long sentence rings with a shortened reason instead of failing
        # validation and not ringing at all.
        long_reason = "x" * 500
        _, reason = _parse_call_block(f"[[BGOS_CALL]]{long_reason}[[/BGOS_CALL]]")
        assert len(reason) == 200

    def test_only_the_first_marker_is_honored(self):
        # One turn rings once. Two markers must not mean two calls.
        cleaned, reason = _parse_call_block(
            "[[BGOS_CALL]]first[[/BGOS_CALL]] and [[BGOS_CALL]]second[[/BGOS_CALL]]"
        )
        assert reason == "first"
        assert "BGOS_CALL" not in cleaned

    def test_tag_is_case_insensitive_like_the_other_markers(self):
        _, reason = _parse_call_block("[[bgos_call]]lowercase[[/bgos_call]]")
        assert reason == "lowercase"

    def test_multiline_reason_is_collapsed_by_strip(self):
        _, reason = _parse_call_block("[[BGOS_CALL]]\n  spread out\n[[/BGOS_CALL]]")
        assert reason == "spread out"

    def test_empty_input_is_safe(self):
        assert _parse_call_block("") == ("", None)
