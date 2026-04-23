"""BGOS channel adapter for Hermes.

Paired with a thin private fork of NousResearch/hermes-agent (~80 lines of
registration boilerplate across the 16 integration points listed in the
fork's gateway/platforms/ADDING_A_PLATFORM.md). All adapter logic, REST
client, Socket.IO client, and CLI tooling lives in this package.
"""

__version__ = "0.1.0"
