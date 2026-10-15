"""Per-agent persistent inbound cursors (cursors.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_channel_bgos.cursors import AgentCursors


@pytest.fixture
def cursors(tmp_path: Path) -> AgentCursors:
    return AgentCursors(tmp_path / "bgos_last_ids.json", tmp_path / "bgos_last_id")


def _file(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "bgos_last_ids.json").read_text())


def test_an_agent_with_no_cursor_starts_at_zero_on_a_fresh_install(cursors: AgentCursors) -> None:
    assert cursors.get(7) == 0
    assert cursors.since([7, 8]) == 0


def test_each_agent_keeps_its_own_cursor(cursors: AgentCursors, tmp_path: Path) -> None:
    cursors.since([1, 2])  # both known from the start
    cursors.save(1, 500)
    cursors.save(2, 300)
    assert cursors.get(1) == 500
    assert cursors.get(2) == 300
    # The poll starts from the LOWEST of the agents it serves.
    assert cursors.since([1, 2]) == 300
    assert _file(tmp_path)["agents"] == {"1": 500, "2": 300}


def test_a_cursor_never_regresses_and_rejects_bad_ids(cursors: AgentCursors) -> None:
    cursors.save(1, 100)
    for bad in (50, 0, -5, 3.14, None, True):
        cursors.save(1, bad)  # type: ignore[arg-type]
    assert cursors.get(1) == 100


def test_migration_every_agent_starts_from_the_old_single_cursor(
    cursors: AgentCursors, tmp_path: Path,
) -> None:
    # The pre per-agent install: one integer in bgos_last_id.
    (tmp_path / "bgos_last_id").write_text("480")

    # First start under the new code: both agents sit exactly at the old value.
    assert cursors.get(1) == 480
    assert cursors.get(2) == 480
    assert cursors.since([1, 2]) == 480  # not replayed: polls past 480
    # ...and each agent now has its own entry; the migration is one event.
    assert _file(tmp_path) == {"version": 1, "head": 480, "agents": {"1": 480, "2": 480}}

    # Agent 1 moves on; agent 2 keeps the migrated value, so its next message
    # (> 480) is still fetched.
    cursors.save(1, 520)
    assert cursors.get(1) == 520
    assert cursors.get(2) == 480
    assert cursors.since([1, 2]) == 480

    # The old file is not consulted again once the new one exists.
    (tmp_path / "bgos_last_id").write_text("9999")
    assert cursors.get(2) == 480


def test_migration_a_live_message_before_the_first_poll_cannot_move_another_agent(
    cursors: AgentCursors, tmp_path: Path,
) -> None:
    (tmp_path / "bgos_last_id").write_text("480")

    cursors.save(1, 520)  # agent 1's live message, before any poll

    assert cursors.get(2) == 480
    assert cursors.since([1, 2]) == 480
    assert cursors.get(2) == 480


def test_an_agent_exposed_after_the_first_poll_starts_at_the_head(
    cursors: AgentCursors, tmp_path: Path,
) -> None:
    (tmp_path / "bgos_last_id").write_text("480")
    assert cursors.since([1]) == 480
    cursors.save(1, 90000)
    # Agent 7 is exposed much later: its old rows are history, as they were for
    # the single cursor.
    assert cursors.get(7) == 90000
    assert cursors.since([1, 7]) == 90000


def test_a_corrupt_cursor_file_migrates_again_and_never_raises(
    cursors: AgentCursors, tmp_path: Path,
) -> None:
    (tmp_path / "bgos_last_id").write_text("77")
    (tmp_path / "bgos_last_ids.json").write_text("{not json")
    assert cursors.get(1) == 77


def test_a_bad_old_cursor_file_is_zero(cursors: AgentCursors, tmp_path: Path) -> None:
    (tmp_path / "bgos_last_id").write_text("not-a-number")
    assert cursors.get(1) == 0


def test_seed_all_moves_the_head_and_every_given_agent(cursors: AgentCursors) -> None:
    cursors.seed_all([1, 2], 900)
    assert cursors.get(1) == 900
    assert cursors.get(2) == 900
    assert cursors.get(3) == 900  # a later agent starts at the new head


def test_advance_idle_moves_only_the_agents_it_is_given(cursors: AgentCursors) -> None:
    cursors.since([1, 2])
    cursors.save(1, 400)  # busy agent
    cursors.save(2, 100)
    cursors.advance_idle([2], 400)  # agent 2 had no row up to 400
    assert cursors.get(2) == 400
    assert cursors.since([1, 2]) == 400


def test_the_reader_answers_from_one_read(cursors: AgentCursors) -> None:
    cursors.since([1, 2])
    cursors.save(1, 10)
    read = cursors.reader()
    cursors.save(1, 20)  # after the snapshot
    assert read(1) == 10
    assert read(2) == 0


def test_the_write_is_atomic_no_tempfile_left(cursors: AgentCursors, tmp_path: Path) -> None:
    cursors.save(1, 5)
    assert [p.name for p in tmp_path.iterdir()] == ["bgos_last_ids.json"]


def test_migration_a_poll_with_no_known_agents_keeps_the_old_cursor_for_the_agents_that_come(
    cursors: AgentCursors, tmp_path: Path,
) -> None:
    (tmp_path / "bgos_last_id").write_text("3000")
    assert cursors.since([]) == 3000  # nothing exposed yet
    cursors.save(1, 3500)  # a live message moves the head past it
    assert cursors.get(2) == 3000
    assert cursors.since([1, 2]) == 3000


def test_a_pending_row_clamps_every_cursor_and_the_head_below_it(
    cursors: AgentCursors,
) -> None:
    cursors.since([1, 2])
    cursors.record_pending(9, 150)

    cursors.save(1, 300)  # a sibling consumes a later row
    cursors.advance_idle([2], 300)
    cursors.seed_all([1, 2], 400)

    assert cursors.get(1) == 149
    assert cursors.get(2) == 149
    assert cursors.head() == 149


def test_the_poll_window_reaches_back_to_a_pending_row(cursors: AgentCursors) -> None:
    cursors.since([1])
    cursors.save(1, 80)
    cursors.record_pending(9, 150)
    assert cursors.since([1]) == 80  # already below
    cursors.save(1, 90)
    assert cursors.since([1]) == 90
    cursors.record_pending(9, 60)
    assert cursors.since([1]) == 59


def test_an_agent_bound_later_starts_below_its_own_pending_row(
    cursors: AgentCursors,
) -> None:
    cursors.since([1])
    cursors.save(1, 200)  # head 200
    cursors.record_pending(9, 150)
    assert cursors.get(9) == 149  # not the head
    assert cursors.since([1, 9]) == 149


def test_clearing_a_pending_row_releases_the_clamp(cursors: AgentCursors) -> None:
    cursors.since([1])
    cursors.record_pending(9, 150)
    cursors.save(1, 300)
    assert cursors.get(1) == 149

    cursors.clear_pending(9, 150)
    cursors.save(1, 300)

    assert cursors.get(1) == 300


def test_pending_rows_survive_a_restart_and_age_out(
    cursors: AgentCursors, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as _time

    cursors.record_pending(9, 150)
    again = AgentCursors(tmp_path / "bgos_last_ids.json", tmp_path / "bgos_last_id")
    assert again.lowest_pending() == 150  # a new process still sees it

    real = _time.time()
    monkeypatch.setattr("hermes_channel_bgos.cursors.time.time", lambda: real + 7200)
    assert again.lowest_pending() is None  # an agent never bound cannot pin forever


def test_the_pending_list_is_bounded(cursors: AgentCursors) -> None:
    for i in range(1, 61):
        cursors.record_pending(9, i)
    assert cursors.lowest_pending() == 11  # the oldest 10 were dropped


def test_the_transient_hole_keys_never_reach_the_cursor_file(
    cursors: AgentCursors, tmp_path: Path,
) -> None:
    cursors.record_pending(9, 150)
    cursors.since([1])
    cursors.save(1, 10)
    assert set(_file(tmp_path)) <= {"version", "head", "agents", "migrating"}
