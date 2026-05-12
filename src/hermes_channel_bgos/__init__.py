"""BGOS channel adapter for Hermes.

Paired with a thin private fork of NousResearch/hermes-agent (~80 lines of
registration boilerplate across the 16 integration points listed in the
fork's gateway/platforms/ADDING_A_PLATFORM.md). All adapter logic, REST
client, Socket.IO client, and CLI tooling lives in this package.
"""

import logging as _logging
import os as _os

__version__ = "0.5.1"


def _maybe_enable_debug_logging() -> None:
    """Honor `BGOS_DEBUG=1` by bumping our two chatty modules to DEBUG.

    Lets operators flip on raw WS event + batch flush visibility for one
    diagnostic session without editing Hermes's global logging config.
    Idempotent; safe to call on every import.

    Set BGOS_DEBUG=1 (or "true" / "yes" — anything truthy that isn't "0",
    "false", or "no") in your service env to enable. Unset / default keeps
    logging at the level Hermes's launcher configured.
    """
    val = _os.environ.get("BGOS_DEBUG", "").strip().lower()
    if val in ("", "0", "false", "no", "off"):
        return
    for name in ("hermes_channel_bgos.bgos_ws",
                 "hermes_channel_bgos.bgos_adapter"):
        logger = _logging.getLogger(name)
        if logger.level == _logging.NOTSET or logger.level > _logging.DEBUG:
            logger.setLevel(_logging.DEBUG)


_maybe_enable_debug_logging()
