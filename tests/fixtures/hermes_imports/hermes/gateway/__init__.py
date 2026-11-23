"""Fixture Hermes ``gateway`` package: lazy exports, like the real one."""

from importlib import import_module

_EXPORTS = {"GatewayRunner": ".run"}


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)
