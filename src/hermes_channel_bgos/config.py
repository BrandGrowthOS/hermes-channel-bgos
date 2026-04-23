"""Adapter configuration — a plain dataclass passed around by value."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BgosConfig:
    base_url: str
    pairing_token: str | None
    device_label: str = ""
    request_timeout_seconds: float = 30.0
