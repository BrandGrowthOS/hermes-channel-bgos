"""Tests for the scheduled apply of a plugin update at a safe moment
(design 2.3, decision D8).

Pure decisions are table tested here: WHAT to do (apply, restart onto a
staged install, or nothing) and WHEN it is safe (idle held for the whole
quiet window, no message in or out for 10 minutes, no backoff). The
adapter loop that drives them is tested in test_scheduled_update_loop.py
with fakes and an injected clock.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_channel_bgos import scheduled_update
from hermes_channel_bgos.scheduled_update import (
    MAX_ATTEMPTS_PER_TARGET,
    QUIET_SECONDS,
    SafeMoment,
    ScheduledPlan,
    decide_safe_moment,
    decide_scheduled_update,
    next_idle_since,
)


# -----------------------------------------------------------------------------
# decide_scheduled_update: what to do
# -----------------------------------------------------------------------------


def _plan(**overrides) -> ScheduledPlan:
    inputs = {
        "current": "0.30.0",
        "latest": None,
        "pending": None,
        "auto_update_enabled": True,
        "supervised": True,
        "attempts": {},
    }
    inputs.update(overrides)
    return decide_scheduled_update(**inputs)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # A newer same-major version at the pinned source: pull, then restart.
        ({"latest": "0.30.1"}, ScheduledPlan("apply", "newer_available", "0.30.1")),
        ({"latest": "0.31.0"}, ScheduledPlan("apply", "newer_available", "0.31.0")),
        # Already staged (pulled by an earlier update_now or a run that
        # found the agent busy): only the restart is missing.
        ({"pending": "0.30.1"}, ScheduledPlan("restart", "staged", "0.30.1")),
        (
            {"latest": "0.30.1", "pending": "0.30.1"},
            ScheduledPlan("restart", "staged", "0.30.1"),
        ),
        # The source moved past the staged install: pull to the newest, one
        # restart instead of two.
        (
            {"latest": "0.30.2", "pending": "0.30.1"},
            ScheduledPlan("apply", "newer_available", "0.30.2"),
        ),
        # Nothing newer.
        ({}, ScheduledPlan("none", "up_to_date")),
        ({"latest": "0.30.0"}, ScheduledPlan("none", "up_to_date")),
        ({"latest": "0.29.9"}, ScheduledPlan("none", "up_to_date")),
        # Major jumps need a human, at the source and on disk alike.
        ({"latest": "1.0.0"}, ScheduledPlan("none", "up_to_date")),
        ({"pending": "1.0.0"}, ScheduledPlan("none", "up_to_date")),
        # An older version on disk (an operator checkout) is never a reason
        # to restart unattended.
        ({"pending": "0.29.0"}, ScheduledPlan("none", "up_to_date")),
        # D8: never exit without something to bring it back.
        (
            {"latest": "0.30.1", "supervised": False},
            ScheduledPlan("none", "unsupervised"),
        ),
        (
            {"pending": "0.30.1", "supervised": False},
            ScheduledPlan("none", "unsupervised"),
        ),
        # The BGOS_AUTO_UPDATE kill switch wins over everything.
        (
            {"latest": "0.30.1", "auto_update_enabled": False},
            ScheduledPlan("none", "updates_disabled"),
        ),
        # A target that already failed to come up MAX times is left for a
        # human (no restart loop); a new target starts fresh.
        (
            {"pending": "0.30.1", "attempts": {"0.30.1": MAX_ATTEMPTS_PER_TARGET}},
            ScheduledPlan("none", "attempts_exhausted", "0.30.1"),
        ),
        (
            {"latest": "0.30.1", "attempts": {"0.30.1": MAX_ATTEMPTS_PER_TARGET}},
            ScheduledPlan("none", "attempts_exhausted", "0.30.1"),
        ),
        (
            {"pending": "0.30.1", "attempts": {"0.30.1": MAX_ATTEMPTS_PER_TARGET - 1}},
            ScheduledPlan("restart", "staged", "0.30.1"),
        ),
        (
            {"latest": "0.30.2", "attempts": {"0.30.1": MAX_ATTEMPTS_PER_TARGET}},
            ScheduledPlan("apply", "newer_available", "0.30.2"),
        ),
    ],
)
def test_decide_scheduled_update(overrides, expected) -> None:
    assert _plan(**overrides) == expected


def test_max_attempts_is_three() -> None:
    # Mirrors the watcher's cap (design section 5: at most 3 attempts per
    # target version, then failed and visible).
    assert MAX_ATTEMPTS_PER_TARGET == 3


# -----------------------------------------------------------------------------
# next_idle_since: idle must be HELD, a busy sample restarts the window
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("busy", "idle_since", "now", "expected"),
    [
        (True, None, 100.0, None),
        (True, 50.0, 100.0, None),
        (False, None, 100.0, 100.0),
        (False, 50.0, 100.0, 50.0),
    ],
)
def test_next_idle_since(busy, idle_since, now, expected) -> None:
    assert next_idle_since(busy=busy, idle_since=idle_since, now=now) == expected


# -----------------------------------------------------------------------------
# decide_safe_moment: when
# -----------------------------------------------------------------------------


def _moment(**overrides) -> SafeMoment:
    inputs = {
        "now": 10_000.0,
        "busy": False,
        "idle_since": 10_000.0 - QUIET_SECONDS,
        "last_message_at": None,
        "not_before": None,
    }
    inputs.update(overrides)
    return decide_safe_moment(**inputs)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, SafeMoment(True, "safe")),
        ({"last_message_at": 10_000.0 - QUIET_SECONDS}, SafeMoment(True, "safe")),
        # Same busy definition as the update_now drain: a live session or
        # pending plugin task is never interrupted.
        ({"busy": True}, SafeMoment(False, "busy")),
        # The caller names why (finding 9: a Hermes background process).
        (
            {"busy": True, "busy_reason": "background_job"},
            SafeMoment(False, "background_job"),
        ),
        # Idle not yet held for the whole quiet window.
        ({"idle_since": None}, SafeMoment(False, "settling")),
        ({"idle_since": 10_000.0 - QUIET_SECONDS + 1}, SafeMoment(False, "settling")),
        # Quiet for 10 minutes since the last inbound or outbound message.
        (
            {"last_message_at": 10_000.0 - QUIET_SECONDS + 1},
            SafeMoment(False, "recent_message"),
        ),
        # A failed or attempted run waits out its backoff.
        ({"not_before": 10_001.0}, SafeMoment(False, "backoff")),
        ({"not_before": 10_000.0}, SafeMoment(True, "safe")),
        # Busy outranks everything else in the reason.
        ({"busy": True, "not_before": 10_001.0}, SafeMoment(False, "busy")),
    ],
)
def test_decide_safe_moment(overrides, expected) -> None:
    assert _moment(**overrides) == expected


def test_quiet_window_is_ten_minutes() -> None:
    assert QUIET_SECONDS == 10 * 60


# -----------------------------------------------------------------------------
# Attempt record (persists across restarts: the cap must survive the very
# restart it counts)
# -----------------------------------------------------------------------------


def test_attempts_missing_file_is_empty(tmp_path: Path) -> None:
    assert scheduled_update.load_attempts(tmp_path / "nope.json") == {}


def test_record_attempt_counts_per_target_and_resets_on_a_new_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attempts.json"
    assert scheduled_update.record_attempt(path, "0.30.1") == {"0.30.1": 1}
    assert scheduled_update.record_attempt(path, "0.30.1") == {"0.30.1": 2}
    assert scheduled_update.load_attempts(path) == {"0.30.1": 2}
    assert scheduled_update.record_attempt(path, "0.30.2") == {"0.30.2": 1}
    assert scheduled_update.load_attempts(path) == {"0.30.2": 1}


@pytest.mark.parametrize(
    "body",
    ["not json", "[]", '{"0.30.1": "x"}', '{"0.30.1": -1}', '{"0.30.1": true}'],
)
def test_corrupt_attempts_file_is_ignored(tmp_path: Path, body: str) -> None:
    path = tmp_path / "attempts.json"
    path.write_text(body, encoding="utf-8")
    assert scheduled_update.load_attempts(path) == {}


def test_record_attempt_survives_an_unwritable_home(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    # The parent is a FILE: the write fails, the count is still returned.
    assert scheduled_update.record_attempt(blocker / "attempts.json", "0.30.1") == {
        "0.30.1": 1,
    }


def test_attempts_path_is_in_the_process_hermes_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert scheduled_update.attempts_path() == tmp_path / scheduled_update.ATTEMPTS_FILENAME
    scheduled_update.record_attempt(scheduled_update.attempts_path(), "0.30.1")
    assert json.loads((tmp_path / scheduled_update.ATTEMPTS_FILENAME).read_text()) == {
        "0.30.1": 1,
    }
