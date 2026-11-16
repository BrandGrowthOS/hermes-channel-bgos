"""Best-effort, write-time evidence for BGOS skill source marks."""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .hermes_profiles import resolve_hermes_home

log = logging.getLogger(__name__)

TEACH_APPROVE = "teach_approve"
TEACH_WINDOW_S = 900
_STAMP_ORIGINS = {"taught", "made"}
_teach_windows: dict[str, tuple[str, float]] = {}
_window_lock = threading.Lock()
_stamp_lock = threading.Lock()


def _chat_key(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError, OverflowError):
        return str(value).strip()


def open_teach_window(chat_id: Any, tapper: str, now: float) -> None:
    """Remember a tap already checked against the pairing owner by the adapter."""
    try:
        key = _chat_key(chat_id)
        if not key or not isinstance(tapper, str) or not tapper.strip():
            return
        with _window_lock:
            _teach_windows[key] = (tapper, now)
    except Exception:
        log.warning("Could not open a skill teach window")


def take_teach_window(chat_id: Any, actor: str, now: float) -> bool:
    """Consume one unexpired approval in this chat, for the same actor if known."""
    try:
        key = _chat_key(chat_id)
        with _window_lock:
            window = _teach_windows.get(key)
            if window is None:
                return False
            tapper, opened = window
            if not 0 <= now - opened <= TEACH_WINDOW_S:
                del _teach_windows[key]
                return False
            if actor and str(actor) != tapper:
                return False
            del _teach_windows[key]
            return True
    except Exception:
        log.warning("Could not consume a skill teach window")
        return False


def origins_path() -> Path | None:
    try:
        return resolve_hermes_home().expanduser() / "bgos_skill_origins.json"
    except Exception:
        log.warning("Could not locate skill origins")
        return None


def read_stamps(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("skills"), dict):
            raise ValueError("Invalid skill origins")
        return data["skills"]
    except FileNotFoundError:
        return {}
    except Exception:
        log.warning("Could not read skill origins")
        return {}


def write_stamp(path: Path | None, rel_dir: str, origin: str, ident: str) -> None:
    """Atomically replace the sidecar, never a file inside a skill folder."""
    temporary: Path | None = None
    try:
        if path is None or origin not in _STAMP_ORIGINS or not isinstance(ident, str) or not ident:
            return
        if not isinstance(rel_dir, str) or not rel_dir or Path(rel_dir).is_absolute():
            return
        if "\\" in rel_dir or any(part in {"", ".", ".."} for part in rel_dir.split("/")):
            return
        with _stamp_lock:
            stamps = read_stamps(path)
            stamps[rel_dir] = {"origin": origin, "at": time.time(), "ident": ident}
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"v": 1, "skills": stamps}, stream)
            os.replace(temporary, path)
            temporary = None
    except Exception:
        log.warning("Could not write a skill origin")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                # The write failure above already logged the operation's failure.
                pass


def _real_directory(path: Path) -> os.stat_result | None:
    info = path.lstat()
    # Junctions on Windows are links even when S_ISLNK is false.
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not stat.S_ISDIR(info.st_mode) or reparse:
        return None
    return info


def folder_identity(path: Path) -> str | None:
    try:
        info = _real_directory(path)
        return f"{info.st_dev}:{info.st_ino}" if info is not None else None
    except Exception:
        log.warning("Could not read a skill folder identity")
        return None


def find_skill_dir(skills_dir: Path, name: str) -> Path | None:
    """Accept one real folder with this leaf name, without following directory links."""
    try:
        if not isinstance(name, str) or not name or not skills_dir.exists():
            return None
        if _real_directory(skills_dir) is None:
            return None
        matches = []

        def on_error(error: OSError) -> None:
            raise error

        for root, dirs, files in os.walk(skills_dir, followlinks=False, onerror=on_error):
            folder = Path(root)
            dirs[:] = [entry for entry in dirs if _real_directory(folder / entry) is not None]
            if folder.name == name and "SKILL.md" in files and (folder / "SKILL.md").is_file():
                matches.append(folder)
                if len(matches) > 1:
                    return None
        return matches[0] if matches else None
    except Exception:
        log.warning("Could not locate a skill folder")
        return None


def on_skill_lifecycle(**kwargs: Any) -> None:
    try:
        if kwargs.get("action") != "created":
            return
        provenance = kwargs.get("provenance")
        if provenance == "agent_created":
            origin = "made"
        elif provenance == "local":
            # Older Hermes and writes replayed outside a session provide no proof.
            from gateway.session_context import get_session_env

            if get_session_env("HERMES_SESSION_PLATFORM") != "bgos":
                return
            if not take_teach_window(
                get_session_env("HERMES_SESSION_CHAT_ID"),
                get_session_env("HERMES_SESSION_USER_ID"), time.time(),
            ):
                return
            origin = "taught"
        else:
            return
        skills_dir = resolve_hermes_home().expanduser() / "skills"
        folder = find_skill_dir(skills_dir, kwargs.get("skill_name"))
        if folder is None:
            return
        ident = folder_identity(folder)
        if ident is not None:
            write_stamp(origins_path(), folder.relative_to(skills_dir).as_posix(), origin, ident)
    except Exception:
        log.warning("Could not record a skill lifecycle origin")


def origin_for(rel_dir: str, ident: str | None, hub_or_bundled: bool, stamps: dict[str, Any]) -> str | None:
    try:
        if hub_or_bundled:
            return "installed"
        stamp = stamps.get(rel_dir)
        if isinstance(stamp, dict) and ident is not None and stamp.get("ident") == ident:
            origin = stamp.get("origin")
            if origin in _STAMP_ORIGINS:
                return origin
        return None
    except Exception:
        log.warning("Could not read a skill origin")
        return None
