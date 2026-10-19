"""Sustained credential rejection: notice it, pause, and say so once.

After the owner revokes a pairing, a still running gateway keeps sending the
dead token on every poll tick (about 17,000 refused calls a day at the default
5 s interval, one traceback each). The backend answers a plain 401 "Invalid
pairing token" with no machine-readable code, so the only signal is the status
itself: count CONSECUTIVE 401s across every authenticated call.

Mirrors lib/auth-rejection.ts in the Claude Code plugin (same thresholds: a
count AND a duration, so a burst of calls that fail together when a token
rotates is not mistaken for a revocation) and adds what that file leaves out on
purpose: the back-off. Once paused, normal polling stops and the caller sends
one slow self probe every `AUTH_PAUSED_PROBE_SECONDS`. The first answer that is
not a refusal resumes everything, so a transient auth blip never kills polling
for good.

Only 401 counts as a rejection. A 403 is a permission answer about one
resource, and a 5xx or a timeout is the server having a bad day; neither says
our credential is refused. Any other answer ends the run.
"""
from __future__ import annotations

import time
from typing import Callable

# Consecutive 401s before the gateway will consider pausing...
AUTH_REJECTION_MIN_COUNT = 30
# ...and the run must also have lasted this long (seconds).
AUTH_REJECTION_MIN_SECONDS = 60.0
# While paused: one probe this often (minutes, not seconds).
AUTH_PAUSED_PROBE_SECONDS = 300.0

PAUSED = "paused"
RESUMED = "resumed"


class AuthRejectionTracker:
    """Folds each authenticated call's HTTP status into a paused/running state.

    `observe` returns `PAUSED` on the call that tips the state into paused,
    `RESUMED` on the first non-refusal answer while paused, otherwise None, so
    the caller logs exactly one line per transition.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.consecutive = 0
        self.first_at: float | None = None
        self.paused = False

    def observe(self, status: int) -> str | None:
        if status == 401:
            now = self._clock()
            self.consecutive += 1
            if self.first_at is None:
                self.first_at = now
            if (
                not self.paused
                and self.consecutive >= AUTH_REJECTION_MIN_COUNT
                and now - self.first_at >= AUTH_REJECTION_MIN_SECONDS
            ):
                self.paused = True
                return PAUSED
            return None
        if self.paused:
            if status >= 500:
                # The server is unwell: that proves nothing about the token, so
                # stay paused and let the next slow probe find out.
                return None
            self._reset()
            return RESUMED
        if self.consecutive:
            self._reset()
        return None

    def force_pause(self) -> bool:
        """The server said the pairing was revoked (the `pairing_revoked`
        WebSocket event): pause now, no count needed. False if already paused."""
        if self.paused:
            return False
        self.paused = True
        return True

    def seconds_rejected(self) -> float:
        if self.first_at is None:
            return 0.0
        return max(0.0, self._clock() - self.first_at)

    def _reset(self) -> None:
        self.consecutive = 0
        self.first_at = None
        self.paused = False
