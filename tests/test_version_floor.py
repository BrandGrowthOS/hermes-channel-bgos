"""The version this package ships as is a promise the backend reads.

The backend gates what it tells an agent by its connector's version
(backend/src/integrations/capability-canon.ts on BGOS main). Its work notes
floor for Hermes (WORK_NOTES_MIN_DAEMON) is 0.33.0: from 0.33.0 on it treats
a Hermes as one that posts work notes itself, and tells its agent not to post
its own. A release from 0.33.0 on that posts none leaves the owner with no
work note at all. So until this package ships work notes, it stays below.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from hermes_channel_bgos import __version__

WORK_NOTES_MIN_DAEMON = (0, 33, 0)
REPO = Path(__file__).resolve().parent.parent


def _triple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split(".")[:3])
    return major, minor, patch


def test_a_release_without_work_notes_stays_below_the_backends_work_notes_floor():
    ships_work_notes = (
        importlib.util.find_spec("hermes_channel_bgos.work_notes") is not None
    )
    assert ships_work_notes or _triple(__version__) < WORK_NOTES_MIN_DAEMON, (
        f"{__version__} would tell the backend this Hermes posts work notes"
    )


def test_the_changelogs_top_section_is_the_version_this_package_ships_as():
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    top = re.search(r"^## (\S+)", text, re.MULTILINE)
    assert top is not None
    assert top.group(1) == __version__
