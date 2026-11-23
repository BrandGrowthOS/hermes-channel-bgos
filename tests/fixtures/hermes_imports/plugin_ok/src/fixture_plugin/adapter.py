"""Fixture plugin: every Hermes import shape the real plugin uses, and every
name exists in the fixture Hermes tree. Parsed by the check, never imported."""

from __future__ import annotations

import importlib
import json  # standard library: skipped

import httpx  # third party: skipped

from . import helpers  # relative, the plugin's own package: skipped
from .helpers import VALUE

try:  # pragma: no cover
    from tools.slash_confirm import resolve as resolve_slash_confirm  # type: ignore
except ImportError:
    resolve_slash_confirm = None

try:  # pragma: no cover
    from tools.approval import resolve_gateway_approval  # type: ignore
except ImportError:
    resolve_gateway_approval = None

try:  # pragma: no cover
    from gateway.config import Platform as _HermesPlatform  # type: ignore
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
except ImportError:
    # The plugin's own test mocks: skipped.
    from tests.mocks.mock_hermes import BasePlatformAdapter, SendResult  # type: ignore

import gateway.platforms.base  # type: ignore


def session_key():
    from gateway.platforms.base import build_session_key  # type: ignore

    return build_session_key


async def voice():
    try:
        from gateway.platforms.base import cache_audio_from_bytes  # type: ignore
    except ImportError:
        return None
    return cache_audio_from_bytes


def commands():
    from hermes_cli import commands as hc  # type: ignore  (a submodule)

    return hc


def profiles():
    from hermes_cli.profiles import (  # type: ignore
        create_profile,
        normalize_profile_name,
        profile_exists,
    )

    return create_profile, normalize_profile_name, profile_exists


def home():
    from hermes_constants import get_hermes_home  # type: ignore

    return get_hermes_home


def memory():
    return importlib.import_module("tools.memory_tool")


def unused():
    return json, httpx, helpers, VALUE, _HermesPlatform, MessageEvent, MessageType
