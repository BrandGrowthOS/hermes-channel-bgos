"""BGOS platform plugin entrypoint for Hermes plugin discovery."""

from .adapter import check_requirements, is_connected, register, validate_config

__all__ = ["check_requirements", "is_connected", "register", "validate_config"]
