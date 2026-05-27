"""Tests for the shared agent-catalog spec parser."""
from __future__ import annotations

import pytest

from hermes_channel_bgos.agents import enumerate_agents_from_env, parse_agents_spec


def test_parse_route_name_pairs():
    assert parse_agents_spec("hades:Hades,ramy:Ramy") == [
        {"agent_route": "hades", "name": "Hades"},
        {"agent_route": "ramy", "name": "Ramy"},
    ]


def test_parse_bare_route_uses_route_as_name():
    assert parse_agents_spec("default") == [
        {"agent_route": "default", "name": "default"},
    ]


def test_parse_strips_whitespace_and_skips_empty_pieces():
    assert parse_agents_spec(" a : Alpha , , b ") == [
        {"agent_route": "a", "name": "Alpha"},
        {"agent_route": "b", "name": "b"},
    ]


def test_parse_empty_string_is_empty_list():
    assert parse_agents_spec("") == []
    assert parse_agents_spec("   ") == []


def test_enumerate_prefers_json(monkeypatch):
    monkeypatch.setenv("BGOS_AGENTS_JSON", '[{"agent_route":"x","name":"X","description":"d"}]')
    monkeypatch.setenv("BGOS_AGENTS", "y:Y")
    out = enumerate_agents_from_env()
    assert out == [{"agent_route": "x", "name": "X", "description": "d"}]


def test_enumerate_falls_back_to_comma_spec(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.setenv("BGOS_AGENTS", "default:David")
    assert enumerate_agents_from_env() == [{"agent_route": "default", "name": "David"}]


def test_enumerate_ignores_invalid_json_and_uses_comma(monkeypatch):
    monkeypatch.setenv("BGOS_AGENTS_JSON", "{not json")
    monkeypatch.setenv("BGOS_AGENTS", "default:David")
    assert enumerate_agents_from_env() == [{"agent_route": "default", "name": "David"}]


def test_enumerate_empty_when_nothing_set(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.delenv("BGOS_AGENTS", raising=False)
    assert enumerate_agents_from_env() == []
