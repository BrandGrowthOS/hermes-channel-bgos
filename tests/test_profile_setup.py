"""Tests for the add_profile local work: profile creation, BGOS_AGENTS env
persistence, and the multiplex honesty flag (one-click new-Hermes-agent,
existing-host path).

Topology contract (topology.py): ONE pairing, multi-route catalog, each
non-default route served by the Hermes profile of the same name. The server
binds the new assistant to the existing pairing; this module only does the
LOCAL work, and must never create a per-profile bgos.json (the stray-secrets
FAIL class).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_channel_bgos.profile_setup import apply_add_profile


def hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"])


PAYLOAD = {"assistant_id": 1012, "agent_route": "wolf", "name": "Wolf"}


def fake_create_profile(created: list[str]):
    def _create(name: str) -> Path:
        created.append(name)
        pdir = hermes_home() / "profiles" / name
        pdir.mkdir(parents=True)
        return pdir

    return _create


def test_creates_the_profile_and_seeds_a_soul(tmp_path):
    created: list[str] = []

    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile(created),
        multiplex_active=True,
    )

    assert result.ok, result.error_message
    assert created == ["wolf"]
    assert result.profile == "wolf"
    soul = hermes_home() / "profiles" / "wolf" / "SOUL.md"
    assert soul.is_file()
    assert "Wolf" in soul.read_text()


def test_a_template_soul_from_the_bootstrapper_still_gets_the_identity(tmp_path):
    """hermes_cli.create_profile seeds a GENERIC template SOUL.md; a profile
    we created IN THIS CALL must still end up answering as the named agent
    (found live 2026-08-09: Sandbox Beta would have introduced itself as
    "Hermes Agent"). A pre-existing profile's SOUL is never touched (the
    test above this one)."""
    created: list[str] = []

    def create_with_template(name: str):
        pdir = fake_create_profile(created)(name)
        (pdir / "SOUL.md").write_text("You are Hermes Agent, generic.\n")
        return pdir

    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=create_with_template,
        multiplex_active=True,
    )

    assert result.ok
    soul_text = (hermes_home() / "profiles" / "wolf" / "SOUL.md").read_text()
    assert "You are Hermes Agent, generic." in soul_text
    assert "Wolf" in soul_text


def test_existing_profile_is_reused_and_its_soul_untouched(tmp_path):
    pdir = hermes_home() / "profiles" / "wolf"
    pdir.mkdir(parents=True)
    (pdir / "SOUL.md").write_text("# Custom Wolf soul\n")
    created: list[str] = []

    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile(created),
        multiplex_active=True,
    )

    assert result.ok
    assert created == []
    assert (pdir / "SOUL.md").read_text() == "# Custom Wolf soul\n"


def test_appends_route_to_bgos_agents_preserving_other_env_keys(tmp_path):
    env_file = hermes_home() / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "BGOS_AGENTS=default:Hermes\nOPENAI_API_KEY=sk-secret\n",
    )

    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert result.ok
    content = env_file.read_text()
    assert "OPENAI_API_KEY=sk-secret" in content
    assert result.agents_spec == "default:Hermes,wolf:Wolf"
    assert "BGOS_AGENTS=default:Hermes,wolf:Wolf" in content


def test_env_update_is_idempotent_for_a_known_route(tmp_path):
    env_file = hermes_home() / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("BGOS_AGENTS=default:Hermes,wolf:Wolf\n")

    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert result.ok
    assert result.agents_spec == "default:Hermes,wolf:Wolf"
    assert env_file.read_text().count("BGOS_AGENTS=") == 1


def test_creates_env_file_when_missing(tmp_path):
    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert result.ok
    assert "BGOS_AGENTS=wolf:Wolf" in (hermes_home() / ".env").read_text()


def test_multiplex_off_reports_restart_required(tmp_path):
    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=False,
    )

    assert result.ok
    assert result.multiplex is False
    assert result.restart_required is True


def test_multiplex_on_needs_no_restart(tmp_path):
    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert result.ok
    assert result.multiplex is True
    assert result.restart_required is False


@pytest.mark.parametrize(
    "route",
    ["", "Wolf", "wolf/../../etc", "-wolf", "wo lf", "default"],
)
def test_invalid_or_reserved_routes_are_refused(tmp_path, route):
    result = apply_add_profile(
        {**PAYLOAD, "agent_route": route},
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert not result.ok
    assert result.error_code == "invalid_route"


def test_never_writes_a_per_profile_pairing_file(tmp_path):
    apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert not (
        hermes_home() / "profiles" / "wolf" / "secrets" / "bgos.json"
    ).exists()


def test_create_profile_failure_is_an_error_result(tmp_path):
    def exploding_create(name: str) -> Path:
        raise RuntimeError("disk full")

    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=exploding_create,
        multiplex_active=True,
    )

    assert not result.ok
    assert result.error_code == "profile_create_failed"
    assert "disk full" in (result.error_message or "")


def test_comma_or_newline_in_the_name_cannot_corrupt_the_env(tmp_path):
    """The spec is comma-delimited and .env is line-delimited: an unsanitized
    display name could mint a phantom route or inject an arbitrary env line
    (review finding, 2026-08-09)."""
    result = apply_add_profile(
        {**PAYLOAD, "name": "Wolf, Jr\nOPENAI_API_BASE=evil"},
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert result.ok
    assert result.agents_spec == "wolf:Wolf Jr OPENAI_API_BASE=evil"
    content = (hermes_home() / ".env").read_text()
    assert "OPENAI_API_BASE" not in [
        line.split("=", 1)[0] for line in content.splitlines()
    ]
    assert content.count("BGOS_AGENTS=") == 1


def test_env_merge_recognizes_export_and_quoted_spellings(tmp_path):
    """The doctor's own parser accepts `export BGOS_AGENTS=...` and quoted
    values, so those spellings exist in the wild; failing to recognize them
    used to append a SECOND line and shrink the catalog to just the new
    route on last-wins parsing (review finding, 2026-08-09)."""
    env_file = hermes_home() / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text('export BGOS_AGENTS="default:Hermes"\n')

    result = apply_add_profile(
        PAYLOAD,
        hermes_home=hermes_home(),
        create_profile=fake_create_profile([]),
        multiplex_active=True,
    )

    assert result.ok
    assert result.agents_spec == "default:Hermes,wolf:Wolf"
    content = env_file.read_text()
    assert content.count("BGOS_AGENTS") == 1
    assert "BGOS_AGENTS=default:Hermes,wolf:Wolf" in content


def test_concurrent_add_profile_calls_do_not_lose_updates(tmp_path):
    """Two frames with different rpcIds run in two worker threads; the .env
    read-modify-write must be serialized or one route vanishes until the
    next add (review finding, 2026-08-09)."""
    import threading

    barrier = threading.Barrier(2)

    def create(name):
        pdir = hermes_home() / "profiles" / name
        pdir.mkdir(parents=True)
        return pdir

    def run(route, name):
        barrier.wait()
        apply_add_profile(
            {"assistant_id": 1, "agent_route": route, "name": name},
            hermes_home=hermes_home(),
            create_profile=create,
            multiplex_active=True,
        )

    t1 = threading.Thread(target=run, args=("wolf", "Wolf"))
    t2 = threading.Thread(target=run, args=("bear", "Bear"))
    t1.start(); t2.start(); t1.join(); t2.join()

    content = (hermes_home() / ".env").read_text()
    assert content.count("BGOS_AGENTS=") == 1
    spec_line = [l for l in content.splitlines() if l.startswith("BGOS_AGENTS=")][0]
    assert "wolf:Wolf" in spec_line
    assert "bear:Bear" in spec_line
