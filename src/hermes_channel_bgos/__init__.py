"""BGOS channel adapter for Hermes.

Paired with a thin private fork of NousResearch/hermes-agent (~80 lines of
registration boilerplate across the 16 integration points listed in the
fork's gateway/platforms/ADDING_A_PLATFORM.md). All adapter logic, REST
client, Socket.IO client, and CLI tooling lives in this package.
"""

import logging as _logging
import os as _os
import sys as _sys

__version__ = "0.19.0"


# Sentinel attribute set on a handler we install so subsequent imports
# can detect-and-skip without stacking duplicates.
_BGOS_DEBUG_HANDLER_MARK = "_bgos_debug_handler"


def _maybe_enable_debug_logging() -> None:
    """Honor `BGOS_DEBUG=1` by routing our two chatty modules' DEBUG output
    to stderr (and thus to systemd-journald when Hermes runs as a user
    service).

    Why a dedicated handler rather than just `logger.setLevel(DEBUG)`:
    Hermes's launcher installs its own handlers on the root logger at
    INFO level. With `propagate=True` (the default), our module's DEBUG
    records bubble up and get filtered out at the root's handler level —
    they never reach any output sink. Solution: attach our own stderr
    handler directly to our package logger, set to DEBUG. The handler
    accepts DEBUG records that the module logger emits; root's handler
    still gets a copy via propagation, where it's filtered at INFO (as
    before). INFO/WARNING/ERROR end up duplicated on stderr (acceptable
    for a diagnostic build); DEBUG only appears on stderr (the whole
    point).

    Caught live on kc's server 2026-05-12: 0.5.1 set the module levels
    correctly but DEBUG output never reached gateway.log or journal because
    Hermes's root-logger handlers were filtering at INFO.

    Idempotent — re-importing the package doesn't stack handlers (we
    detect our previously-installed handler via a sentinel attribute and
    skip).

    Set BGOS_DEBUG=1 (or "true" / "yes" / "on") in your service env to
    enable. Default off; no observable change without the env var.
    """
    val = _os.environ.get("BGOS_DEBUG", "").strip().lower()
    if val in ("", "0", "false", "no", "off"):
        return

    # Attach to the package-root logger so child loggers
    # (bgos_ws, bgos_adapter) inherit the level and the handler.
    pkg_logger = _logging.getLogger("hermes_channel_bgos")
    pkg_logger.setLevel(_logging.DEBUG)

    # Idempotency: don't stack handlers across re-imports / reloads.
    for existing in pkg_logger.handlers:
        if getattr(existing, _BGOS_DEBUG_HANDLER_MARK, False):
            return

    handler = _logging.StreamHandler(_sys.stderr)
    handler.setLevel(_logging.DEBUG)
    handler.setFormatter(
        _logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    setattr(handler, _BGOS_DEBUG_HANDLER_MARK, True)
    pkg_logger.addHandler(handler)


_maybe_enable_debug_logging()
