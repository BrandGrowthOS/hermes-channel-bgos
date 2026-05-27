# BGOS as a Hermes plugin — retire the fork patch

**Date:** 2026-05-27
**Status:** Approved (brainstorming)
**Branch:** `feat/bgos-hermes-plugin` (off `main` @ v0.8.0)

## Goal

Replace the 11-file Hermes fork patch (`hermes-fork-patch/0001-bgos-integration.patch`)
with a drop-in **Hermes plugin** for plugin-capable Hermes installs, while keeping
the fork patch as a documented fallback for older Hermes that predates the plugin
loader. This eliminates the rebase-on-every-upstream-update burden and the
corrupt-patch class of install failures (the one that blocked a real install — see
PR #6), and it closes the cron-delivery gap (`send_message_tool` returning
"Direct sending not yet implemented for bgos") as a side effect.

## Background — what we verified

As of 2026-05-27, upstream `NousResearch/hermes-agent` `main` ships a mature plugin
system (`gateway/platform_registry.py`, `hermes_cli/plugins.py`, a build guide, an
interface test, and **working first-party example plugins**: `discord`, `teams`,
`ntfy`, `google_chat`, `simplex`, `line` under `plugins/platforms/`).

A plugin is a directory — `~/.hermes/plugins/<name>/` (user) or `plugins/platforms/<name>/`
(bundled) — containing `plugin.yaml` + `adapter.py` with a `register(ctx)` entry point.
**No pip package and no core source edits are required by Hermes itself.**

The single `ctx.register_platform(...)` call covers every fork-patch touch-point. From
the real `ntfy` adapter (`plugins/platforms/ntfy/adapter.py`), the args are:

| `register_platform(...)` arg | Replaces fork touch-point |
|---|---|
| `name`, `label`, `emoji` | enum + status/CLI labels |
| `adapter_factory=lambda cfg: BGOSAdapter(cfg)` | `gateway/run.py` `_create_adapter` factory + `gateway/platforms/bgos.py` shim |
| `env_enablement_fn` (returns a dict; special `home_channel` key → `HomeChannel` dataclass) | `gateway/config.py` `_apply_env_overrides` + home-channel wiring |
| `cron_deliver_env_var` | `cron/scheduler.py` `_HOME_TARGET_ENV_VARS` |
| `standalone_sender_fn` | `cron/scheduler.py` delivery + `tools/send_message_tool.py` `_send_to_platform` (closes the #9b gap) |
| `allowed_users_env`, `allow_all_env` | `gateway/run.py` auth/allow-all maps |
| `platform_hint` | `agent/prompt_builder.py` `PLATFORM_HINTS` |
| `max_message_length`, `pii_safe`, `allow_update_command` | toolsets / redact / update-command bits |
| `check_fn`, `validate_config`, `is_connected`, `required_env`, `install_hint` | health/validation surface |

Min-version is a non-issue: the installer/doctor detect plugin support at runtime
rather than hardcoding a version.

## Non-goals

- Removing the fork patch (it stays as the fallback for pre-plugin Hermes).
- Changing any adapter behavior (`BGOSAdapter`, `BgosApi`, `BgosWs` are reused as-is).
- Upstreaming the plugin to Hermes / publishing to any plugin registry.

## Components

### 1. New plugin directory (in the vendor repo)

```
plugins/platforms/bgos/
  __init__.py
  plugin.yaml        # name: bgos-platform, label: BGOS, kind: platform, version,
                     # description, author, requires_env/optional_env (BGOS_AGENTS,
                     # BGOS_ALLOW_ALL_USERS, BGOS_ALLOWED_USERS, BGOS_BACKEND_URL,
                     # BGOS_HOME_CHANNEL, BGOS_HOME_CHANNEL_NAME, BGOS_POLL_INTERVAL,
                     # BGOS_SCOPE_REFRESH_COOLDOWN)
  adapter.py         # thin shim — see below
```

`adapter.py` contains **no business logic** — it imports `BGOSAdapter` from the pip
package and defines the registration shim:

```python
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.plugin import (
    env_enablement, standalone_send, BGOS_PLATFORM_HINT,
)

def register(ctx) -> None:
    ctx.register_platform(
        name="bgos",
        label="BGOS",
        emoji="📱",
        adapter_factory=lambda cfg: BGOSAdapter(cfg),
        env_enablement_fn=env_enablement,
        cron_deliver_env_var="BGOS_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        allowed_users_env="BGOS_ALLOWED_USERS",
        allow_all_env="BGOS_ALLOW_ALL_USERS",
        platform_hint=BGOS_PLATFORM_HINT,
        max_message_length=<existing _DEFAULT_MAX_MESSAGE_LENGTH>,
        pii_safe=True,
        allow_update_command=True,
        # check_fn / validate_config / is_connected: optional, add if Hermes requires
    )
```

### 2. New `hermes_channel_bgos/plugin.py` (in the pip package)

Holds the hook functions so they're versioned + unit-tested with the rest of the
package (the plugin dir's `adapter.py` just imports them):

- `env_enablement() -> dict | None` — reads `BGOS_BACKEND_URL`, `BGOS_HOME_CHANNEL`,
  `BGOS_HOME_CHANNEL_NAME` from env; returns a seed dict (`{"backend_url": ...,
  "home_channel": {"chat_id": ..., "name": ...}}`) or `None` if not minimally
  configured (no pairing secret + no `BGOS_API_KEY`). Mirrors ntfy's `_env_enablement`.
- `async standalone_send(pconfig, chat_id, message, *, thread_id=None,
  media_files=None, force_document=False) -> dict` — resolves the pairing token +
  base_url (same precedence as `BGOSAdapter._resolve_config`: env `BGOS_API_KEY` →
  `~/.hermes/secrets/bgos.json`; `BGOS_BACKEND_URL` → secret → prod default),
  constructs a `BgosApi`, and calls `post_message(chat_id=int(chat_id), text=message)`.
  Returns `{"success": True, "platform": "bgos", "chat_id": ..., "message_id": ...}`
  or `{"error": ...}`. This is the cron-delivery fix.
- `BGOS_PLATFORM_HINT: str` — the agent-facing capabilities doc currently embedded in
  the fork patch's `PLATFORM_HINTS` entry (single source of truth; the fork patch can
  later import/reference it too, but that's out of scope here).

### 3. Install flow (README + skill)

Detection: plugin-capable if `python -c "import gateway.platform_registry"` succeeds
(verify the exact module/probe against the installed Hermes at implementation time).

- **Plugin path (primary):** `pip install` / `uv pip install --python … -e ~/hermes-channel-bgos`
  (unchanged) **+** make the plugin discoverable: `mkdir -p ~/.hermes/plugins` and
  symlink (or copy) `~/hermes-channel-bgos/plugins/platforms/bgos` →
  `~/.hermes/plugins/bgos`. No fork patch, no `git am`, no rebase. Restart Hermes.
- **Fallback path:** Hermes too old → today's fork-patch flow, unchanged.

### 4. `hermes-bgos-doctor` change

Add a `registration` check that reports `plugin` (plugin loaded / `Platform.BGOS`
present via the registry), `patch` (fork patch applied), or `none` (FAIL with the
fix: install the plugin or apply the patch). Replaces the current `fork_patch` check's
single assumption with a both-paths-aware one.

## Testing (TDD)

Follow the repo's `pytest` (`asyncio_mode=auto`) + in-repo mock patterns.

- `tests/test_plugin.py`:
  - `env_enablement` — env set → seed dict incl. `home_channel`; nothing set → `None`.
  - `standalone_send` — mock `BgosApi.post_message`; asserts token/base_url resolution
    from a tmp secrets file, correct `post_message` args, success + error shapes.
  - `register(ctx)` — a fake `ctx` records the `register_platform` kwargs; assert
    `name="bgos"`, the hooks are wired, `standalone_sender_fn`/`cron_deliver_env_var`
    present.
  - `plugin.yaml` parses (yaml.safe_load) and declares the expected `requires_env`.
- Doctor: extend `tests/test_doctor.py` for the new `registration` check states.
- Existing suite stays green.

## Risks / trade-offs

- **Exact API drift:** `ctx.register_platform` kwargs + the `home_channel` seed shape +
  `standalone_sender_fn` signature are taken from the `ntfy` reference on upstream
  `main`. Verify against the *installed* Hermes when implementing; adjust kwargs if the
  installed version differs. Low risk — it's verification against a concrete example,
  not invention.
- **Discovery detail:** confirm Hermes auto-loads `~/.hermes/plugins/<name>/` at startup
  (vs. requiring a config entry) on the target install before documenting the symlink
  step as turnkey.
- **Dependency on pip pkg:** the plugin's `adapter.py` imports `hermes_channel_bgos`, so
  the pip package must be installed in Hermes's Python (same requirement as today). A
  failed pkg install would make the plugin fail to load — acceptable and surfaced by the
  doctor.
- **Fallback kept:** because the fork patch remains for old Hermes, no existing install
  breaks during rollout.
