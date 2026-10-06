"""The README is the canonical partner runbook: what it says about self
updates must match the code. An operator who follows it must learn that
Hermes updates itself on its own schedule, independent of the HOAI app's
Keep agents running switch, and the off value it names must really turn
updates off.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_channel_bgos import self_update

README = Path(__file__).resolve().parents[1] / "README.md"


def _updates_at_idle_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Updates at idle")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_readme_says_hermes_updates_itself_independent_of_the_switch() -> None:
    section = _updates_at_idle_section()
    assert "on its own schedule" in section
    assert "verified supervisor" in section
    assert "Keep agents running" in section
    assert "BGOS_AUTO_UPDATE=off" in section


def test_the_documented_off_value_really_turns_updates_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = set(re.findall(r"BGOS_AUTO_UPDATE=(\w+)", README.read_text(encoding="utf-8")))
    assert "off" in values
    monkeypatch.setenv("BGOS_AUTO_UPDATE", "off")
    assert self_update.auto_update_enabled() is False
