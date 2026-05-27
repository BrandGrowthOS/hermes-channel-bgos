"""BGOS Hermes plugin — thin registration shim.

Drop this directory into ~/.hermes/plugins/bgos/ (or bundle under
plugins/platforms/). All logic lives in the `hermes_channel_bgos` pip package,
which must be installed into Hermes's Python. This file only wires the package
into Hermes's plugin registry via `register(ctx)`.

Mirrors the upstream `ntfy` plugin's registration surface.
"""
from __future__ import annotations

from hermes_channel_bgos.bgos_adapter import BGOSAdapter, _DEFAULT_MAX_MESSAGE_LENGTH
from hermes_channel_bgos.plugin import (
    BGOS_PLATFORM_HINT,
    env_enablement,
    standalone_send,
)


def check_requirements() -> bool:
    """The pip-package import above is the only hard requirement; if this
    module loaded, it's satisfied."""
    return True


def validate_config(config) -> bool:  # noqa: ARG001 - registry protocol
    return True


def is_connected(config) -> bool:  # noqa: ARG001 - registry protocol
    """Env-only configuration is enough to consider BGOS connectable; the live
    socket state is owned by the adapter once constructed."""
    return env_enablement() is not None


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="bgos",
        label="BGOS",
        emoji="📱",
        adapter_factory=lambda cfg: BGOSAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        install_hint="pip install -e ~/hermes-channel-bgos  # into Hermes's Python",
        env_enablement_fn=env_enablement,
        cron_deliver_env_var="BGOS_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        allowed_users_env="BGOS_ALLOWED_USERS",
        allow_all_env="BGOS_ALLOW_ALL_USERS",
        max_message_length=_DEFAULT_MAX_MESSAGE_LENGTH,
        pii_safe=True,
        allow_update_command=True,
        platform_hint=BGOS_PLATFORM_HINT,
    )
