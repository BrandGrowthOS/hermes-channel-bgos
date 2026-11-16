"""Write-time evidence for optional skill origin marks."""
from __future__ import annotations

import importlib
import json
import sys
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
    origins.open_teach_window(opened, "owner", 100)
    assert origins.take_teach_window(taken, "owner", 101)
    assert not origins.take_teach_window(taken, "owner", 102)


def test_window_refuses_other_actor_and_other_chat():
    origins = _origins()
    origins.open_teach_window("7", "owner", 100)
    assert not origins.take_teach_window("8", "owner", 101)
    assert not origins.take_teach_window("7", "other", 101)
    assert origins.take_teach_window("7", "owner", 102)


@pytest.mark.parametrize("elapsed,valid", [(899, True), (900, True), (901, False)])
def test_window_expires_after_fifteen_minutes(elapsed, valid):
    origins = _origins()
    origins.open_teach_window(7, "owner", 100)
    assert bool(origins.take_teach_window(7, "owner", 100 + elapsed)) is valid
    assert not origins.take_teach_window(7, "owner", 100 + elapsed)


def test_window_allows_absent_session_actor():
    origins = _origins()
    origins.open_teach_window(7, "owner", 100)
    assert origins.take_teach_window(7, "", 101)


def test_window_is_consumed_once_across_threads():
    origins = _origins()
    origins.open_teach_window(7, "owner", 100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: bool(origins.take_teach_window(7, "owner", 101)), range(24)))
    assert sum(results) == 1


@pytest.mark.parametrize("chat,tapper", [(None, "owner"), ("", "owner"), (7, ""), (7, None)])
def test_window_refuses_missing_facts(chat, tapper):
    origins = _origins()
    origins.open_teach_window(chat, tapper, 100)
    assert not origins.take_teach_window(chat, "", 101)


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

    def fail_replace(*_args):
        raise OSError("fixture failure")

    monkeypatch.setattr(origins.os, "replace", fail_replace)
    caplog.clear()
    origins.write_stamp(path, "second", "taught", "1:3")
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
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
    origins.open_teach_window(7, "owner", 100)
    origins.on_skill_lifecycle(action="created", skill_name="example", provenance="local", task_id="task")
    origins.on_skill_lifecycle(action="created", skill_name="second", provenance="local")
    stamps = origins.read_stamps(origins.origins_path())
    assert origins.origin_for("category/example", _ident(folder), False, stamps) == "taught"
    assert "second" not in stamps


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
        origins.open_teach_window(7, "owner", 100)
    origins.on_skill_lifecycle(action=action, skill_name="example", provenance=provenance)
    assert origins.read_stamps(origins.origins_path()) == {}


def test_missing_session_module_does_not_stamp(tmp_path, monkeypatch):
    origins = _origins()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _skill(tmp_path / "skills")
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    origins.open_teach_window(7, "owner", 100)
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
