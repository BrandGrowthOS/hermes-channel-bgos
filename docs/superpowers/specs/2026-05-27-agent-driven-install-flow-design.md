# hermes-channel-bgos v0.8.0 — agent-driven install that just works

**Date:** 2026-05-27
**Status:** Approved (brainstorming)
**Branch:** `feat/install-flow-v0.8` (off `origin/main` @ 0.7.0)

## Goal

A user points an AI agent (Claude Code) at this repo and says "install this
plugin." If a pairing key is available, the integration comes up and messages
round-trip without manual intervention. If no pairing key exists yet, the agent
gets one clear instruction to obtain one. The two failure modes that bit a real
fresh install are eliminated:

1. **Post-exposure cache staleness.** The user exposed an agent in BGOS *after*
   the gateway started. The running adapter had cached zero bound assistants at
   startup, so inbound messages for the new `assistant_id` were dropped forever
   until a manual restart.
2. **No single command to verify state.** Diagnosing the above required reading
   logs and reasoning about adapter internals.

This work is the subset of the operator's (Jeff's) 11 recommendations that fits
an *agent-driven* install. An interactive setup wizard (his #1) and a native
`hermes plugins install` command (his #11) are deliberately out of scope: an
agent runs non-interactive commands and parses machine-checkable output rather
than answering prompts, and Hermes has no entry-point plugin discovery (see the
note in `pyproject.toml`).

## Non-goals

- Interactive `setup_wizard` that prompts a human and edits systemd from Python.
- `hermes plugins install` / `hermes gateway setup bgos` native integration.
- Profile auto-detection beyond the existing `HERMES_HOME` mechanism.

## Components

### 1. Hot-refresh on unknown `assistant_id` (the centerpiece) — `bgos_adapter.py`

New method `_refresh_pairing_scope()`:

- Re-runs `GET /api/v1/integrations/me` (`whoami`).
- Diffs the returned assistants against `StateStore.assistant_route`:
  - new ids → `set_route`, collect for WS bind + command sync;
  - removed ids → `remove_assistant` + `_ws.unbind_assistant`.
- Calls `self._ws.bind_assistants(list(routes))` so new `assistant:<id>` rooms
  are joined immediately (already supported by `BgosWs.bind_assistants`).
- Fires `sync_commands_for(aid)` for each newly-added assistant (fail-open).
- Refreshes `pairing_user_id` if it was previously unset.
- Logs `bgos scope refreshed: added=[...] removed=[...] bound=[...]`.

Concurrency / rate-limit:

- Guarded by `self._scope_refresh_lock: asyncio.Lock` so concurrent
  unknown-assistant inbound (live WS + poll loop) don't double-`whoami`.
- Monotonic cooldown `self._last_scope_refresh: float` with
  `BGOS_SCOPE_REFRESH_COOLDOWN` (default 10s). Inside the lock, if the last
  refresh was within the cooldown, return early without calling `whoami`. This
  prevents a genuinely-unknown id (one that will never resolve) plus the 5s poll
  loop from hammering the endpoint.

`_handle_inbound` change — the existing drop branch:

```python
route = self._state.get_route(assistant_id)
if route is None:
    log.info("inbound for unknown assistant_id=%s — refreshing pairing scope",
             assistant_id)
    await self._refresh_pairing_scope()
    route = self._state.get_route(assistant_id)
    if route is None:
        log.warning("assistant_id=%s still unknown after refresh — dropping",
                    assistant_id)
        return
# ... falls through and processes the SAME message
```

Net effect: exposing an agent after gateway start self-heals within ~one poll
cycle (≤ poll interval + cooldown). No restart required.

### 2. `hermes-bgos-doctor` — new `doctor.py` + console script

Non-interactive diagnostic. Each check yields a structured result
`{name, status: OK|WARN|FAIL, detail, fix?}`. Rendered as human text by default,
or `--json` for agent parsing. Exit code `1` if any check is FAIL, else `0`
(WARN does not fail).

Checks:

1. **Package** — `import hermes_channel_bgos`; report `__version__`.
2. **Fork patch / registration** — `from gateway.config import Platform;
   Platform.BGOS` and `from gateway.platforms.bgos import BGOSAdapter`. FAIL with
   "run from Hermes's Python env / apply the fork patch" if `gateway` missing.
3. **Config / pairing token** — resolve via `BGOSAdapter._resolve_config(None)`;
   report secrets-file path (HERMES_HOME-aware) and whether it exists. If no
   token: FAIL with the canonical instruction — *"Not paired. In BGOS open
   Integrations → Hermes → Connect a new Hermes server, copy the BGOS-XXXX-XX
   code, then run `hermes-pair-bgos <CODE> --device-label <host>`."* This is the
   "no pairing key → give instructions" path.
4. **Env** — `BGOS_AGENTS` / `BGOS_AGENTS_JSON` present? `BGOS_ALLOW_ALL_USERS`
   or `BGOS_ALLOWED_USERS` set? WARN with the consequence if missing.
5. **Catalog configured** — `_enumerate_agents()` count + list.
6. **Live `whoami`** (network; skip with `--offline`) — pairing id, owner,
   exposed assistants (id/route/name). Validates the token (401 → FAIL
   "PAIRING_REVOKED — re-pair"). If catalog is configured but zero assistants are
   exposed: WARN "open Integrations → tick agent(s) → Save (new exposures
   hot-load, no restart needed on ≥0.8.0)".
7. **Gateway process** (best-effort, informational only, never FAIL) — `ps`
   match for a running hermes/gateway process.

The doctor does NOT need to detect the old stale-cache condition: hot-refresh
makes the running adapter self-correct, so the doctor's job is "is everything
configured and is pairing live + are agents exposed."

### 3. Pair CLI: `--agents` + `--wait-for-exposure` — `pair_cli.py`

- `--agents "default:David,hades:Hades"` — builds an agent catalog and (a) passes
  it to `pair_exchange(agent_catalog=...)` and (b) explicitly calls authenticated
  `push_agent_catalog(pairing_id, entries)` after writing the secret. Belt-and-
  suspenders so the catalog lands regardless of whether `pair-exchange` persists
  it — the user can tick agents in the UI *before the gateway ever starts*.
- `--wait-for-exposure` — after pairing (+ catalog), polls `whoami` every
  `--wait-interval` (default 4s) up to `--wait-timeout` (default 180s), printing
  "Waiting for you to expose an agent in BGOS… Open Integrations → Hermes → tick
  agent(s) → Save." On success prints bound assistants (id/route/name), exit 0.
  On timeout prints guidance, exit non-zero.

### 4. Shared agent-spec parser

Extract the `route:Name` comma-format parsing currently inline in
`_enumerate_agents` into a module-level `parse_agents_spec(raw: str) -> list[dict]`
(new small module `agents.py`). Used by both `_enumerate_agents` (the
`BGOS_AGENTS` branch) and `pair_cli --agents`. Single source of truth for the
format.

### 5. Clearer startup logs — `bgos_adapter.py`

- On catalog push: `BGOS catalog pushed: default:David, hades:Hades`.
- After binding in `connect()`: bound-assistants line with routes; when zero are
  exposed, an actionable WARNING that exposures hot-load (no restart needed).

### 6. Docs + skill

- **README** — the "Quick start with Claude Code" prompt is the in-repo agent
  playbook (what makes "point at the repo" work). Add the `--agents` flag to the
  pairing step, add a `hermes-bgos-doctor` verify step, drop the mandatory
  post-exposure restart, add the `unknown assistant_id` troubleshooting row
  (now self-healing), and a `HERMES_HOME`/profile env-path note.
- **Skill** `bgos-integrate-hermes-agent/SKILL.md` — propagate the same: `--agents`
  at pair time, doctor step, hot-refresh gotcha (no restart), updated
  troubleshooting table.

### 7. Version

Bump `0.7.0` → `0.8.0` in `pyproject.toml` and `__init__.py`. Add the
`hermes-bgos-doctor` console script in `pyproject.toml`.

## Testing (TDD)

Follow the repo's existing `pytest` + `asyncio_mode=auto` + in-repo mock patterns
(`tests/mocks/mock_bgos_server.py`, `mock_hermes.py`, `conftest.py`).

- **Hot-refresh** (`test_bgos_adapter_inbound.py` or new): inbound for an unknown
  assistant triggers a `whoami` refresh, binds the new room, and the retried
  message is processed; cooldown prevents repeated `whoami` within the window;
  an id still unknown after refresh is dropped with the warning.
- **Pair CLI** (`test_pair_cli.py`): `--agents` parses and pushes the catalog;
  `--wait-for-exposure` polls until assistants appear (mocked) and on timeout.
- **Doctor** (new `test_doctor.py`): check results render OK/WARN/FAIL; `--json`
  shape; exit codes; not-paired path; `whoami` 401 path.
- **Parser** (new `test_agents.py`): `parse_agents_spec` edge cases (bare route,
  `route:Name`, whitespace, empty pieces).

## Risks / trade-offs

- Cooldown could delay recovery by up to its window when an id is exposed right
  after a failed refresh. Acceptable: bounded to ~10s + poll interval, and
  configurable.
- `pair_exchange` may or may not persist the catalog server-side; the explicit
  `push_agent_catalog` after pairing makes `--agents` robust either way.
- Branch is off `origin/main` (0.7.0); the unmerged `feat/agent-self-status`
  feature will merge independently — minor version/README/docs conflicts to
  resolve at merge time (accepted by the user).
