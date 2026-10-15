"""Persistent per-agent inbound cursors.

ONE CURSOR PER AGENT (per assistant id), not one per gateway. The backend
serves a message addressed to several agents as one poll row per agent, all
carrying the same message id. With a single gateway cursor, the first agent's
row moved the cursor past the id and the other agents' rows for that id were
dropped, or never fetched again after a restart in between. Each agent now
advances only past messages it consumed itself.

File: ``$HERMES_HOME/bgos_last_ids.json`` (the profile home the adapter
captured at construction).

Format: ``{"version": 1, "head": <n>, "agents": {"<assistantId>": <n>},
"migrating": <n>?}``.

- ``agents`` holds each agent's own cursor.
- ``head`` is the highest id any agent has consumed or been carried past. An
  agent exposed LATER starts there (its older rows are history it was not part
  of), exactly as the single cursor used to place it.
- ``migrating`` exists only between the migration and the first poll.

MIGRATION: before per-agent cursors the file was ``$HERMES_HOME/bgos_last_id``,
a single integer. When ``bgos_last_ids.json`` does not exist yet, every agent
known at the first poll gets that old value as its own cursor (``migrating``
holds it until then, so a live message that lands first cannot move it), so
every agent starts exactly where the single cursor was: nothing replays and
nothing is skipped. The old file is left in place and is no longer written.

Writes are atomic (tempfile + rename) so a crash mid-write cannot leave a half
written file. Errors never raise: the worst case is a one-time replay on the
next start, and a persistence hiccup must not take the gateway down.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Rows the backend returns per inbound poll (its ``LIMIT 200``).
BACKEND_PAGE_SIZE = 200


def _is_cursor(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class AgentCursors:
    def __init__(self, path: Path, legacy_path: Path) -> None:
        self._path = path
        self._legacy_path = legacy_path

    # -- persistence ----------------------------------------------------------

    def _read_legacy(self) -> int:
        try:
            value = int(self._legacy_path.read_text().strip())
        except (OSError, ValueError):
            return 0
        return value if value >= 0 else 0

    def _write(self, state: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self._path)
        except OSError:
            # Disk full / read-only / permission denied: do not crash; worst
            # case we replay on the next restart.
            log.warning("could not persist bgos_last_ids.json")

    def _load(self) -> dict:
        """The cursor state, migrating the old single cursor on first start
        (and persisting the result once, so the migration is a single event)."""
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict) and _is_cursor(raw.get("head")):
            agents: dict[str, int] = {}
            stored = raw.get("agents")
            if isinstance(stored, dict):
                for key, value in stored.items():
                    try:
                        int(key)
                    except (TypeError, ValueError):
                        continue
                    if _is_cursor(value):
                        agents[str(key)] = value
            state: dict = {"version": 1, "head": raw["head"], "agents": agents}
            if _is_cursor(raw.get("migrating")):
                state["migrating"] = raw["migrating"]
            return state
        legacy = self._read_legacy()
        state = {"version": 1, "head": legacy, "agents": {}}
        if legacy > 0:
            state["migrating"] = legacy
            self._write(state)
        return state

    @staticmethod
    def _effective(state: dict, assistant_id: int) -> int:
        """One agent's cursor: its own entry, else where an agent not seen
        before starts: the migrated value until the first poll, afterwards the
        head."""
        own = state["agents"].get(str(assistant_id))
        if own is not None:
            return own
        return state.get("migrating", state["head"])

    # -- reads ----------------------------------------------------------------

    def head(self) -> int:
        return self._load()["head"]

    def get(self, assistant_id: int) -> int:
        """One agent's cursor."""
        return self._effective(self._load(), assistant_id)

    def reader(self):
        """A snapshot reader: read the file once, answer for many rows."""
        state = self._load()
        return lambda assistant_id: self._effective(state, assistant_id)

    def since(self, known: Iterable[int]) -> int:
        """The id to poll ``since_message_id`` from: the LOWEST cursor of the
        agents this gateway serves (the poll returns every agent's rows, and
        each row is then kept or dropped by its own agent's cursor). With no
        agent known yet, the head.

        Also gives every known agent that has no entry its own (the migrated
        value on the first poll after an upgrade, the head for an agent exposed
        later), so from then on it is just a cursor like the others."""
        state = self._load()
        ids = list(known)
        changed = False
        for aid in ids:
            if str(aid) not in state["agents"]:
                state["agents"][str(aid)] = self._effective(state, aid)
                changed = True
        if "migrating" in state:
            del state["migrating"]
            changed = True
        if changed:
            self._write(state)
        if not ids:
            return state["head"]
        return min(self._effective(state, a) for a in ids)

    # -- writes ---------------------------------------------------------------

    def rebase_reexposed(self, assistant_ids: Iterable[int]) -> None:
        """Agents that were absent from the known set and are back (removed
        from the pairing, then exposed again while this gateway ran) rejoin at
        the head like an agent seen for the first time: what arrived for them
        while away is history they were not part of, not a backlog to replay or
        a storm to trip over. Never moves a cursor down."""
        state = self._load()
        changed = False
        for aid in assistant_ids:
            own = state["agents"].get(str(aid))
            if own is not None and state["head"] > own:
                state["agents"][str(aid)] = state["head"]
                changed = True
        if changed:
            self._write(state)

    def save(self, assistant_id: int, message_id: int) -> None:
        """Advance ONE agent's cursor. Never regresses; ignores non-positive
        or non-integer ids."""
        self._advance([assistant_id], message_id)

    def seed_all(self, known: Iterable[int], message_id: int) -> None:
        """Move the head and every given agent forward together: the storm
        guard, which skips dispatch for a whole batch. Agents exposed later
        start at the new head."""
        self._advance(list(known), message_id, seed=True)

    def advance_idle(self, idle: Iterable[int], watermark: int) -> None:
        """After a poll that returned rows up to ``watermark`` (every row at or
        below it is in that response), the agents that had NO row in it have
        nothing pending at or below it: move them to the watermark. Without
        this an idle agent's cursor would pin the poll window below everyone
        else's and the 200 row page would stop reaching new messages."""
        ids = list(idle)
        if ids:
            self._advance(ids, watermark)

    def _advance(
        self, assistant_ids: list[int], message_id: int, seed: bool = False,
    ) -> None:
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            return
        state = self._load()
        changed = False
        for aid in assistant_ids:
            if message_id > self._effective(state, aid):
                state["agents"][str(aid)] = message_id
                changed = True
        if (seed or assistant_ids) and message_id > state["head"]:
            state["head"] = message_id
            changed = True
        if seed and "migrating" in state:
            del state["migrating"]
            changed = True
        if changed:
            self._write(state)
