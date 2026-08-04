"""Unit tests for the install-time topology guard (topology.py, 0.23.0).

Each check maps to one of the misconfigurations that had to be repaired BY
HAND on the operator's machine after the 0.22.0 routing fix shipped:
missing per-route profile, multiplexing off, stray per-profile pairing
file, duplicate active pairing, a threat-scanner-blocked SOUL.md, and
sessions replaying a stored [BLOCKED: prompt.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_channel_bgos.topology import (
    FAIL,
    WARN,
    blocked_soul_findings,
    distinct_routes,
    find_profile_secrets,
    local_topology_findings,
    overlapping_pairing_findings,
    profile_dir,
    resolve_multiplex_flag,
    stale_blocked_session_finding,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "hermes_home"


def _profile(home: Path, name: str, *, soul: str | None = "I am someone.") -> Path:
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    if soul is not None:
        (d / "SOUL.md").write_text(soul)
    return d


# -----------------------------------------------------------------------------
# route + profile primitives
# -----------------------------------------------------------------------------


def test_distinct_routes_normalizes_and_drops_blanks():
    assert distinct_routes(["Shadow", " shadow ", "", None, "default"]) == {
        "shadow", "default",
    }


def test_profile_dir_default_is_home(home: Path):
    assert profile_dir(home, "default") == home
    assert profile_dir(home, "Shadow") == home / "profiles" / "shadow"


# -----------------------------------------------------------------------------
# resolve_multiplex_flag
# -----------------------------------------------------------------------------


def test_multiplex_env_override_wins(home: Path):
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: false\n")
    assert resolve_multiplex_flag(home, {"GATEWAY_MULTIPLEX_PROFILES": "on"}) is True
    assert resolve_multiplex_flag(home, {"GATEWAY_MULTIPLEX_PROFILES": "0"}) is False


def test_multiplex_unrecognized_env_falls_through_to_config(home: Path):
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    assert resolve_multiplex_flag(home, {"GATEWAY_MULTIPLEX_PROFILES": "maybe"}) is True


def test_multiplex_missing_config_is_gateway_default_false(home: Path):
    assert resolve_multiplex_flag(home, {}) is False


def test_multiplex_top_level_key_accepted(home: Path):
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("multiplex_profiles: true\n")
    assert resolve_multiplex_flag(home, {}) is True


def test_multiplex_key_absent_in_config_is_false(home: Path):
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("gateway:\n  trust_recent_files: true\n")
    assert resolve_multiplex_flag(home, {}) is False


# -----------------------------------------------------------------------------
# local_topology_findings - the pair-time gate
# -----------------------------------------------------------------------------


def test_single_route_yields_no_route_findings(home: Path):
    # hades served by the active profile with no hades profile: correct,
    # common topology. Must not be blocked or warned.
    assert local_topology_findings(home, ["hades"], {}) == []


def test_multi_route_missing_profile_fails_with_create_command(home: Path):
    findings = local_topology_findings(home, ["default", "shadow"], {})
    missing = [f for f in findings if f.check == "profile_missing"]
    assert missing and missing[0].severity == FAIL
    assert "hermes profile create shadow" in missing[0].fix


def test_multi_route_profile_without_soul_warns(home: Path):
    _profile(home, "shadow", soul=None)
    (home / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    findings = local_topology_findings(home, ["default", "shadow"], {})
    souls = [f for f in findings if f.check == "soul_missing"]
    assert souls and souls[0].severity == WARN
    assert "SOUL.md" in souls[0].detail


def test_multi_route_multiplex_off_fails_with_exact_command(home: Path):
    _profile(home, "shadow")
    findings = local_topology_findings(home, ["default", "shadow"], {})
    mux = [f for f in findings if f.check == "multiplex_off"]
    assert mux and mux[0].severity == FAIL
    assert "hermes config set gateway.multiplex_profiles true" in mux[0].fix


def test_clean_multi_route_topology_yields_nothing(home: Path):
    _profile(home, "shadow")
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    assert local_topology_findings(home, ["default", "shadow"], {}) == []


# -----------------------------------------------------------------------------
# stray per-profile secrets
# -----------------------------------------------------------------------------


def _write_secrets(path: Path, pairing_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pairing_token": "pair_x", "pairing_id": pairing_id}))


def test_find_profile_secrets_lists_only_existing_files(home: Path):
    _profile(home, "shadow")
    _profile(home, "hades")
    _write_secrets(home / "profiles" / "shadow" / "secrets" / "bgos.json", 9)
    assert find_profile_secrets(home) == [
        home / "profiles" / "shadow" / "secrets" / "bgos.json",
    ]


def test_stray_secrets_same_pairing_id_is_fail_even_single_route(home: Path):
    """The exact 2026-08-04 state: profiles/shadow/secrets/bgos.json held a
    COPY of the default pairing, a second adapter connected with it, and
    every reply arrived twice. FAIL regardless of route count."""
    _profile(home, "shadow")
    _write_secrets(home / "secrets" / "bgos.json", 42)
    _write_secrets(home / "profiles" / "shadow" / "secrets" / "bgos.json", 42)
    findings = local_topology_findings(home, ["default"], {})
    stray = [f for f in findings if f.check == "stray_secrets"]
    assert stray and stray[0].severity == FAIL
    assert "twice" in stray[0].detail
    assert str(home / "profiles" / "shadow" / "secrets" / "bgos.json") in stray[0].fix


def test_stray_secrets_on_a_routed_profile_is_fail(home: Path):
    """A profile that is a route on THIS pairing must not also carry its own
    pairing file - the one-pairing topology serves it through the default
    pairing and the extra file double-answers."""
    _profile(home, "shadow")
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    _write_secrets(home / "profiles" / "shadow" / "secrets" / "bgos.json", 7)
    findings = local_topology_findings(home, ["default", "shadow"], {})
    stray = [f for f in findings if f.check == "stray_secrets"]
    assert stray and stray[0].severity == FAIL


def test_profile_secrets_for_an_unrelated_profile_is_warn_only(home: Path):
    """Topology B (that profile runs its own separate gateway) is supported:
    an unrelated profile's own pairing file is a WARN, not a block."""
    _profile(home, "other")
    _write_secrets(home / "profiles" / "other" / "secrets" / "bgos.json", 7)
    findings = local_topology_findings(home, ["default"], {})
    stray = [f for f in findings if f.check == "stray_secrets"]
    assert stray and stray[0].severity == WARN


# -----------------------------------------------------------------------------
# overlapping pairings
# -----------------------------------------------------------------------------


def test_overlap_names_the_duplicate_and_the_revoke_path():
    findings = overlapping_pairing_findings(
        [
            {"id": 5, "device_label": "new-box",
             "agent_catalog": [{"agent_route": "shadow", "name": "S"}]},
            {"id": 7, "device_label": "old-box",
             "agent_catalog": [{"agent_route": "shadow", "name": "S"}]},
        ],
        self_pairing_id=5,
        routes=["default", "shadow"],
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == FAIL
    assert "old-box" in f.detail and "twice" in f.detail
    assert "revoke" in f.fix


def test_overlap_ignores_self_and_disjoint_pairings():
    findings = overlapping_pairing_findings(
        [
            {"id": 5, "device_label": "new-box",
             "agent_catalog": [{"agent_route": "shadow", "name": "S"}]},
            {"id": 8, "device_label": "other-host",
             "agent_catalog": [{"agent_route": "hades", "name": "H"}]},
        ],
        self_pairing_id=5,
        routes=["shadow"],
    )
    assert findings == []


def test_same_device_label_without_visible_overlap_warns():
    findings = overlapping_pairing_findings(
        [{"id": 7, "device_label": "kc-box", "agent_catalog": []}],
        self_pairing_id=5,
        routes=["shadow"],
        device_label="kc-box",
    )
    assert len(findings) == 1
    assert findings[0].severity == WARN
    assert "re-pair leftover" in findings[0].detail


# -----------------------------------------------------------------------------
# Layer A - SOUL.md blocked by Hermes's threat scanner
# -----------------------------------------------------------------------------


def test_blocked_soul_reported_for_routed_profile(home: Path):
    _profile(home, "shadow", soul="I carry controlled mythic weight.")
    home.mkdir(parents=True, exist_ok=True)
    (home / "SOUL.md").write_text("Harmless root persona.")

    def scanner(content: str) -> list[str]:
        return ["known_c2_framework"] if "mythic" in content else []

    findings = blocked_soul_findings(home, ["default", "shadow"], scanner)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == FAIL
    assert "known_c2_framework" in f.detail
    assert str(home / "profiles" / "shadow" / "SOUL.md") in f.detail


def test_blocked_soul_scans_the_active_profile_too(home: Path):
    home.mkdir(parents=True)
    (home / "SOUL.md").write_text("metasploit for breakfast")
    findings = blocked_soul_findings(
        home, [], lambda c: ["known_c2_framework"] if "metasploit" in c else [],
    )
    assert len(findings) == 1
    assert str(home / "SOUL.md") in findings[0].detail


def test_blocked_soul_skipped_when_no_scanner_available(home: Path):
    """Outside Hermes's venv there is no scanner - the check must be skipped
    honestly (no findings), never faked with a homegrown pattern list."""
    _profile(home, "shadow", soul="controlled mythic weight")
    assert blocked_soul_findings(home, ["shadow"], None) == []


# -----------------------------------------------------------------------------
# Layer B - sessions replaying a stored [BLOCKED: prompt
# -----------------------------------------------------------------------------


def _seed_state_db(home: Path, rows: list[tuple[str, str | None, float | None]]):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, system_prompt TEXT, "
        "ended_at REAL)"
    )
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_stale_sessions_none_without_state_db(home: Path):
    assert stale_blocked_session_finding(home) is None


def test_stale_sessions_none_when_clean(home: Path):
    _seed_state_db(home, [("s1", "healthy", None)])
    assert stale_blocked_session_finding(home) is None


def test_stale_sessions_fail_when_open_sessions_affected(home: Path):
    _seed_state_db(home, [
        ("s1", "[BLOCKED: SOUL.md contained potential prompt injection]", None),
        ("s2", "[BLOCKED: SOUL.md contained potential prompt injection]", 1.0),
        ("s3", "healthy", None),
    ])
    finding = stale_blocked_session_finding(home)
    assert finding is not None and finding.severity == FAIL
    assert "2 session(s)" in finding.detail and "1 still open" in finding.detail
    assert "system_prompt = NULL" in finding.fix
    assert "/new" in finding.fix


def test_stale_sessions_warn_when_only_ended_sessions_affected(home: Path):
    _seed_state_db(home, [
        ("s1", "[BLOCKED: whatever]", 1.0),
    ])
    finding = stale_blocked_session_finding(home)
    assert finding is not None and finding.severity == WARN


def test_yaml_fallback_parser_matches_pyyaml_semantics():
    """The standalone pair CLI may run without PyYAML - the minimal parser
    must agree with the yaml path on the shapes Hermes writes."""
    from hermes_channel_bgos.topology import _parse_multiplex_from_yaml_text

    assert _parse_multiplex_from_yaml_text(
        "gateway:\n  multiplex_profiles: true\n"
    ) is True
    assert _parse_multiplex_from_yaml_text(
        "gateway:\n  media_delivery_allow_dirs: []\n  multiplex_profiles: false\n"
    ) is False
    assert _parse_multiplex_from_yaml_text("multiplex_profiles: true\n") is True
    assert _parse_multiplex_from_yaml_text(
        "other:\n  multiplex_profiles: true\n"
    ) is None
    assert _parse_multiplex_from_yaml_text("gateway:\n  foo: 1\n") is None
    # inline comments must not confuse it
    assert _parse_multiplex_from_yaml_text(
        "gateway:\n  multiplex_profiles: true  # enabled for shadow\n"
    ) is True
