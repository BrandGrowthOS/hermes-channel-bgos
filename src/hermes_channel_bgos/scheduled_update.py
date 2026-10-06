"""Scheduled apply of a plugin update at a safe moment (design 2.3, D8).

Before this, the daily check only RECORDED latestKnownVersion and nothing
applied it: an update landed only when a human pressed Update now, and on
an unsupervised host even that only staged. This module holds the pure
decisions the adapter's scheduled-update loop runs every minute:

- `decide_scheduled_update`: WHAT to do. Pull a newer same-major version
  (`apply`), restart onto an install already on disk (`restart`), or
  nothing. Only under a VERIFIED supervisor (systemd unit or launchd job
  owning this pid; decision D8: never exit without something to bring the
  gateway back), only while BGOS_AUTO_UPDATE allows it, and never a fourth
  time onto a target that already failed to come up three times.
- `decide_safe_moment`: WHEN. The update_now drain's busy definition (no
  active session, no pending plugin task, no running Hermes background
  process) HELD for the whole quiet window,
  plus 10 minutes with no inbound or outbound message, plus any backoff
  after an attempt. It never cancels or interrupts a turn: an unsafe moment
  is simply not yet.
- the attempt record: a tiny JSON file in the process Hermes home that
  survives the very restart it counts (a restart that keeps landing on the
  old code must not loop forever).
- the visible outcome: a failed or exhausted run reaches the app as the
  heartbeat `lastError` (backend HeartbeatErrorDto), kept in a report file
  beside the attempt record so the next process can say it again, or clear
  it (`lastError: null`) once the target landed.

Everything here is pure or best-effort file IO; the adapter owns the clock,
the probes and the effects.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .self_update import decide_version_update, parse_version_tuple

log = logging.getLogger(__name__)

# Quiet window: no message in or out, and idle held, for this long before an
# unattended restart. Long enough that a person mid conversation is never
# cut off between two messages; the same 10 minutes the watcher uses for
# Claude Code agents (design section 6).
QUIET_SECONDS = 10 * 60

# After any attempt (and after a failed pull or spawn), wait this long before
# the next one: design section 5, never more than one restart per agent per
# 30 minutes.
RETRY_SECONDS = 30 * 60

# A target that failed to come up this many times is left for a human
# (design section 5: at most 3 attempts per target version).
MAX_ATTEMPTS_PER_TARGET = 3

# Soak: the unattended apply takes a target only once it has been on
# origin/main this long (git committer time of the fetched commit), so a bad
# release can be pulled before every supervised host takes it on its own.
# update_now does not wait: a person asked for it.
SOAK_SECONDS = 24 * 60 * 60

ATTEMPTS_FILENAME = "bgos_scheduled_update.json"
REPORT_FILENAME = "bgos_scheduled_update_error.json"

# The heartbeat lastError codes a scheduled run reports (backend
# HeartbeatErrorDto: code <= 64 characters, message <= 300, `at` ISO 8601).
# Neither is the reserved session_unresponsive code, so presence still reads
# the agent as healthy: it answers, its update did not take.
FAILED_CODE = "scheduled_update_failed"
EXHAUSTED_CODE = "scheduled_update_exhausted"
_ERROR_CODE_MAX = 64
_ERROR_MESSAGE_MAX = 300


@dataclass(frozen=True)
class ScheduledPlan:
    """`action` is none, apply (pull then restart) or restart (already
    staged); `reason` is a short log token; `target_version` the version
    the restart should land on, when known."""

    action: str
    reason: str
    target_version: str | None = None


@dataclass(frozen=True)
class SafeMoment:
    safe: bool
    reason: str


def decide_scheduled_update(
    *,
    current: str | None,
    latest: str | None,
    pending: str | None,
    auto_update_enabled: bool,
    supervised: bool,
    attempts: Mapping[str, int],
) -> ScheduledPlan:
    """Pure: what the scheduled loop should do right now.

    `current` is the running version, `latest` the daily-checked newest at
    the pinned source, `pending` the on-disk clone version when it differs
    from the running one (self_update.pending_restart_version), `attempts`
    restarts already tried per target version.
    """
    if not auto_update_enabled:
        return ScheduledPlan("none", "updates_disabled")
    if not supervised:
        # D8: unsupervised hosts keep today's behaviour. Nothing is pulled
        # and nothing exits; update_now still stages there.
        return ScheduledPlan("none", "unsupervised")

    staged = pending if decide_version_update(current, pending) else None
    baseline = staged or current
    if decide_version_update(baseline, latest):
        plan = ScheduledPlan("apply", "newer_available", latest)
    elif staged is not None:
        plan = ScheduledPlan("restart", "staged", staged)
    else:
        return ScheduledPlan("none", "up_to_date")

    if attempts.get(plan.target_version or "", 0) >= MAX_ATTEMPTS_PER_TARGET:
        return ScheduledPlan("none", "attempts_exhausted", plan.target_version)
    return plan


def next_idle_since(
    *, busy: bool, idle_since: float | None, now: float,
) -> float | None:
    """Pure: when the current unbroken idle stretch began. Any busy sample
    resets it, so idle must hold continuously for the quiet window."""
    if busy:
        return None
    return now if idle_since is None else idle_since


def decide_safe_moment(
    *,
    now: float,
    busy: bool,
    idle_since: float | None,
    last_message_at: float | None,
    not_before: float | None,
    quiet_seconds: float = QUIET_SECONDS,
    busy_reason: str = "busy",
) -> SafeMoment:
    """Pure: is this a moment an unattended restart interrupts nothing?
    `busy_reason` names what is busy (busy, or background_job for a Hermes
    background process; finding 9)."""
    if busy:
        return SafeMoment(False, busy_reason)
    if not_before is not None and now < not_before:
        return SafeMoment(False, "backoff")
    if idle_since is None or now - idle_since < quiet_seconds:
        return SafeMoment(False, "settling")
    if last_message_at is not None and now - last_message_at < quiet_seconds:
        return SafeMoment(False, "recent_message")
    return SafeMoment(True, "safe")


# -----------------------------------------------------------------------------
# Attempt record
# -----------------------------------------------------------------------------


def attempts_path() -> Path:
    """The record lives in the PROCESS Hermes home (what the supervisor
    started the gateway with), not a multiplexed profile's home, so every
    adapter in the process reads the same count."""
    home = os.environ.get("HERMES_HOME", "").strip()
    root = Path(home) if home else Path.home() / ".hermes"
    return root.expanduser() / ATTEMPTS_FILENAME


def load_attempts(path: Path) -> dict[str, int]:
    """`{target_version: attempts}`; missing or malformed reads as empty
    (worst case one extra attempt, never a crash). Never raises."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }


def record_attempt(path: Path, target: str) -> dict[str, int]:
    """Count one more restart onto `target`. Only the current target is
    kept: a new version starts from zero. Best effort: a write failure is
    logged and the in-memory count still returned. Never raises."""
    attempts = {target: load_attempts(path).get(target, 0) + 1}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(attempts), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.warning("scheduled update attempt record not written: %s", path)
    return attempts


def clear_record(path: Path) -> None:
    """Forget a record (attempts or report). Best effort, never raises."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("scheduled update record not removed: %s", path)


# -----------------------------------------------------------------------------
# Visible outcome (heartbeat lastError)
# -----------------------------------------------------------------------------


def landed(target: str | None, current: str | None) -> bool:
    """Pure: the running version is `target` or past it (same major), so a
    restart onto it took. Unparsable versions never count as landed."""
    if parse_version_tuple(target) is None or parse_version_tuple(current) is None:
        return False
    return target == current or decide_version_update(target, current)


def last_error(code: str, message: str, *, at: float) -> dict:
    """The heartbeat lastError object, bounded to the backend DTO. `at` is
    epoch seconds (the adapter's wall clock)."""
    stamp = datetime.fromtimestamp(at, tz=timezone.utc)
    return {
        "code": code[:_ERROR_CODE_MAX],
        "message": message[:_ERROR_MESSAGE_MAX],
        "at": stamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def report_path() -> Path:
    """Beside the attempt record, in the PROCESS Hermes home."""
    return attempts_path().with_name(REPORT_FILENAME)


def load_report(path: Path) -> dict | None:
    """`{target, lastError}` of the last reported failure, else None
    (missing or malformed reads as nothing reported). Never raises."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("target"), str):
        return None
    error = raw.get("lastError")
    if not isinstance(error, dict) or not all(
        isinstance(error.get(key), str) for key in ("code", "message", "at")
    ):
        return None
    return {
        "target": raw["target"],
        "lastError": {key: error[key] for key in ("code", "message", "at")},
    }


def save_report(path: Path, target: str, error: dict) -> None:
    """Keep the reported failure for the next process. Best effort: a write
    failure is logged (the heartbeat already carried it). Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"target": target, "lastError": error}), encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        log.warning("scheduled update report not written: %s", path)
