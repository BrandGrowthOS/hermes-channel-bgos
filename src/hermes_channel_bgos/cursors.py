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
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Rows the backend returns per inbound poll (its ``LIMIT 200``).
BACKEND_PAGE_SIZE = 200

# Pending-unknown records (see AgentCursors): how many are kept (drop oldest)
# and for how long, so an agent that is never bound cannot pin every cursor
# forever.
MAX_PENDING = 50
PENDING_TTL_SECONDS = 3600.0


def _is_cursor(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class AgentCursors:
    def __init__(
        self, path: Path, legacy_path: Path, pending_path: Path | None = None,
    ) -> None:
        self._path = path
        self._legacy_path = legacy_path
        self._pending_path = pending_path or path.with_name(
            "bgos_pending_unknown.json"
        )

    # -- pending unknown ------------------------------------------------------
    #
    # A row served for an agent this gateway does not know yet is held in memory
    # by the adapter. Cursors must not move past it, or after a restart the agent
    # (bound by then) starts at the head and the row is lost. So the id is
    # persisted here, every cursor and the head are clamped below the lowest one,
    # and the poll window reaches back to it until the row is delivered.

    def _load_pending(self) -> list[dict]:
        try:
            raw = json.loads(self._pending_path.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        now = time.time()
        out: list[dict] = []
        for e in raw:
            if (
                isinstance(e, dict)
                and _is_cursor(e.get("id")) and e["id"] > 0
                and _is_cursor(e.get("assistant_id"))
                and isinstance(e.get("at"), (int, float))
                and now - e["at"] <= PENDING_TTL_SECONDS
            ):
                out.append({"id": e["id"], "assistant_id": e["assistant_id"],
                            "at": e["at"]})
        return out

    def _write_pending(self, entries: list[dict]) -> None:
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._pending_path.with_name(
                f"{self._pending_path.name}.{os.getpid()}.tmp"
            )
            tmp.write_text(json.dumps(entries))
            os.replace(tmp, self._pending_path)
        except OSError:
            log.warning("could not persist bgos_pending_unknown.json")

    def record_pending(self, assistant_id: int, message_id: int) -> None:
        """Remember a row left unconsumed because its agent is not known yet."""
        if not _is_cursor(message_id) or message_id <= 0:
            return
        entries = self._load_pending()
        if any(e["id"] == message_id and e["assistant_id"] == assistant_id
               for e in entries):
            return
        entries.append({"id": message_id, "assistant_id": assistant_id,
                        "at": time.time()})
        self._write_pending(entries[-MAX_PENDING:])

    def clear_pending(self, assistant_id: int, message_id: int) -> None:
        """The row was consumed: release the clamp it held."""
        entries = self._load_pending()
        kept = [e for e in entries
                if not (e["id"] == message_id and e["assistant_id"] == assistant_id)]
        if len(kept) != len(entries):
            self._write_pending(kept)

    def lowest_pending(self, assistant_id: int | None = None) -> int | None:
        """The lowest pending row id (of one agent, or of any)."""
        ids = [e["id"] for e in self._load_pending()
               if assistant_id is None or e["assistant_id"] == assistant_id]
        return min(ids) if ids else None

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
            persisted = {k: v for k, v in state.items()
                         if k not in ("holes", "lowest_hole")}
            tmp.write_text(json.dumps(persisted))
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
            return self._with_holes(state)
        legacy = self._read_legacy()
        state = {"version": 1, "head": legacy, "agents": {}}
        if legacy > 0:
            state["migrating"] = legacy
            self._write(state)
        return self._with_holes(state)

    def _with_holes(self, state: dict) -> dict:
        """Attach the pending-unknown lowest ids (never written to the cursor
        file): per agent, and across all agents."""
        holes: dict[str, int] = {}
        for e in self._load_pending():
            key = str(e["assistant_id"])
            if key not in holes or e["id"] < holes[key]:
                holes[key] = e["id"]
        state["holes"] = holes
        state["lowest_hole"] = min(holes.values()) if holes else None
        return state

    @staticmethod
    def _effective(state: dict, assistant_id: int) -> int:
        """One agent's cursor: its own entry, else where an agent not seen
        before starts: the migrated value until the first poll, afterwards the
        head."""
        own = state["agents"].get(str(assistant_id))
        if own is not None:
            return own
        base = state.get("migrating", state["head"])
        # An agent whose own row is still pending starts below it.
        hole = state["holes"].get(str(assistant_id))
        return min(base, max(0, hole - 1)) if hole is not None else base

    # -- reads ----------------------------------------------------------------

    def head(self) -> int:
        return self._load()["head"]

    def get(self, assistant_id: int) -> int:
        """One agent's cursor."""
        return self._effective(self._load(), assistant_id)

    def agent_ids(self) -> set[int]:
        """The agents that have a cursor of their own."""
        return {int(k) for k in self._load()["agents"]}

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
        # Only a poll that serves agents hands the migrated value over: one with
        # none known keeps it for the agents that come, or a live message could
        # move the head past it first.
        if "migrating" in state and ids:
            del state["migrating"]
            changed = True
        if changed:
            self._write(state)
        since = (min(self._effective(state, a) for a in ids)
                 if ids else state["head"])
        # Reach back to the lowest row still pending for an unknown agent, so it
        # is fetched again once that agent is bound (also after a restart).
        if state["lowest_hole"] is not None:
            since = min(since, max(0, state["lowest_hole"] - 1))
        return since

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
            hole = state["holes"].get(str(aid))
            target = state["head"] if hole is None else min(state["head"], hole - 1)
            if own is not None and target > own:
                state["agents"][str(aid)] = target
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
        # Never past a row still pending for an unknown agent (see above).
        if state["lowest_hole"] is not None and message_id >= state["lowest_hole"]:
            message_id = state["lowest_hole"] - 1
            if message_id <= 0:
                return
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
