"""BGOS channel adapter — thin shim. Real logic lives in the
hermes-channel-bgos pip package (install separately).

This file is intended to be copied verbatim into the private Hermes
fork at `gateway/platforms/bgos.py`. Keep this file ≤ 10 lines — any
helper or additional import belongs in the vendor package. See
../FORK-NOTES.md for the rationale.
"""
from hermes_channel_bgos.bgos_adapter import BGOSAdapter

__all__ = ["BGOSAdapter"]
