"""Write-time evidence for optional skill origin marks."""
from __future__ import annotations

import importlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest


def _origins():
    # Fresh process-local windows without reaching into the implementation.
    return importlib.reload(importlib.import_module("hermes_channel_bgos.skill_origins"))


def _skill(root, rel="category/example"):
    folder = root / rel
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    return folder


def _ident(folder):
    info = folder.stat()
    return f"{info.st_dev}:{info.st_ino}"


def _session(monkeypatch, *, platform="bgos", chat="007", actor="owner"):
    import contextvars
    from hermes_channel_bgos import bgos_adapter

    monkeypatch.setattr(bgos_adapter, "_TURN_SPEAKER", contextvars.ContextVar("test_speaker", default=(int(chat), 7)))
    gateway = ModuleType("gateway")
    gateway.__path__ = []
    context = ModuleType("gateway.session_context")
    values = {
        "HERMES_SESSION_PLATFORM": platform,
        "HERMES_SESSION_CHAT_ID": chat,
        "HERMES_SESSION_USER_ID": actor,
    }
    context.get_session_env = lambda name: values.get(name, "")
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", context)


@pytest.mark.parametrize("opened,taken", [(7, "007"), (" 007 ", 7), (" chat ", "chat")])
def test_window_normalizes_chat_and_is_one_use(opened, taken):
    origins = _origins()
    assert origins.TEACH_APPROVE == "teach_approve"
    assert origins.TEACH_WINDOW_S == 900
    origins.open_teach_window(opened, "owner", 100, assistant_id=7)
    assert origins.take_teach_window(taken, "owner", 101, assistant_id=7)
    assert not origins.take_teach_window(taken, "owner", 102, assistant_id=7)


def test_window_refuses_other_actor_and_other_chat():
    origins = _origins()
    origins.open_teach_window("7", "owner", 100, assistant_id=7)
    assert not origins.take_teach_window("8", "owner", 101, assistant_id=7)
    assert not origins.take_teach_window("7", "other", 101, assistant_id=7)
    assert origins.take_teach_window("7", "owner", 102, assistant_id=7)


@pytest.mark.parametrize("elapsed,valid", [(899, True), (900, True), (901, False)])
def test_window_expires_after_fifteen_minutes(elapsed, valid):
    origins = _origins()
    origins.open_teach_window(7, "owner", 100, assistant_id=7)
    assert bool(origins.take_teach_window(7, "owner", 100 + elapsed, assistant_id=7)) is valid
    assert not origins.take_teach_window(7, "owner", 100 + elapsed, assistant_id=7)


def test_window_allows_absent_session_actor():
    origins = _origins()
    origins.open_teach_window(7, "owner", 100, assistant_id=7)
    assert origins.take_teach_window(7, "", 101, assistant_id=7)


def test_window_is_consumed_once_across_threads():
    origins = _origins()
    origins.open_teach_window(7, "owner", 100, assistant_id=7)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: bool(origins.take_teach_window(7, "owner", 101, assistant_id=7)), range(24)))
    assert sum(results) == 1


@pytest.mark.parametrize("chat,tapper", [(None, "owner"), ("", "owner"), (7, ""), (7, None)])
def test_window_refuses_missing_facts(chat, tapper):
    origins = _origins()
    origins.open_teach_window(chat, tapper, 100, assistant_id=7)
    assert not origins.take_teach_window(chat, "", 101, assistant_id=7)


def test_stamp_round_trip_outside_skill_and_identity_match(tmp_path, monkeypatch):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    folder = _skill(tmp_path / "skills")
    path = origins.origins_path()
    assert path == tmp_path / "bgos_skill_origins.json"
    before = (folder / "SKILL.md").read_bytes()
    origins.write_stamp(path, "category/example", "taught", _ident(folder))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["v"] == 1
    assert set(raw["skills"]["category/example"]) == {"origin", "at", "ident"}
    stamps = origins.read_stamps(path)
    assert origins.origin_for("category/example", _ident(folder), False, stamps) == "taught"
    assert origins.origin_for("category/example", "different:identity", False, stamps) is None
    assert origins.origin_for("category/example", None, False, stamps) is None
    assert (folder / "SKILL.md").read_bytes() == before
    assert list(folder.iterdir()) == [folder / "SKILL.md"]


def test_replaced_folder_loses_stamp(tmp_path):
    origins = _origins()
    folder = _skill(tmp_path / "skills", "example")
    path = tmp_path / "origins.json"
    old_ident = _ident(folder)
    origins.write_stamp(path, "example", "made", old_ident)
    # Keep the old inode allocated so the filesystem cannot immediately reuse it.
    folder.rename(tmp_path / "old-example")
    replacement = _skill(tmp_path / "skills", "example")
    assert _ident(replacement) != old_ident
    assert origins.origin_for("example", _ident(replacement), False, origins.read_stamps(path)) is None


@pytest.mark.parametrize("provenance,platform,actor,window", [
    ("local", "bgos", "owner", False),
    ("local", "other", "owner", True),
    ("local", "bgos", "other", True),
    ("local", None, "owner", True),
    ("installed", "bgos", "owner", True),
    (None, "bgos", "owner", True),
])
def test_unproven_create_drops_stamp_even_with_reused_identity(tmp_path, monkeypatch, provenance, platform, actor, window):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    folder = _skill(tmp_path / "skills")
    reused_ident = _ident(folder)
    origins.write_stamp(origins.origins_path(), "category/example", "taught", reused_ident)
    (folder / "SKILL.md").unlink()
    folder.rmdir()
    _skill(tmp_path / "skills")
    # Force the inode-reuse case independently of this filesystem's allocator.
    monkeypatch.setattr(origins, "folder_identity", lambda _folder: reused_ident)
    _session(monkeypatch, platform=platform, actor=actor)
    if platform is None:
        monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    monkeypatch.setattr(origins.time, "time", lambda: 101)
    if window:
        origins.open_teach_window(7, "owner", 100, assistant_id=7)
    origins.on_skill_lifecycle(action="created", skill_name="example", provenance=provenance)
    stamps = origins.read_stamps(origins.origins_path())
    assert "category/example" not in stamps
    assert origins.origin_for("category/example", reused_ident, False, stamps) is None


@pytest.mark.parametrize("contents", ["{", "[]", '{"v": 2, "skills": {}}', '{"v": 1, "skills": []}'])
def test_bad_sidecar_means_no_stamps(tmp_path, contents):
    origins = _origins()
    path = tmp_path / "origins.json"
    assert origins.read_stamps(path) == {}
    path.write_text(contents, encoding="utf-8")
    assert origins.read_stamps(path) == {}


def test_failed_atomic_replace_keeps_old_stamps(tmp_path, monkeypatch, caplog):
    origins = _origins()
    path = tmp_path / "origins.json"
    origins.write_stamp(path, "first", "made", "1:2")
    before = path.read_bytes()
    before_entries = set(tmp_path.iterdir())

    def fail_replace(*_args):
        raise OSError("fixture failure")

    monkeypatch.setattr(origins.os, "replace", fail_replace)
    caplog.clear()
    origins.write_stamp(path, "second", "taught", "1:3")
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == before_entries
    assert len(caplog.records) == 1


def test_find_skill_requires_a_single_real_folder(tmp_path):
    origins = _origins()
    assert origins.find_skill_dir(tmp_path, "example") is None
    folder = _skill(tmp_path)
    assert origins.find_skill_dir(tmp_path, "example") == folder
    _skill(tmp_path, "other/example")
    assert origins.find_skill_dir(tmp_path, "example") is None


def test_find_skill_refuses_linked_folder(tmp_path):
    origins = _origins()
    folder = _skill(tmp_path / "outside", "example")
    root = tmp_path / "skills"
    root.mkdir()
    link = root / "example"
    try:
        link.symlink_to(folder, target_is_directory=True)
    except OSError:
        # Windows junctions are directory links too and need no special privilege.
        import _winapi
        _winapi.CreateJunction(str(folder), str(link))
    assert origins.find_skill_dir(root, "example") is None


@pytest.mark.parametrize("hidden", [".archive", ".hub", ".git", ".venv", ".custom"])
def test_find_skill_prunes_hidden_trees(tmp_path, monkeypatch, hidden):
    origins = _origins()
    folder = _skill(tmp_path)
    _skill(tmp_path, f"{hidden}/example")
    visited = []
    real_walk = origins.os.walk

    def walk(*args, **kwargs):
        for entry in real_walk(*args, **kwargs):
            visited.append(Path(entry[0]))
            yield entry

    monkeypatch.setattr(origins.os, "walk", walk)
    assert origins.find_skill_dir(tmp_path, "example") == folder
    assert tmp_path / hidden not in visited


def test_linked_skills_root_is_refused_but_real_root_still_works(tmp_path):
    origins = _origins()
    root = tmp_path / "skills"
    folder = _skill(root)
    link = tmp_path / "linked-skills"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        import _winapi
        _winapi.CreateJunction(str(root), str(link))
    assert origins.find_skill_dir(link, "example") is None
    assert origins.find_skill_dir(root, "example") == folder


@pytest.mark.parametrize("origin", ["taught", "made"])
def test_installer_evidence_wins_over_stamp(origin):
    origins = _origins()
    stamps = {"example": {"origin": origin, "ident": "1:2"}}
    assert origins.origin_for("example", "1:2", True, stamps) == "installed"
    assert origins.origin_for("example", "1:2", False, stamps) == origin
    assert origins.origin_for("plain", "1:2", False, stamps) is None


def test_unknown_stamp_value_is_ignored():
    origins = _origins()
    assert origins.origin_for("example", "1:2", False, {"example": {"origin": "other", "ident": "1:2"}}) is None


def test_created_by_agent_stamps_made(tmp_path, monkeypatch):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    folder = _skill(tmp_path / "skills")
    origins.on_skill_lifecycle(action="created", skill_name="example", provenance="agent_created")
    assert origins.origin_for("category/example", _ident(folder), False, origins.read_stamps(origins.origins_path())) == "made"


def test_local_create_consumes_owner_approval(tmp_path, monkeypatch):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    folder = _skill(tmp_path / "skills")
    _skill(tmp_path / "skills", "second")
    _session(monkeypatch)
    monkeypatch.setattr(origins.time, "time", lambda: 101)
    origins.open_teach_window(7, "owner", 100, assistant_id=7)
    origins.on_skill_lifecycle(action="created", skill_name="example", provenance="local", task_id="task")
    origins.on_skill_lifecycle(action="created", skill_name="second", provenance="local")
    stamps = origins.read_stamps(origins.origins_path())
    assert origins.origin_for("category/example", _ident(folder), False, stamps) == "taught"
    assert "second" not in stamps


def test_two_agents_in_one_chat_do_not_share_approval(tmp_path, monkeypatch):
    from hermes_channel_bgos.bgos_adapter import _speaking_as

    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skill(tmp_path / "skills", "first")
    _skill(tmp_path / "skills", "second")
    _session(monkeypatch)
    monkeypatch.setattr(origins.time, "time", lambda: 101)
    origins.open_teach_window("007", "owner", 100, assistant_id="011")
    with _speaking_as(7, 22):
        origins.on_skill_lifecycle(action="created", skill_name="second", provenance="local")
    assert origins.read_stamps(origins.origins_path()) == {}
    with _speaking_as(7, 11):
        origins.on_skill_lifecycle(action="created", skill_name="first", provenance="local")
    stamps = origins.read_stamps(origins.origins_path())
    assert stamps["first"]["origin"] == "taught"
    assert "second" not in stamps


@pytest.mark.parametrize("speaker", [None, (8, 11)])
def test_missing_or_other_chat_speaker_cannot_take_approval(tmp_path, monkeypatch, speaker):
    import contextvars
    from hermes_channel_bgos import bgos_adapter

    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skill(tmp_path / "skills")
    _session(monkeypatch)
    monkeypatch.setattr(bgos_adapter, "_TURN_SPEAKER", contextvars.ContextVar("test_speaker", default=speaker))
    monkeypatch.setattr(origins.time, "time", lambda: 101)
    origins.open_teach_window(7, "owner", 100, assistant_id=11)
    origins.on_skill_lifecycle(action="created", skill_name="example", provenance="local")
    assert origins.read_stamps(origins.origins_path()) == {}
    assert origins.take_teach_window(7, "owner", 101, assistant_id=11)


@pytest.mark.parametrize("action,provenance,platform,actor,window", [
    ("created", "local", "bgos", "owner", False),
    ("created", "local", "other", "owner", True),
    ("created", "local", "bgos", "other", True),
    ("edited", "agent_created", "bgos", "owner", True),
    ("patched", "local", "bgos", "owner", True),
    ("created", "installed", "bgos", "owner", True),
])
def test_lifecycle_without_proof_does_not_stamp(tmp_path, monkeypatch, action, provenance, platform, actor, window):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skill(tmp_path / "skills")
    _session(monkeypatch, platform=platform, actor=actor)
    monkeypatch.setattr(origins.time, "time", lambda: 101)
    if window:
        origins.open_teach_window(7, "owner", 100, assistant_id=7)
    origins.on_skill_lifecycle(action=action, skill_name="example", provenance=provenance)
    assert origins.read_stamps(origins.origins_path()) == {}


def test_missing_session_module_does_not_stamp(tmp_path, monkeypatch):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skill(tmp_path / "skills")
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    origins.open_teach_window(7, "owner", 100, assistant_id=7)
    origins.on_skill_lifecycle(action="created", skill_name="example", provenance="local")
    assert origins.read_stamps(origins.origins_path()) == {}


@pytest.mark.parametrize("kwargs", [{}, {"action": []}, {"action": "created"},
    {"action": "created", "provenance": "agent_created", "skill_name": []},
    {"action": "created", "provenance": {}, "skill_name": "example"}])
def test_bad_lifecycle_kwargs_never_raise(tmp_path, monkeypatch, kwargs):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skill(tmp_path / "skills")
    origins.on_skill_lifecycle(**kwargs)
    assert origins.read_stamps(origins.origins_path()) == {}


def test_clock_rollback_cannot_use_approval():
    origins = _origins()
    origins.open_teach_window(7, "owner", 100, assistant_id=11)
    assert not origins.take_teach_window(7, "owner", 99, assistant_id=11)


@pytest.mark.parametrize("relative", ["", ".", "..", "../example", "a/../example", "a//example", "a\\example", "/example"])
def test_write_stamp_refuses_invalid_relative_dir(tmp_path, relative):
    origins = _origins()
    path = tmp_path / "origins.json"
    origins.write_stamp(path, relative, "made", "1:2")
    assert not path.exists()


def test_write_stamp_refuses_absolute_dir(tmp_path):
    origins = _origins()
    path = tmp_path / "origins.json"
    origins.write_stamp(path, str(tmp_path / "example"), "made", "1:2")
    assert not path.exists()


@pytest.mark.parametrize("origin", ["installed", "shared", "other", None, [], {}])
def test_write_stamp_refuses_unproven_origin(tmp_path, origin):
    origins = _origins()
    path = tmp_path / "origins.json"
    origins.write_stamp(path, "example", origin, "1:2")
    assert not path.exists()


def test_named_directory_without_skill_file_is_not_a_skill(tmp_path):
    origins = _origins()
    (tmp_path / "example").mkdir()
    assert origins.find_skill_dir(tmp_path, "example") is None


def test_find_skill_rechecks_file_after_walk_snapshot(tmp_path, monkeypatch):
    origins = _origins()
    folder = _skill(tmp_path, "example")
    real_walk = origins.os.walk

    def walk(*args, **kwargs):
        for root, dirs, files in real_walk(*args, **kwargs):
            if Path(root) == folder:
                (folder / "SKILL.md").unlink()
                (folder / "SKILL.md").mkdir()
            yield root, dirs, files

    monkeypatch.setattr(origins.os, "walk", walk)
    assert origins.find_skill_dir(tmp_path, "example") is None


def test_pruned_link_is_not_followed_if_replaced_during_walk(tmp_path, monkeypatch):
    origins = _origins()
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "category"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    real_walk = origins.os.walk

    def walk(*args, **kwargs):
        for entry in real_walk(*args, **kwargs):
            yield entry
            if Path(entry[0]) == root:
                # The enumerated link becomes a real tree before walk recurses.
                if link.is_symlink():
                    link.unlink()
                else:
                    link.rmdir()  # Remove only the fixture junction, not its target.
                _skill(root, "category/example")

    monkeypatch.setattr(origins.os, "walk", walk)
    assert origins.find_skill_dir(root, "example") is None


def test_window_consumption_serializes_read_and_delete(monkeypatch):
    origins = _origins()
    origins.open_teach_window(7, "owner", 100, assistant_id=11)
    first_read = threading.Event()
    release_read = threading.Event()
    overlap = threading.Event()
    second_started = threading.Event()

    class PausedRead(dict):
        def get(self, *args):
            value = super().get(*args)
            if not first_read.is_set():
                first_read.set()
                assert release_read.wait(5)
            elif not release_read.is_set():
                overlap.set()
            return value

    monkeypatch.setattr(origins, "_teach_windows", PausedRead(origins._teach_windows))

    def take(second=False):
        if second:
            second_started.set()
        return origins.take_teach_window(7, "owner", 101, assistant_id=11)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(take)
        assert first_read.wait(5)
        second = pool.submit(take, True)
        try:
            assert second_started.wait(5)
            overlapped = overlap.wait(0.5)
        finally:
            release_read.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert not overlapped
    assert sum(results) == 1


def test_stamp_writers_preserve_both_updates(tmp_path, monkeypatch):
    origins = _origins()
    path = tmp_path / "origins.json"
    first_read = threading.Event()
    release_read = threading.Event()
    overlap = threading.Event()
    second_started = threading.Event()
    read_stamps = origins.read_stamps

    def read(file):
        stamps = read_stamps(file)
        if not first_read.is_set():
            first_read.set()
            assert release_read.wait(5)
        elif not release_read.is_set():
            overlap.set()
        return stamps

    monkeypatch.setattr(origins, "read_stamps", read)

    def write(name):
        if name == "second":
            second_started.set()
        origins.write_stamp(path, name, "made", "1:2")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "first")
        assert first_read.wait(5)
        second = pool.submit(write, "second")
        try:
            assert second_started.wait(5)
            overlap.wait(0.5)
        finally:
            release_read.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert set(read_stamps(path)) == {"first", "second"}


def _stamp_in_process(path, name, first_read, release_read, second_read, started):
    origins = importlib.import_module("hermes_channel_bgos.skill_origins")
    real_read = origins.read_stamps

    def paused_read(file):
        stamps = real_read(file)
        if name == "first":
            first_read.set()
            assert release_read.wait(15)
        else:
            second_read.set()
        return stamps

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(origins, "read_stamps", paused_read)
        started.set()
        origins.write_stamp(Path(path), name, "made", "1:2")


def test_two_process_writers_preserve_both_stamps(tmp_path):
    import multiprocessing

    origins = _origins()
    path = tmp_path / "origins.json"
    context = multiprocessing.get_context("spawn")
    first_read, release_read, second_read = [context.Event() for _ in range(3)]
    starts = [context.Event(), context.Event()]
    writers = [
        context.Process(target=_stamp_in_process, args=(str(path), name, first_read, release_read, second_read, starts[index]))
        for index, name in enumerate(("first", "second"))
    ]
    started = []
    try:
        writers[0].start()
        started.append(writers[0])
        assert first_read.wait(15)
        writers[1].start()
        started.append(writers[1])
        assert starts[1].wait(15)
        # Without a process lock, the second writer reads the same old snapshot.
        if second_read.wait(2):
            writers[1].join(timeout=5)
            assert writers[1].exitcode == 0
        release_read.set()
        for writer in writers:
            writer.join(timeout=15)
            assert writer.exitcode == 0
    finally:
        release_read.set()
        for writer in started:
            if writer.is_alive():
                writer.terminate()
            writer.join(timeout=5)
    assert set(origins.read_stamps(path)) == {"first", "second"}
