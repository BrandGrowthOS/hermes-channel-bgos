"""Tests for the STATUS: marker lane (PR #14, added at the merge gate).

Covers the pure parse function only; the API call is the caller's job and
its wiring is exercised by the adapter send-path tests.
"""

from hermes_channel_bgos.bgos_adapter import _parse_status_line


def test_status_line_extracted_and_stripped():
    cleaned, status = _parse_status_line("STATUS: Reviewing the report\nHello KC")
    assert status == "Reviewing the report"
    assert "STATUS" not in cleaned
    assert cleaned == "Hello KC"


def test_status_midtext_line_is_honored_and_removed():
    cleaned, status = _parse_status_line("Part one\nSTATUS: deep in the build\nPart two")
    assert status == "deep in the build"
    assert cleaned == "Part one\n\nPart two" or "STATUS" not in cleaned


def test_empty_and_dash_clear_the_status():
    for raw in ("STATUS:\ntext", "STATUS: -\ntext"):
        cleaned, status = _parse_status_line(raw)
        assert status == ""
        assert "STATUS" not in cleaned


def test_no_marker_returns_unchanged_and_none():
    cleaned, status = _parse_status_line("Just a normal reply")
    assert status is None
    assert cleaned == "Just a normal reply"
    cleaned, status = _parse_status_line("")
    assert status is None


def test_first_of_multiple_markers_wins_and_all_are_stripped():
    cleaned, status = _parse_status_line(
        "STATUS: first\nbody\nSTATUS: second\ntail"
    )
    assert status == "first"
    assert "STATUS" not in cleaned
    assert "body" in cleaned and "tail" in cleaned
