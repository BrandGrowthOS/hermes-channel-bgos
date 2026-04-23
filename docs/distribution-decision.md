# Distribution decision — Option D (thin fork + vendor plugin)

**Date:** 2026-04-23
**Status:** decided
**Supersedes:** the preliminary "Path A vs Path B" framing in the design spec's §1 (investigation disproved Path A).

## What we investigated

Goal: find a way to register `Platform.BGOS` with Hermes so the gateway instantiates `BGOSAdapter` at startup — without maintaining a public fork of `NousResearch/hermes-agent` and without making BGOS public.

## What the Hermes source revealed (2026-04-23, `main`)

| Finding | Impact |
|---|---|
| `Platform` is a closed `Enum` at `gateway/config.py:48-69` (21 members). No dynamic extension. | A plugin cannot add `BGOS` without a source-level change. |
| `GatewayRunner._create_adapter` in `gateway/run.py:~2737` is a hardcoded `if/elif platform == Platform.X:` chain, called from lines 2075 + 2463. | Adding BGOS requires a new `elif` branch in that function. |
| No `importlib.metadata.entry_points(group=...)` or `pkg_resources.iter_entry_points` anywhere in the repo. The sole `entry_points` hit is an unrelated console script. | **Path A (entry_points plugin discovery) is impossible.** |
| `gateway/platforms/__init__.py` uses a static `__all__` list. No `pkgutil.iter_modules` auto-registration. | Dropping files into `gateway/platforms/` does not auto-register. |
| `gateway/platforms/ADDING_A_PLATFORM.md` lists **16 integration points** that must all be touched when adding a platform: `gateway/config.py`, `gateway/run.py` (factory + two auth maps), `gateway/session.py`, `agent/prompt_builder.py` PLATFORM_HINTS, `toolsets.py`, `cron/scheduler.py`, `tools/send_message_tool.py`, `tools/cronjob_tools.py`, `gateway/channel_directory.py`, `hermes_cli/status.py`, `hermes_cli/gateway.py`, `agent/redact.py`, + the `gateway/platforms/bgos.py` file itself. | "Minimal patcher" is not a realistic option — miss any of them and features (cron, send_message tool, setup wizard, redaction) silently break. |
| `BasePlatformAdapter` is a stable public export at `from gateway.platforms.base import BasePlatformAdapter`. | Our adapter can import it safely from an external package. |
| `BasePlatformAdapter` has only 4 abstract methods: `connect()`, `disconnect()`, `send()`, `get_chat_info()`. Everything else (media sends, typing, edit_message) has base-class default stubs and is optional. | Task 6 media sends are optional overrides, not required. |
| Hermes targets `requires-python = ">=3.11"` in its `pyproject.toml`. | We match. |
| Hermes has no `send_exec_approval(...)` API. Approvals are text-based: agent posts a prompt, user replies with `/approve` or `/deny`, gateway routes via `should_bypass_active_session` in `base.py:~1940-1965`. | The design spec's §5 structured-approval payload is still correct on the BGOS side, but the adapter-side translation changes. See §5 revisions (next commit). |

## The three distribution options we considered

- **Option A — full install-time patcher.** `hermes-channel-bgos install` AST/regex-patches all 16 points in the user's Hermes install. Works, but patches are fragile across Hermes's daily upstream updates; every upstream change to a patched line is silent breakage.
- **Option B / D — private fork of NousResearch/hermes-agent.** Kc owns a private fork; nightly `git rebase upstream/main` pulls updates. Fork diff is small (~80 lines of registration boilerplate across the 16 points) because all real BGOS logic lives in this vendor package. Daily updates remain trivial to pull.
- **Option C — minimal patcher (only `config.py` + factory).** Smallest surface, but `ADDING_A_PLATFORM.md` explicitly warns that skipping the other 14 points breaks cron delivery, the `send_message` tool, redaction, the CLI status banner, and the setup wizard. Rejected.

## Chosen — Option D (thin fork + vendor plugin)

A **private fork** of NousResearch/hermes-agent, kept current with upstream via nightly auto-rebase, containing only:

- One `elif` branch per touched file from `ADDING_A_PLATFORM.md` — ~80 lines total.
- `gateway/platforms/bgos.py` as a **5-line shim**:
  ```python
  """BGOS channel adapter — thin shim; real logic lives in the
  hermes-channel-bgos pip package (bgos-monorepo)."""
  from hermes_channel_bgos.bgos_adapter import BGOSAdapter
  __all__ = ["BGOSAdapter"]
  ```

All BGOS-specific code — adapter class, REST client, Socket.IO client, pair CLI, tests — lives in this package (`hermes-channel-bgos/` in the BGOS monorepo), distributed via `pip install .` (or a private PyPI if we want to later).

### Why Option D over Option A

| | Option D (fork) | Option A (patcher) |
|---|---|---|
| Daily Hermes updates | `git rebase upstream/main` — clean nightly | `pip upgrade hermes && hermes-channel-bgos install` — silent breakage if upstream moves any patched line |
| Version pinning | Not needed — rebase continuously | Required — pin Hermes to a tested version range |
| "How do I know something broke" | Rebase fails with conflict markers — visible | Patcher may succeed but produce wrong code — runtime surprise |
| Complexity to build | Small diff, 16 files, mostly one-liners | AST/regex patcher for each of 16 files with rollback/manifest logic |
| Does it make BGOS public? | No — fork is private | No |

### Fork maintenance workflow

1. Kc creates a private fork: `brandgrowthos/hermes-agent-bgos` (exact name TBD — see next-actions).
2. Add upstream: `git remote add upstream https://github.com/NousResearch/hermes-agent`.
3. Apply the BGOS integration commit on top of `upstream/main` (prepared in Phase 1 Task 11).
4. GitHub Action runs nightly: `git fetch upstream && git rebase upstream/main && git push --force-with-lease`. On conflict, the Action fails with a notification to Kc.
5. When the user runs the installer, they `git clone` the fork in place of vanilla Hermes (or point their existing Hermes install to the fork via a `pip install -e <fork>` workflow).

## Next actions (tracked in Phase 1 plan)

- Phase 1 Task 11 prepares the fork commit: 16 patches + the 5-line shim + a FORK-NOTES.md explaining the integration. No fork is created yet; Kc creates the private repo when he's ready and we push the prepared commit.
- Phase 1 Task 13's README documents the fork-clone install path for the end user.
