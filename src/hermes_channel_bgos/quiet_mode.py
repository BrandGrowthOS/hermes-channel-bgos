"""Conservative helpers for keeping machine narration out of agent chat.

The classifier deliberately accepts only narrow gateway shapes because hiding
real prose is worse than showing an unfamiliar machine message. Style choices
live in a small adapter-owned file so quiet behavior can vary by assistant
without introducing a backend setting or importing adapter runtime state here.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


TIDY = "tidy"
EVERYTHING = "everything"

_ARGS_LIMIT = 120
_ERROR_FRIENDLY_LIMIT = 200
_NAME_LIMIT = 64
_STYLE_FILENAME = "bgos_chat_style.json"
_VALID_STYLES = frozenset({TIDY, EVERYTHING})

_ERROR_PREFIX_RE = re.compile(r"^[❌⚠]\ufe0f*")
_CALL_RE = re.compile(
    r"^(?P<identifier>[A-Za-z_][A-Za-z0-9_.]*)"
    r"\((?P<args>(?:[^()]|\([^()]*\))*)\)"
    r"\s*(?:\(×\d+\))?$"
)
_DEDUP_TAIL_RE = re.compile(r"^\(×\d+\)$")
_VOICE_PREFIX_RE = re.compile(
    r"^(?:\[voice consult\]|\[voice dispatch\])\s*"
)


@dataclass(frozen=True)
class RobotTalk:
    """A confidently recognized machine message and its safe presentation.

    ``details`` always retains the original input so later routing can suppress
    chat text without discarding diagnostic content. Tool rows use the existing
    progress card shape, while errors carry a short sentence for the user.
    """

    kind: str
    rows: list[dict[str, str]]
    friendly: str
    details: str


def _truncate(value: str, limit: int) -> str:
    """Keep a display value within its wire limit and mark actual truncation."""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _json_tool_row(line: str) -> dict[str, str] | None:
    """Build a row only for JSON objects that carry call-related keys."""
    if not line.startswith("{"):
        return None
    try:
        parsed = json.loads(line)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not any(key in parsed for key in ("name", "arguments", "parameters")):
        return None

    try:
        compact = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    return {
        "icon": "⚙️",
        "name": str(parsed.get("name") or "call")[:_NAME_LIMIT],
        "args": _truncate(compact, _ARGS_LIMIT),
        "status": "done",
    }


def _call_tool_row(line: str) -> dict[str, str] | None:
    """Build a row for the gateway's compact identifier call notation."""
    match = _CALL_RE.fullmatch(line)
    if match is None:
        return None
    identifier = match.group("identifier")
    return {
        "icon": "⚙️",
        "name": identifier.rsplit(".", 1)[-1][:_NAME_LIMIT],
        "args": _truncate(match.group("args"), _ARGS_LIMIT),
        "status": "done",
    }


def classify_robot_talk(text: str | None) -> RobotTalk | None:
    """Recognize narrow gateway output shapes while treating doubt as prose.

    A leading gateway failure symbol is authoritative even when diagnostic
    prose follows it. Tool output is stricter: every nonblank line must be a
    JSON call, compact call expression, or deduplication tail. This all-lines
    rule prevents an ordinary reply containing one code sample from vanishing.
    """
    if text is None or not text.strip():
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first_line = lines[0]
    error_match = _ERROR_PREFIX_RE.match(first_line)
    if error_match is not None:
        friendly = first_line[error_match.end() :].strip()
        return RobotTalk(
            kind="error",
            rows=[],
            friendly=friendly[:_ERROR_FRIENDLY_LIMIT],
            details=text,
        )

    rows: list[dict[str, str]] = []
    for line in lines:
        row = _json_tool_row(line)
        if row is not None:
            rows.append(row)
            continue

        row = _call_tool_row(line)
        if row is not None:
            rows.append(row)
            continue

        if _DEDUP_TAIL_RE.fullmatch(line) is not None:
            continue

        return None

    return RobotTalk(
        kind="tool_dump",
        rows=rows,
        friendly="",
        details=text,
    )


def strip_voice_prefixes(text: str) -> str:
    """Remove one transport prefix only when it begins the visible reply.

    Voice mode adds these markers to inbound turns, but an assistant can echo
    one into its response. Anchoring and limiting the substitution preserves
    intentional references elsewhere in the response and any second prefix.
    """
    return _VOICE_PREFIX_RE.sub("", text, count=1)


def load_default_style() -> str:
    """Resolve the host default with tidy behavior as the safe fallback.

    Only the documented value enables the legacy everything style. Stripping
    and case folding make shell configuration forgiving without accepting new
    undocumented modes.
    """
    configured = (os.environ.get("BGOS_CHAT_STYLE") or "").strip().lower()
    if configured == EVERYTHING:
        return EVERYTHING
    return TIDY


class ChatStyleStore:
    """Persist optional per-assistant overrides in a small JSON object.

    The default path is resolved for every operation because tests and hosted
    runtimes can redirect ``HERMES_HOME`` after constructing the store. Reads
    are best effort so a damaged preference file can never stop chat delivery.
    Writes use a sibling temporary file and replacement so readers see either
    the prior complete mapping or the new complete mapping.
    """

    def __init__(self, path: "Path | None" = None):
        self._injected_path = Path(path) if path is not None else None

    def _path_for_call(self) -> Path:
        """Return an injected path or lazily resolve the current Hermes home."""
        if self._injected_path is not None:
            return self._injected_path
        hermes_home = Path(os.environ.get("HERMES_HOME") or "~/.hermes")
        return hermes_home.expanduser() / _STYLE_FILENAME

    @staticmethod
    def _read(path: Path) -> dict[str, str]:
        """Return only valid overrides, treating storage failures as empty."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, UnicodeError, ValueError, RecursionError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key, value in raw.items()
            if (
                isinstance(key, str)
                and isinstance(value, str)
                and value in _VALID_STYLES
            )
        }

    @staticmethod
    def _write(path: Path, styles: dict[str, str]) -> None:
        """Atomically replace the mapping so interruption cannot leave fragments."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(
                    styles,
                    temporary_file,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def style_for(self, assistant_id: "int | None") -> str:
        """Return a valid assistant override or the current host default."""
        default = load_default_style()
        if assistant_id is None:
            return default
        styles = self._read(self._path_for_call())
        return styles.get(str(assistant_id), default)

    def set_style(self, assistant_id: int, style: str) -> None:
        """Store a nondefault override, or remove a redundant default value."""
        if not isinstance(style, str) or style not in _VALID_STYLES:
            raise ValueError(f"style must be {TIDY!r} or {EVERYTHING!r}")

        path = self._path_for_call()
        styles = self._read(path)
        key = str(assistant_id)
        if style == load_default_style():
            styles.pop(key, None)
        else:
            styles[key] = style
        self._write(path, styles)
