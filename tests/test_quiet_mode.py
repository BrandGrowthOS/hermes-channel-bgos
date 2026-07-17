import json
import os
import stat
from pathlib import Path

import pytest

import hermes_channel_bgos.quiet_mode as quiet_mode
from hermes_channel_bgos.quiet_mode import (
    EVERYTHING,
    TIDY,
    ChatStyleStore,
    classify_robot_talk,
    load_default_style,
    strip_voice_prefixes,
)


def test_json_dump_single_line_builds_tool_row() -> None:
    text = '{"name":"calendar_read","arguments":{"start":"2026-07-18"}}'

    result = classify_robot_talk(text)

    assert result is not None
    assert result.kind == "tool_dump"
    assert result.rows == [
        {
            "icon": "⚙️",
            "name": "calendar_read",
            "args": text,
            "status": "done",
        }
    ]
    assert len(result.rows[0]["args"]) <= 120
    assert result.friendly == ""
    assert result.details == text


def test_json_dump_compacts_spaced_object() -> None:
    text = '{"name": "calendar_read", "arguments": {"day": "sat"}}'

    result = classify_robot_talk(text)

    assert result is not None
    assert result.rows[0]["args"] == (
        '{"name":"calendar_read","arguments":{"day":"sat"}}'
    )


def test_long_json_args_are_truncated_to_limit() -> None:
    text = json.dumps(
        {"name": "calendar_read", "arguments": {"query": "x" * 180}}
    )

    result = classify_robot_talk(text)

    assert result is not None
    args = result.rows[0]["args"]
    assert len(args) == 120
    assert args.endswith("…")


def test_json_recursion_error_is_treated_as_chat_worthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_loads(value: str) -> object:
        raise RecursionError

    monkeypatch.setattr(quiet_mode.json, "loads", fail_loads)

    assert classify_robot_talk('{"name":"call"}') is None


def test_multiline_json_dump_ignores_bare_dedup_tail() -> None:
    text = "\n".join(
        [
            '{"name":"calendar_read","arguments":{"day":"sat"}}',
            '{"name":"weather_read","parameters":{"city":"Dubai"}}',
            "(×3)",
        ]
    )

    result = classify_robot_talk(text)

    assert result is not None
    assert result.kind == "tool_dump"
    assert [row["name"] for row in result.rows] == [
        "calendar_read",
        "weather_read",
    ]


def test_bare_dedup_tails_return_tool_dump_without_rows() -> None:
    text = "(×2)\n\n(×9)"

    result = classify_robot_talk(text)

    assert result is not None
    assert result.kind == "tool_dump"
    assert result.rows == []
    assert result.details == text


def test_call_syntax_builds_tool_row_without_dedup_suffix() -> None:
    text = 'read_file(path="/home/jeff/notes.md") (×3)'

    result = classify_robot_talk(text)

    assert result is not None
    assert result.kind == "tool_dump"
    assert result.rows == [
        {
            "icon": "⚙️",
            "name": "read_file",
            "args": 'path="/home/jeff/notes.md"',
            "status": "done",
        }
    ]


def test_dotted_call_uses_final_name_segment() -> None:
    result = classify_robot_talk('tools.calendar.read(day="sat")')

    assert result is not None
    assert result.rows[0]["name"] == "read"
    assert result.rows[0]["args"] == 'day="sat"'


def test_mixed_prose_and_json_is_chat_worthy() -> None:
    text = "I checked your calendar.\n{\"name\":\"calendar_read\"}"

    assert classify_robot_talk(text) is None


def test_fenced_code_reply_is_chat_worthy() -> None:
    text = '```json\n{"name":"calendar_read"}\n```'

    assert classify_robot_talk(text) is None


def test_plain_prose_is_chat_worthy() -> None:
    assert classify_robot_talk("Your Saturday is clear after lunch.") is None


def test_spaced_parenthetical_prose_is_chat_worthy() -> None:
    assert classify_robot_talk("Ok (done)") is None


def test_celebration_emoji_reply_is_chat_worthy() -> None:
    assert classify_robot_talk("🎉 Done! Your Saturday is planned.") is None


def test_multilingual_prose_is_chat_worthy() -> None:
    assert classify_robot_talk("تم ترتيب يوم السبت لك.") is None


@pytest.mark.parametrize("text", ["", " \n\t", None])
def test_empty_text_is_chat_worthy(text: str | None) -> None:
    assert classify_robot_talk(text) is None


def test_long_call_args_are_truncated_to_limit() -> None:
    text = f'write_note(content="{"x" * 160}")'

    result = classify_robot_talk(text)

    assert result is not None
    args = result.rows[0]["args"]
    assert len(args) == 120
    assert args.endswith("…")


def test_cross_error_uses_first_line_as_friendly_text() -> None:
    text = "❌ Hermes update failed."

    result = classify_robot_talk(text)

    assert result is not None
    assert result.kind == "error"
    assert result.rows == []
    assert result.friendly == "Hermes update failed."
    assert result.details == text


def test_warning_error_wins_over_later_prose() -> None:
    text = "⚠️ Your calendar didn't answer\nmore detail"

    result = classify_robot_talk(text)

    assert result is not None
    assert result.kind == "error"
    assert result.friendly == "Your calendar didn't answer"
    assert result.details == text


def test_error_strips_repeated_variation_selectors() -> None:
    result = classify_robot_talk("⚠\ufe0f\ufe0f something")

    assert result is not None
    assert result.kind == "error"
    assert result.friendly == "something"


def test_error_uses_first_nonblank_line() -> None:
    text = "\n  \n  ❌ Background task failed  \n```\ntrace\n```"

    result = classify_robot_talk(text)

    assert result is not None
    assert result.kind == "error"
    assert result.friendly == "Background task failed"
    assert result.details == text


def test_error_friendly_text_is_truncated_to_limit() -> None:
    result = classify_robot_talk(f"❌ {'x' * 250}")

    assert result is not None
    assert len(result.friendly) == 200
    assert result.friendly == "x" * 200


@pytest.mark.parametrize("prefix", ["[voice consult]", "[voice dispatch]"])
def test_strip_voice_prefix_eats_following_whitespace(prefix: str) -> None:
    assert strip_voice_prefixes(f"{prefix}\n \tHello") == "Hello"


def test_strip_voice_prefix_leaves_interior_occurrence() -> None:
    text = "Keep [voice consult] inside this reply."

    assert strip_voice_prefixes(text) == text


def test_strip_voice_prefix_leaves_nonprefixed_text_unchanged() -> None:
    text = "Hello from the assistant."

    assert strip_voice_prefixes(text) == text


@pytest.mark.parametrize("prefix", ["[voice consult]", "[voice dispatch]"])
def test_strip_voice_prefix_only_returns_empty(prefix: str) -> None:
    assert strip_voice_prefixes(prefix) == ""


def test_strip_voice_prefix_removes_exactly_one_prefix() -> None:
    text = "[voice consult][voice dispatch]Hello"

    assert strip_voice_prefixes(text) == "[voice dispatch]Hello"


def test_load_default_style_is_tidy_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)

    assert load_default_style() == TIDY


def test_load_default_style_accepts_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BGOS_CHAT_STYLE", "everything")

    assert load_default_style() == EVERYTHING


def test_load_default_style_ignores_case_and_outer_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BGOS_CHAT_STYLE", "EVERYTHING ")

    assert load_default_style() == EVERYTHING


def test_load_default_style_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BGOS_CHAT_STYLE", "weird")

    assert load_default_style() == TIDY


def test_style_store_returns_tidy_for_unknown_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    store = ChatStyleStore(tmp_path / "styles.json")

    assert store.style_for(42) == TIDY


def test_style_store_persists_for_fresh_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    path = tmp_path / "styles.json"
    ChatStyleStore(path).set_style(42, EVERYTHING)

    assert ChatStyleStore(path).style_for(42) == EVERYTHING


def test_style_store_removes_value_equal_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    path = tmp_path / "styles.json"
    store = ChatStyleStore(path)
    store.set_style(42, EVERYTHING)

    store.set_style(42, TIDY)

    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_style_store_tolerates_corrupt_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    path = tmp_path / "styles.json"
    path.write_text("not json", encoding="utf-8")

    assert ChatStyleStore(path).style_for(42) == TIDY


def test_style_store_tolerates_invalid_mapping_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    path = tmp_path / "styles.json"
    path.write_text('{"42":[]}', encoding="utf-8")

    assert ChatStyleStore(path).style_for(42) == TIDY


def test_style_store_tolerates_json_recursion_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    path = tmp_path / "styles.json"
    path.write_text("{}", encoding="utf-8")

    def fail_loads(value: str) -> object:
        raise RecursionError

    monkeypatch.setattr(quiet_mode.json, "loads", fail_loads)

    assert ChatStyleStore(path).style_for(42) == TIDY


def test_style_store_rejects_bad_style(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ChatStyleStore(tmp_path / "styles.json").set_style(42, "verbose")


def test_style_store_rejects_non_string_style(tmp_path: Path) -> None:
    store = ChatStyleStore(tmp_path / "styles.json")

    with pytest.raises(ValueError):
        store.set_style(42, [])  # type: ignore[arg-type]


def test_style_store_none_id_returns_current_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BGOS_CHAT_STYLE", "everything")

    assert ChatStyleStore(tmp_path / "missing.json").style_for(None) == EVERYTHING


def test_style_store_unknown_id_returns_environment_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BGOS_CHAT_STYLE", "everything")

    assert ChatStyleStore(tmp_path / "missing.json").style_for(42) == EVERYTHING


def test_style_store_resolves_default_path_for_each_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    first_home = tmp_path / "first"
    second_home = tmp_path / "second"
    store = ChatStyleStore()
    monkeypatch.setenv("HERMES_HOME", str(first_home))
    store.set_style(42, EVERYTHING)

    monkeypatch.setenv("HERMES_HOME", str(second_home))

    assert store.style_for(42) == TIDY
    assert json.loads(
        (first_home / "bgos_chat_style.json").read_text(encoding="utf-8")
    ) == {"42": EVERYTHING}
    assert not (second_home / "bgos_chat_style.json").exists()


def test_style_store_uses_same_directory_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    path = tmp_path / "nested" / "styles.json"
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == path.parent
        assert source_path.is_file()
        replace_calls.append((source_path, destination_path))
        real_replace(source_path, destination_path)

    monkeypatch.setattr(quiet_mode.os, "replace", record_replace)

    ChatStyleStore(path).set_style(42, EVERYTHING)

    assert len(replace_calls) == 1
    assert replace_calls[0][1] == path
    assert not replace_calls[0][0].exists()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_style_store_replace_failure_preserves_old_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BGOS_CHAT_STYLE", raising=False)
    path = tmp_path / "styles.json"
    path.write_text('{"7":"everything"}\n', encoding="utf-8")

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(quiet_mode.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        ChatStyleStore(path).set_style(42, EVERYTHING)

    assert json.loads(path.read_text(encoding="utf-8")) == {"7": EVERYTHING}
    assert list(tmp_path.glob("*.tmp")) == []
