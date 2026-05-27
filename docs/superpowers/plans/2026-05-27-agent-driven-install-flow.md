# Agent-Driven Install Flow (v0.8.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a fresh `hermes-channel-bgos` install self-heal the post-exposure cache race and become verifiable in one command, so an agent pointed at the repo can install it end-to-end.

**Architecture:** Hot-refresh re-syncs the assistant route map from `whoami` when an unknown `assistant_id` arrives (rate-limited, fail-open). A new `hermes-bgos-doctor` CLI reports configured/live state with inline fixes. The pair CLI gains `--agents` (push catalog at pair time) and `--wait-for-exposure` (poll until the user ticks agents). A shared `agents.py` parser is the single source of truth for the `route:Name` spec.

**Tech Stack:** Python 3.11+, `click`, `httpx`, `python-socketio`, `pytest` (`asyncio_mode=auto`), in-repo aiohttp mock backend.

**Working dir:** `/Users/kc/Projects/BGOS/hermes-channel-bgos/.claude/worktrees/install-flow-v0.8` (branch `feat/install-flow-v0.8`).

**Run tests with:** `.venv/bin/python -m pytest` (the worktree shares the repo `.venv`; if absent, `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`).

---

## File Structure

- **Create** `src/hermes_channel_bgos/agents.py` — `parse_agents_spec` + `enumerate_agents_from_env`.
- **Create** `src/hermes_channel_bgos/doctor.py` — diagnostic CLI.
- **Modify** `src/hermes_channel_bgos/bgos_adapter.py` — hot-refresh, log clarity, delegate `_enumerate_agents`.
- **Modify** `src/hermes_channel_bgos/pair_cli.py` — `--agents`, `--wait-for-exposure`, `wait_for_exposure` helper.
- **Modify** `src/hermes_channel_bgos/__init__.py` + `pyproject.toml` — version 0.8.0 + doctor console script.
- **Modify** `README.md` + the `bgos-integrate-hermes-agent` skill — docs.
- **Create** `tests/test_agents.py`, `tests/test_doctor.py`; **extend** `tests/test_bgos_adapter_inbound.py`, `tests/test_pair_cli.py`.

---

## Task 1: Shared agent-spec parser

**Files:**
- Create: `src/hermes_channel_bgos/agents.py`
- Test: `tests/test_agents.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agents.py
"""Tests for the shared agent-catalog spec parser."""
from __future__ import annotations

import pytest

from hermes_channel_bgos.agents import enumerate_agents_from_env, parse_agents_spec


def test_parse_route_name_pairs():
    assert parse_agents_spec("hades:Hades,ramy:Ramy") == [
        {"agent_route": "hades", "name": "Hades"},
        {"agent_route": "ramy", "name": "Ramy"},
    ]


def test_parse_bare_route_uses_route_as_name():
    assert parse_agents_spec("default") == [
        {"agent_route": "default", "name": "default"},
    ]


def test_parse_strips_whitespace_and_skips_empty_pieces():
    assert parse_agents_spec(" a : Alpha , , b ") == [
        {"agent_route": "a", "name": "Alpha"},
        {"agent_route": "b", "name": "b"},
    ]


def test_parse_empty_string_is_empty_list():
    assert parse_agents_spec("") == []
    assert parse_agents_spec("   ") == []


def test_enumerate_prefers_json(monkeypatch):
    monkeypatch.setenv("BGOS_AGENTS_JSON", '[{"agent_route":"x","name":"X","description":"d"}]')
    monkeypatch.setenv("BGOS_AGENTS", "y:Y")
    out = enumerate_agents_from_env()
    assert out == [{"agent_route": "x", "name": "X", "description": "d"}]


def test_enumerate_falls_back_to_comma_spec(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.setenv("BGOS_AGENTS", "default:David")
    assert enumerate_agents_from_env() == [{"agent_route": "default", "name": "David"}]


def test_enumerate_ignores_invalid_json_and_uses_comma(monkeypatch):
    monkeypatch.setenv("BGOS_AGENTS_JSON", "{not json")
    monkeypatch.setenv("BGOS_AGENTS", "default:David")
    assert enumerate_agents_from_env() == [{"agent_route": "default", "name": "David"}]


def test_enumerate_empty_when_nothing_set(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.delenv("BGOS_AGENTS", raising=False)
    assert enumerate_agents_from_env() == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_channel_bgos.agents'`.

- [ ] **Step 3: Write the implementation**

```python
# src/hermes_channel_bgos/agents.py
"""Agent-catalog spec parsing — shared by the pair CLI (`--agents`), the
adapter's catalog push (`BGOS_AGENTS` / `BGOS_AGENTS_JSON`), and the doctor.

Single source of truth for the `route:Display Name` comma format so the CLI,
adapter, and diagnostics never disagree on what a spec string means.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def parse_agents_spec(raw: str) -> list[dict]:
    """Parse a comma-separated `route:Display Name` spec into catalog entries.

    - `"hades:Hades,ramy:Ramy"` → two entries.
    - A bare route with no colon uses the route as both route and name.
    - Whitespace around pieces, routes, and names is stripped.
    - Empty pieces (e.g. a trailing comma) are skipped.
    - An empty/blank string yields `[]`.

    Returns entries shaped `{"agent_route": str, "name": str}` — the shape the
    backend's agent-catalog endpoint expects.
    """
    out: list[dict] = []
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            route, name = piece.split(":", 1)
            route = route.strip()
            name = name.strip() or route
        else:
            route = piece
            name = piece
        if route:
            out.append({"agent_route": route, "name": name})
    return out


def enumerate_agents_from_env() -> list[dict]:
    """Discover configured agents from env. First non-empty source wins:

    1. `BGOS_AGENTS_JSON` — JSON list of `{"agent_route", "name", ...}` dicts.
    2. `BGOS_AGENTS` — comma-separated `route:Display Name` (see parse_agents_spec).

    Returns `[]` when neither is set.
    """
    raw_json = os.environ.get("BGOS_AGENTS_JSON", "").strip()
    if raw_json:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            log.warning("BGOS_AGENTS_JSON is not valid JSON — ignoring")
        else:
            if isinstance(data, list):
                out = [e for e in data if isinstance(e, dict) and e.get("agent_route")]
                if out:
                    return out
    return parse_agents_spec(os.environ.get("BGOS_AGENTS", ""))
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_agents.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/agents.py tests/test_agents.py
git commit -m "feat: shared agent-spec parser (agents.py)"
```

---

## Task 2: Delegate `_enumerate_agents` to the shared parser

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (`_enumerate_agents`, ~lines 797–847)

No behavior change — this is a DRY refactor. Existing adapter tests are the regression guard.

- [ ] **Step 1: Add the import**

At the top of `bgos_adapter.py`, after `from .config import BgosConfig` (line 34), add:

```python
from .agents import enumerate_agents_from_env
```

- [ ] **Step 2: Replace the method body**

Replace the entire `_enumerate_agents` method (from `def _enumerate_agents(self) -> list[dict]:` through its closing `return []`) with:

```python
    def _enumerate_agents(self) -> list[dict]:
        """Discover Hermes's configured agents for the agent-catalog push.

        Delegates to `agents.enumerate_agents_from_env`, the single source of
        truth for the `BGOS_AGENTS_JSON` / `BGOS_AGENTS` precedence and the
        `route:Display Name` spec format (also used by the pair CLI's
        `--agents` flag and the doctor). Returns `[]` when nothing is
        configured — callers treat that as warn-but-continue, not an error.
        """
        return enumerate_agents_from_env()
```

- [ ] **Step 3: Run the adapter test suites to verify no regression**

Run: `.venv/bin/python -m pytest tests/test_bgos_adapter.py tests/test_bgos_adapter_inbound.py -q`
Expected: PASS (all existing tests green).

- [ ] **Step 4: Commit**

```bash
git add src/hermes_channel_bgos/bgos_adapter.py
git commit -m "refactor: _enumerate_agents delegates to shared parser"
```

---

## Task 3: Hot-refresh on unknown `assistant_id`

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (imports; `__init__` ~line 604; new method after `connect()`; `_handle_inbound` drop branch ~lines 1848–1853)
- Test: `tests/test_bgos_adapter_inbound.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bgos_adapter_inbound.py`:

```python
# -----------------------------------------------------------------------------
# Hot-refresh on unknown assistant_id (v0.8.0). When a message arrives for an
# assistant the adapter doesn't know — almost always because the user exposed a
# new agent in BGOS *after* the gateway started — the adapter re-fetches whoami,
# rebinds, and retries the lookup once instead of dropping until restart.
# -----------------------------------------------------------------------------


class _FakeWs:
    def __init__(self):
        self.bound: list[int] | None = None
        self.unbound: list[int] = []

    def bind_assistants(self, ids):
        self.bound = list(ids)

    def unbind_assistant(self, aid):
        self.unbound.append(aid)


async def test_hot_refresh_recovers_unknown_assistant(monkeypatch):
    adapter = _make_adapter()
    adapter._state.set_route(7, "hades")
    fake_ws = _FakeWs()
    adapter._ws = fake_ws
    adapter._text_batch_window = 0.05

    async def fake_whoami():
        return {
            "pairing_id": 1, "user_id": "owner",
            "assistants": [
                {"assistant_id": 7, "agent_route": "hades"},
                {"assistant_id": 892, "agent_route": "default"},
            ],
        }
    monkeypatch.setattr(adapter._api, "whoami", fake_whoami)

    handled: list[Any] = []

    async def capture(event):
        handled.append(event)
    adapter.handle_message = capture

    await adapter._handle_inbound({
        "assistant_id": 892, "chat_id": 5, "message_id": 1000,
        "user_id": "owner", "text": "hello", "files": [],
        "message_type": "standard",
    })
    await asyncio.sleep(0.15)

    assert adapter._state.get_route(892) == "default"
    assert fake_ws.bound is not None and 892 in fake_ws.bound
    assert len(handled) == 1
    assert handled[0].text == "hello"
    await adapter._api.close()


async def test_hot_refresh_still_unknown_is_dropped(monkeypatch):
    adapter = _make_adapter()
    adapter._state.set_route(7, "hades")
    adapter._ws = _FakeWs()

    calls: list[int] = []

    async def fake_whoami():
        calls.append(1)
        return {"pairing_id": 1, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]}
    monkeypatch.setattr(adapter._api, "whoami", fake_whoami)

    handled: list[Any] = []

    async def capture(event):
        handled.append(event)
    adapter.handle_message = capture

    await adapter._handle_inbound({
        "assistant_id": 999, "chat_id": 5, "message_id": 1,
        "user_id": "u", "text": "x", "files": [],
        "message_type": "standard",
    })
    assert handled == []
    assert len(calls) == 1  # refresh attempted exactly once
    await adapter._api.close()


async def test_hot_refresh_cooldown_limits_whoami(monkeypatch):
    adapter = _make_adapter()
    adapter._state.set_route(7, "hades")
    adapter._ws = _FakeWs()
    adapter._scope_refresh_cooldown = 60.0  # long → 2nd refresh gated

    calls: list[int] = []

    async def fake_whoami():
        calls.append(1)
        return {"pairing_id": 1, "assistants": [{"assistant_id": 7, "agent_route": "hades"}]}
    monkeypatch.setattr(adapter._api, "whoami", fake_whoami)

    async def noop(event):
        pass
    adapter.handle_message = noop

    for mid in (1, 2):
        await adapter._handle_inbound({
            "assistant_id": 999, "chat_id": 5, "message_id": mid,
            "user_id": "u", "text": "x", "files": [],
            "message_type": "standard",
        })
    assert len(calls) == 1  # second inbound's refresh blocked by cooldown
    await adapter._api.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bgos_adapter_inbound.py -q -k hot_refresh`
Expected: FAIL — `AttributeError: 'BGOSAdapter' object has no attribute '_scope_refresh_cooldown'` and the recovery test asserting route 892 unset.

- [ ] **Step 3a: Add `import time`**

In `bgos_adapter.py` imports, after `import re` (line 20) add:

```python
import time
```

- [ ] **Step 3b: Add the `__init__` fields**

After the `self._max_message_length: int = _DEFAULT_MAX_MESSAGE_LENGTH` line (end of `__init__`, ~line 604), add:

```python
        # Pairing-scope hot-refresh (v0.8.0). When inbound arrives for an
        # assistant_id we don't recognize — almost always because the user
        # exposed a new agent in the BGOS Integrations UI *after* the gateway
        # started — we re-fetch whoami and reconcile the route map in place
        # instead of dropping the message until the next restart.
        # _scope_refresh_lock serializes concurrent refreshes (live WS + the
        # REST poll loop can both hit an unknown id at once);
        # _last_scope_refresh + _scope_refresh_cooldown rate-limit whoami so a
        # genuinely-unknown id can't trigger a fetch on every poll tick.
        self._scope_refresh_lock = asyncio.Lock()
        self._last_scope_refresh: float = 0.0
        self._scope_refresh_cooldown: float = float(
            os.environ.get("BGOS_SCOPE_REFRESH_COOLDOWN", "10")
        )
```

- [ ] **Step 3c: Add the `_refresh_pairing_scope` method**

Immediately after the `connect()` method's `return True` (before the `# ----- Last-seen message-id persistence` comment block, ~line 760), insert:

```python
    async def _refresh_pairing_scope(self) -> bool:
        """Re-fetch the pairing scope from `GET /api/v1/integrations/me` and
        reconcile the in-process assistant→route map (and WS room bindings)
        with what BGOS now reports.

        Called from `_handle_inbound` when a message arrives for an
        assistant_id we don't recognize — the common case being the user
        exposing a new agent in the BGOS Integrations UI *after* the gateway
        started. Lets new assistants hot-load without a gateway restart.

        Rate-limited: serialized by `_scope_refresh_lock` and gated by
        `_scope_refresh_cooldown` (env `BGOS_SCOPE_REFRESH_COOLDOWN`, default
        10s) so a flood of inbound for a genuinely-unknown id can't hammer
        whoami. Fail-open — any error is logged and the route map left as-is.
        Mirrors `connect()` (route map + WS bind + pairing_user_id); it does
        NOT sync per-assistant command manifests, since connect() doesn't.

        Returns True if the route map changed.
        """
        async with self._scope_refresh_lock:
            now = time.monotonic()
            if self._last_scope_refresh and (
                now - self._last_scope_refresh < self._scope_refresh_cooldown
            ):
                return False
            self._last_scope_refresh = now

            try:
                me = await self._api.whoami()
            except Exception:
                log.exception("pairing-scope refresh: whoami failed")
                return False

            if self.pairing_user_id is None:
                self.pairing_user_id = me.get("user_id") or me.get("userId")

            new_routes: dict[int, str] = {}
            for entry in me.get("assistants", []):
                aid = entry.get("assistant_id", entry.get("id"))
                route = entry.get("agent_route")
                if aid is None or route is None:
                    continue
                new_routes[aid] = route

            old_ids = set(self._state.assistant_route.keys())
            new_ids = set(new_routes.keys())
            added = sorted(new_ids - old_ids)
            removed = sorted(old_ids - new_ids)
            if not added and not removed:
                return False

            for aid in added:
                self._state.set_route(aid, new_routes[aid])
            for aid in removed:
                self._state.remove_assistant(aid)
                if self._ws is not None:
                    self._ws.unbind_assistant(aid)
            if self._ws is not None and added:
                self._ws.bind_assistants(list(new_routes.keys()))

            log.info(
                "bgos scope refreshed: added=%s removed=%s bound=%s",
                added, removed, sorted(new_ids),
            )
            return True
```

- [ ] **Step 3d: Rewire the `_handle_inbound` drop branch**

Replace the existing branch (~lines 1848–1853):

```python
        route = self._state.get_route(assistant_id)
        if route is None:
            log.warning(
                "inbound for unknown assistant_id=%s — dropping", assistant_id,
            )
            return
```

with:

```python
        route = self._state.get_route(assistant_id)
        if route is None:
            # Unknown assistant — almost always the user just exposed a new
            # agent in BGOS after the gateway started. Re-sync the pairing
            # scope from whoami and retry the lookup once before giving up, so
            # the message self-heals instead of being dropped until a manual
            # restart. _refresh_pairing_scope is rate-limited internally.
            log.info(
                "inbound for unknown assistant_id=%s — refreshing pairing scope",
                assistant_id,
            )
            await self._refresh_pairing_scope()
            route = self._state.get_route(assistant_id)
            if route is None:
                log.warning(
                    "assistant_id=%s still unknown after refresh — dropping",
                    assistant_id,
                )
                return
```

- [ ] **Step 4: Run to verify the new tests pass and old ones stay green**

Run: `.venv/bin/python -m pytest tests/test_bgos_adapter_inbound.py -q`
Expected: PASS (existing + 3 new). `test_inbound_for_unknown_assistant_is_dropped` still passes — the mock `whoami` returns only assistant 7, so 999 stays unknown and is dropped.

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/bgos_adapter.py tests/test_bgos_adapter_inbound.py
git commit -m "feat: hot-refresh pairing scope on unknown assistant_id"
```

---

## Task 4: Clearer startup logs

**Files:**
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (`connect()` after catalog push ~line 723; `_push_agent_catalog_safe` success log ~line 793)

No new test — these are operator-facing log strings (no test asserts on them, confirmed by grep). Regression guard is the existing adapter suite.

- [ ] **Step 1: Add a bound-assistants log in `connect()`**

After `await self._push_agent_catalog_safe()` (line 723) and before the `# Replay any messages...` comment (line 725), insert:

```python
        # Surface bound-assistant state explicitly so operators see at a glance
        # whether any agents are exposed yet. Zero is the common fresh-install
        # state (catalog pushed, user hasn't ticked agents in the UI) — and
        # with hot-refresh that resolves on its own, no restart required.
        bound = sorted(self._state.assistant_route.items())
        if bound:
            log.info(
                "BGOS bound assistants: %s",
                ", ".join(f"{aid}:{route}" for aid, route in bound),
            )
        else:
            log.warning(
                "BGOS: 0 assistants exposed yet — open BGOS Integrations → "
                "Hermes → tick agent(s) → Save. New exposures hot-load "
                "automatically (no gateway restart needed)."
            )
```

- [ ] **Step 2: Make the catalog-push log list the routes**

Replace `log.info("pushed agent catalog: %d entries", len(agents))` (line 793) with:

```python
            log.info(
                "BGOS catalog pushed: %s",
                ", ".join(
                    f"{a['agent_route']}:{a.get('name', a['agent_route'])}"
                    for a in agents
                ),
            )
```

- [ ] **Step 3: Run the adapter suites**

Run: `.venv/bin/python -m pytest tests/test_bgos_adapter.py tests/test_bgos_adapter_inbound.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/hermes_channel_bgos/bgos_adapter.py
git commit -m "feat: clearer startup logs (catalog routes + bound assistants)"
```

---

## Task 5: Pair CLI `--agents` (push catalog at pair time)

**Files:**
- Modify: `src/hermes_channel_bgos/pair_cli.py`
- Test: `tests/test_pair_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pair_cli.py`:

```python
async def test_pair_cli_agents_pushes_catalog(mock_bgos_server, tmp_secrets_dir):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "tok", "pairing_id": 55},
    )
    mock_bgos_server.on(
        "POST", "/api/v1/integrations/pairings/55/agent-catalog",
    ).respond(200, {})

    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "host",
        "--base-url", mock_bgos_server.url,
        "--agents", "default:David,hades:Hades",
    ])
    assert result.exit_code == 0, result.output

    pe = mock_bgos_server.last_request("POST", "/api/v1/integrations/pair-exchange")
    assert pe.json_body["agentCatalog"] == [
        {"agent_route": "default", "name": "David"},
        {"agent_route": "hades", "name": "Hades"},
    ]

    cat = mock_bgos_server.last_request(
        "POST", "/api/v1/integrations/pairings/55/agent-catalog",
    )
    assert cat.headers["X-BGOS-Pairing"] == "tok"
    assert cat.json_body == {"agents": [
        {"agent_route": "default", "name": "David"},
        {"agent_route": "hades", "name": "Hades"},
    ]}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pair_cli.py::test_pair_cli_agents_pushes_catalog -q`
Expected: FAIL — `--agents` is an unknown option (Click usage error, exit 2).

- [ ] **Step 3: Implement `--agents`**

In `pair_cli.py`, add the import after `from .config import BgosConfig` (line 21):

```python
from .agents import parse_agents_spec
```

Add the option to `main` (after the `--integration` option, before `def main(...)`):

```python
@click.option(
    "--agents", default="", show_default=False,
    help="Comma-separated route:Name agents to publish to BGOS at pair time, "
         "e.g. 'default:David' or 'hades:Hades,ramy:Ramy'. Lets you tick "
         "agents in the Integrations UI before the gateway even starts.",
)
```

Change the `main` signature and its `asyncio.run` call:

```python
def main(
    code: str, device_label: str, base_url: str, integration: str, agents: str,
) -> None:
    """Exchange a BGOS pairing code for a pairing token.

    The token is written to ~/.hermes/secrets/bgos.json (mode 0600 on POSIX).
    Re-run to re-pair — the old file is overwritten.
    """
    asyncio.run(_run(code, device_label, base_url, integration, agents))
```

Replace the `_run` function with (note the new `agents` param, the `agent_catalog` on pair-exchange, and the post-pairing authenticated push):

```python
async def _run(
    code: str, device_label: str, base_url: str, integration: str, agents: str,
) -> None:
    catalog = parse_agents_spec(agents)
    api = BgosApi(BgosConfig(base_url=base_url, pairing_token=None))
    try:
        resp = await api.pair_exchange(
            code=code, device_label=device_label, integration=integration,
            agent_catalog=catalog,
        )
    except BgosApiError as exc:
        code_str = f" {exc.code}" if exc.code else ""
        click.secho(
            f"Pair exchange failed: HTTP {exc.status}{code_str}",
            fg="red", err=True,
        )
        sys.exit(1)
    finally:
        await api.close()

    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "pairing_token": resp["pairing_token"],
        "pairing_id": resp["pairing_id"],
        "base_url": base_url,
    }
    path.write_text(json.dumps(data, indent=2))
    if os.name == "posix":
        os.chmod(path, 0o600)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

    click.secho(f"Paired. Secret written to {path}", fg="green")

    # Publish the catalog with the authenticated token too, so it lands even if
    # the pre-auth pair-exchange didn't persist agentCatalog. This makes the
    # agents tickable in the Integrations UI before the gateway ever starts.
    if catalog:
        auth_api = BgosApi(
            BgosConfig(base_url=base_url, pairing_token=resp["pairing_token"]),
        )
        try:
            await auth_api.push_agent_catalog(
                pairing_id=resp["pairing_id"], entries=catalog,
            )
            click.secho(f"Published {len(catalog)} agent(s) to BGOS.", fg="green")
        except BgosApiError as exc:
            click.secho(
                f"Catalog push failed (non-fatal): HTTP {exc.status}",
                fg="yellow", err=True,
            )
        finally:
            await auth_api.close()
```

- [ ] **Step 4: Run to verify pass + no regression**

Run: `.venv/bin/python -m pytest tests/test_pair_cli.py -q`
Expected: PASS (existing + new). Existing `test_pair_cli_sends_correct_payload` still passes (`agentCatalog: []` when `--agents` omitted).

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/pair_cli.py tests/test_pair_cli.py
git commit -m "feat(pair-cli): --agents publishes catalog at pair time"
```

---

## Task 6: Pair CLI `--wait-for-exposure`

**Files:**
- Modify: `src/hermes_channel_bgos/pair_cli.py`
- Test: `tests/test_pair_cli.py`

Decision: on timeout the CLI prints guidance and exits **0** — pairing already succeeded and exposures hot-load, so a timeout is not a hard failure.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pair_cli.py`:

```python
async def test_wait_for_exposure_returns_when_assistants_appear():
    from hermes_channel_bgos.pair_cli import wait_for_exposure

    seq = [
        {"assistants": []},
        {"assistants": []},
        {"assistants": [{"assistant_id": 892, "agent_route": "default", "name": "David"}]},
    ]

    class FakeApi:
        def __init__(self):
            self.i = 0

        async def whoami(self):
            r = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return r

    api = FakeApi()
    result = await wait_for_exposure(api, interval=0.01, timeout=5.0)
    assert result == [{"assistant_id": 892, "agent_route": "default", "name": "David"}]
    assert api.i >= 3


async def test_wait_for_exposure_times_out_empty():
    from hermes_channel_bgos.pair_cli import wait_for_exposure

    class FakeApi:
        async def whoami(self):
            return {"assistants": []}

    result = await wait_for_exposure(FakeApi(), interval=0.01, timeout=0.05)
    assert result == []


async def test_pair_cli_wait_for_exposure_timeout(mock_bgos_server, tmp_secrets_dir):
    mock_bgos_server.on("POST", "/api/v1/integrations/pair-exchange").respond(
        200, {"pairing_token": "tok", "pairing_id": 7},
    )
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": []},
    )

    result = await _invoke_cli([
        "BGOS-CODE", "--device-label", "h",
        "--base-url", mock_bgos_server.url,
        "--wait-for-exposure", "--wait-timeout", "0.1", "--wait-interval", "0.02",
    ])
    assert result.exit_code == 0, result.output
    assert "expos" in result.output.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pair_cli.py -q -k wait_for_exposure`
Expected: FAIL — `wait_for_exposure` undefined / `--wait-for-exposure` unknown option.

- [ ] **Step 3a: Add `import time` to `pair_cli.py`**

After `import sys` (line 15) add:

```python
import time
```

- [ ] **Step 3b: Add the `wait_for_exposure` helper**

Add this module-level function after `secrets_path()` (after line 31):

```python
async def wait_for_exposure(
    api: BgosApi, *, interval: float, timeout: float,
    echo=lambda msg: None,
) -> list[dict]:
    """Poll `whoami` until at least one assistant is exposed, or `timeout`.

    Returns the exposed assistants list (empty on timeout). `echo` is called
    with progress strings — the CLI passes a `click.secho` wrapper; tests pass
    the default no-op.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            me = await api.whoami()
            assistants = me.get("assistants") or []
        except BgosApiError as exc:
            echo(f"whoami failed (HTTP {exc.status}); retrying…")
            assistants = []
        if assistants:
            return assistants
        if time.monotonic() >= deadline:
            return []
        echo(
            "Waiting for you to expose an agent in BGOS… "
            "Open Integrations → Hermes → tick agent(s) → Save"
        )
        await asyncio.sleep(interval)
```

- [ ] **Step 3c: Add the CLI options + signature**

Add three options to `main` (after the `--agents` option):

```python
@click.option(
    "--wait-for-exposure", "wait_for_exposure_flag", is_flag=True,
    help="After pairing, poll until you expose an agent in the BGOS UI.",
)
@click.option(
    "--wait-timeout", default=180.0, type=float, show_default=True,
    help="Seconds to wait for exposure before giving up (pairing still stands).",
)
@click.option(
    "--wait-interval", default=4.0, type=float, show_default=True,
    help="Seconds between exposure polls.",
)
```

Update `main` signature + call:

```python
def main(
    code: str, device_label: str, base_url: str, integration: str, agents: str,
    wait_for_exposure_flag: bool, wait_timeout: float, wait_interval: float,
) -> None:
    """Exchange a BGOS pairing code for a pairing token.

    The token is written to ~/.hermes/secrets/bgos.json (mode 0600 on POSIX).
    Re-run to re-pair — the old file is overwritten.
    """
    asyncio.run(_run(
        code, device_label, base_url, integration, agents,
        wait_for_exposure_flag, wait_timeout, wait_interval,
    ))
```

- [ ] **Step 3d: Thread the params through `_run` and add the wait block**

Change the `_run` signature to:

```python
async def _run(
    code: str, device_label: str, base_url: str, integration: str, agents: str,
    wait_for_exposure_flag: bool = False, wait_timeout: float = 180.0,
    wait_interval: float = 4.0,
) -> None:
```

At the very end of `_run` (after the `if catalog:` push block), append:

```python
    if wait_for_exposure_flag:
        auth_api = BgosApi(
            BgosConfig(base_url=base_url, pairing_token=resp["pairing_token"]),
        )
        try:
            assistants = await wait_for_exposure(
                auth_api, interval=wait_interval, timeout=wait_timeout,
                echo=lambda m: click.secho(m, fg="cyan"),
            )
        finally:
            await auth_api.close()
        if assistants:
            click.secho("Exposed assistants:", fg="green")
            for a in assistants:
                aid = a.get("assistant_id", a.get("id"))
                click.secho(
                    f"  - assistant_id={aid} route={a.get('agent_route')} "
                    f"name={a.get('name', '')}",
                    fg="green",
                )
        else:
            click.secho(
                "No agents exposed yet (timed out). Pairing still stands — "
                "expose agents anytime in BGOS → Integrations → Hermes; the "
                "running gateway hot-loads them (no restart needed).",
                fg="yellow",
            )
```

- [ ] **Step 4: Run to verify pass + full pair suite**

Run: `.venv/bin/python -m pytest tests/test_pair_cli.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/pair_cli.py tests/test_pair_cli.py
git commit -m "feat(pair-cli): --wait-for-exposure polls until agents are ticked"
```

---

## Task 7: `hermes-bgos-doctor`

**Files:**
- Create: `src/hermes_channel_bgos/doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_doctor.py
"""Tests for hermes-bgos-doctor."""
from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from hermes_channel_bgos.config import BgosConfig
from hermes_channel_bgos.doctor import (
    FAIL, OK, WARN, CheckResult, check_catalog, check_config, check_env,
    check_package, check_whoami, main as doctor_main, render_json,
)


def test_check_package_reports_version():
    r = check_package()
    assert r.status == OK
    assert "hermes_channel_bgos" in r.detail


def test_check_catalog_warns_when_unset(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS", raising=False)
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    r = check_catalog()
    assert r.status == WARN
    assert r.fix


def test_check_catalog_ok(monkeypatch):
    monkeypatch.delenv("BGOS_AGENTS_JSON", raising=False)
    monkeypatch.setenv("BGOS_AGENTS", "default:David")
    r = check_catalog()
    assert r.status == OK
    assert "default:David" in r.detail


def test_check_env_warns_without_auth(monkeypatch):
    monkeypatch.delenv("BGOS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("BGOS_ALLOWED_USERS", raising=False)
    assert check_env().status == WARN


def test_check_env_ok_with_allow_all(monkeypatch):
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    assert check_env().status == OK


def test_check_config_not_paired(monkeypatch):
    # autouse fixture points HERMES_HOME at an empty tmp dir → no secrets file
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    cfg, r = check_config()
    assert cfg is None
    assert r.status == FAIL
    assert "hermes-pair-bgos" in r.fix


async def test_check_whoami_reports_exposed_assistants(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": [
            {"assistant_id": 892, "agent_route": "default", "name": "David"}]},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok"))
    assert r.status == OK
    assert "892" in r.detail


async def test_check_whoami_401_is_fail(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        401, {"error": "PAIRING_REVOKED"},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="bad"))
    assert r.status == FAIL
    assert "re-pair" in r.fix.lower()


async def test_check_whoami_warns_when_no_assistants(mock_bgos_server):
    mock_bgos_server.on("GET", "/api/v1/integrations/me").respond(
        200, {"pairing_id": 7, "assistants": []},
    )
    r = await check_whoami(BgosConfig(base_url=mock_bgos_server.url, pairing_token="tok"))
    assert r.status == WARN


def test_render_json_marks_fail():
    data = json.loads(render_json([
        CheckResult("a", OK, "x"),
        CheckResult("b", FAIL, "y", "fixit"),
    ]))
    assert data["result"] == "fail"
    assert data["checks"][1]["fix"] == "fixit"


def test_render_json_ok_when_no_fail():
    data = json.loads(render_json([CheckResult("a", OK, "x"), CheckResult("b", WARN, "y")]))
    assert data["result"] == "ok"


async def test_doctor_main_exits_1_when_unconfigured(monkeypatch):
    # No Hermes gateway in the test env → fork_patch FAILs → exit 1.
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    runner = CliRunner()
    result = await asyncio.to_thread(runner.invoke, doctor_main, ["--offline"])
    assert result.exit_code == 1
    assert "fork_patch" in result.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_doctor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_channel_bgos.doctor'`.

- [ ] **Step 3: Implement `doctor.py`**

```python
# src/hermes_channel_bgos/doctor.py
"""hermes-bgos-doctor — non-interactive diagnostic for the BGOS channel.

Run on the Hermes server (from Hermes's Python env) to verify the install is
wired correctly and the pairing is live. Prints one line per check with an
inline fix; `--json` emits machine-readable output for an automating agent.
Exit code 1 if any check FAILs (WARN does not fail), else 0.

    hermes-bgos-doctor
    python -m hermes_channel_bgos.doctor --json
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass

import click

from . import __version__
from .agents import enumerate_agents_from_env
from .bgos_api import BgosApi, BgosApiError
from .config import BgosConfig

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    fix: str = ""


def check_package() -> CheckResult:
    return CheckResult("package", OK, f"hermes_channel_bgos {__version__}")


def check_fork_patch() -> CheckResult:
    try:
        from gateway.config import Platform  # type: ignore
        from gateway.platforms.bgos import BGOSAdapter  # type: ignore
    except Exception as exc:
        return CheckResult(
            "fork_patch", FAIL,
            f"Hermes gateway not importable ({exc.__class__.__name__})",
            fix="Run this from Hermes's Python env, and apply the fork patch: "
                "git am hermes-fork-patch/0001-bgos-integration.patch",
        )
    if getattr(Platform, "BGOS", None) is None:
        return CheckResult(
            "fork_patch", FAIL, "Platform.BGOS missing",
            fix="Re-apply the fork patch (it registers Platform.BGOS).",
        )
    return CheckResult(
        "fork_patch", OK, f"Platform.BGOS + {BGOSAdapter.__name__} importable",
    )


def check_config() -> tuple[BgosConfig | None, CheckResult]:
    """Resolve the pairing config the same way the adapter does. Returns the
    config (or None) plus a CheckResult. A missing token is the canonical
    'not paired' state and yields the get-a-code instruction."""
    from .bgos_adapter import BGOSAdapter
    from .pair_cli import secrets_path
    sp = secrets_path()
    try:
        cfg = BGOSAdapter._resolve_config(None)
    except RuntimeError:
        return None, CheckResult(
            "config", FAIL,
            f"no pairing token found (checked $BGOS_API_KEY and {sp})",
            fix="Not paired. In BGOS open Integrations → Hermes → 'Connect a "
                "new Hermes server', copy the BGOS-XXXX-XX code, then run: "
                "hermes-pair-bgos <CODE> --device-label <host>",
        )
    return cfg, CheckResult(
        "config", OK,
        f"token resolved; base_url={cfg.base_url}; secrets={sp} (exists={sp.exists()})",
    )


def check_env() -> CheckResult:
    if os.environ.get("BGOS_ALLOW_ALL_USERS", "").lower() == "true":
        return CheckResult("auth", OK, "BGOS_ALLOW_ALL_USERS=true")
    allowed = os.environ.get("BGOS_ALLOWED_USERS", "").strip()
    if allowed:
        n = len([u for u in allowed.split(",") if u.strip()])
        return CheckResult("auth", OK, f"BGOS_ALLOWED_USERS set ({n} user(s))")
    return CheckResult(
        "auth", WARN,
        "neither BGOS_ALLOW_ALL_USERS nor BGOS_ALLOWED_USERS set",
        fix="Set BGOS_ALLOW_ALL_USERS=true (or BGOS_ALLOWED_USERS=<clerk id>) "
            "or inbound messages are dropped by the auth gate.",
    )


def check_catalog() -> CheckResult:
    agents = enumerate_agents_from_env()
    if not agents:
        return CheckResult(
            "catalog", WARN, "no agents configured (BGOS_AGENTS unset)",
            fix="Set BGOS_AGENTS=route:Name (e.g. default:David) so the "
                "Integrations UI can offer agents to expose.",
        )
    listing = ", ".join(
        f"{a['agent_route']}:{a.get('name', a['agent_route'])}" for a in agents
    )
    return CheckResult("catalog", OK, f"{len(agents)} configured: {listing}")


async def check_whoami(cfg: BgosConfig) -> CheckResult:
    api = BgosApi(cfg)
    try:
        me = await api.whoami()
    except BgosApiError as exc:
        if exc.status == 401:
            return CheckResult(
                "pairing_live", FAIL, f"whoami 401 ({exc.code or 'unauthorized'})",
                fix="Pairing revoked/expired. Delete the secrets file and re-pair.",
            )
        return CheckResult(
            "pairing_live", FAIL, f"whoami HTTP {exc.status}",
            fix="Check BGOS_BACKEND_URL and connectivity.",
        )
    except Exception as exc:
        return CheckResult(
            "pairing_live", FAIL, f"whoami failed: {exc.__class__.__name__}",
            fix="Check network / BGOS_BACKEND_URL.",
        )
    finally:
        await api.close()

    assistants = me.get("assistants") or []
    pid = me.get("pairing_id")
    if not assistants:
        return CheckResult(
            "pairing_live", WARN,
            f"paired (pairing_id={pid}) but 0 assistants exposed",
            fix="Open BGOS → Integrations → Hermes → tick agent(s) → Save. The "
                "running gateway hot-loads new exposures (no restart needed).",
        )
    listing = ", ".join(
        f"{a.get('assistant_id', a.get('id'))}:{a.get('agent_route')}"
        f"({a.get('name', '')})"
        for a in assistants
    )
    return CheckResult("pairing_live", OK, f"pairing_id={pid}; exposed: {listing}")


def check_gateway_process() -> CheckResult:
    """Best-effort, informational only — never FAILs (the doctor often runs in
    a different process/user than the gateway)."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return CheckResult("gateway_process", WARN, "could not inspect processes")
    running = any(
        "hermes" in ln.lower() and "gateway" in ln.lower()
        for ln in out.splitlines()
    )
    if running:
        return CheckResult(
            "gateway_process", OK, "a hermes gateway process appears to be running",
        )
    return CheckResult(
        "gateway_process", WARN, "no hermes gateway process detected",
        fix="Start Hermes (e.g. systemctl --user start hermes-gateway.service).",
    )


async def run_checks(*, offline: bool = False) -> list[CheckResult]:
    results = [check_package(), check_fork_patch()]
    cfg, cfg_result = check_config()
    results.append(cfg_result)
    results.append(check_env())
    results.append(check_catalog())
    if cfg is not None and not offline:
        results.append(await check_whoami(cfg))
    results.append(check_gateway_process())
    return results


def render_text(results: list[CheckResult]) -> str:
    icons = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = ["BGOS doctor", ""]
    for r in results:
        lines.append(f"  [{icons.get(r.status, '?')}] {r.name}: {r.detail}")
        if r.fix and r.status != OK:
            lines.append(f"        fix: {r.fix}")
    fails = [r for r in results if r.status == FAIL]
    lines += ["", "RESULT: " + ("FAIL" if fails else "OK")]
    return "\n".join(lines)


def render_json(results: list[CheckResult]) -> str:
    fails = [r for r in results if r.status == FAIL]
    return _json.dumps(
        {"result": "fail" if fails else "ok",
         "checks": [asdict(r) for r in results]},
        indent=2,
    )


@click.command("hermes-bgos-doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--offline", is_flag=True, help="Skip the live whoami network check.")
def main(as_json: bool, offline: bool) -> None:
    results = asyncio.run(run_checks(offline=offline))
    click.echo(render_json(results) if as_json else render_text(results))
    if any(r.status == FAIL for r in results):
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_doctor.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/doctor.py tests/test_doctor.py
git commit -m "feat: hermes-bgos-doctor diagnostic CLI"
```

---

## Task 8: Version bump + console script

**Files:**
- Modify: `pyproject.toml`, `src/hermes_channel_bgos/__init__.py`

- [ ] **Step 1: Bump pyproject version + add the doctor script**

In `pyproject.toml`, change `version = "0.7.0"` to `version = "0.8.0"`.

Change the `[project.scripts]` block to:

```toml
[project.scripts]
"hermes-pair-bgos" = "hermes_channel_bgos.pair_cli:main"
"hermes-bgos-doctor" = "hermes_channel_bgos.doctor:main"
```

- [ ] **Step 2: Bump `__init__` version**

In `src/hermes_channel_bgos/__init__.py`, change `__version__ = "0.7.0"` to `__version__ = "0.8.0"`.

- [ ] **Step 3: Reinstall so the new console script lands + verify**

Run:
```bash
.venv/bin/pip install -e . -q
.venv/bin/python -c "import hermes_channel_bgos; print(hermes_channel_bgos.__version__)"
.venv/bin/hermes-bgos-doctor --offline --json | head -5 || true
```
Expected: prints `0.8.0`; the doctor emits JSON (with a `fork_patch` FAIL in this non-Hermes env — that's fine).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/hermes_channel_bgos/__init__.py
git commit -m "chore: bump to 0.8.0 + register hermes-bgos-doctor script"
```

---

## Task 9: Docs — README + integration skill

**Files:**
- Modify: `README.md`
- Modify: `/Users/kc/.claude/skills/bgos-integrate-hermes-agent/SKILL.md` AND `/Users/kc/Projects/BGOS/.claude/skills/bgos-integrate-hermes-agent/SKILL.md` (keep both copies in sync)

No tests. Verify by re-reading.

- [ ] **Step 1: README — pairing step gains `--agents` + doctor + hot-reload**

In the "Quick start with Claude Code" prompt, step 5 (pairing), change the pair command to include `--agents` and add a doctor verification line. In step 9 ("Verify it's alive"), add running `hermes-bgos-doctor`. Replace the post-exposure restart guidance: agents exposed after start now hot-load (no restart). Add to the troubleshooting bullet list:

```
> - Messages arrive in BGOS but the agent never replies, log shows `inbound for unknown assistant_id=<id>` → you exposed the agent after the gateway started. On ≥0.8.0 this self-heals within a few seconds (hot-refresh); if it persists, run `hermes-bgos-doctor` and check the `pairing_live` line.
```

- [ ] **Step 2: README — Configuration table + First-time pairing**

Add a `BGOS_SCOPE_REFRESH_COOLDOWN` row to the env-var table:

```
| `BGOS_SCOPE_REFRESH_COOLDOWN` | `10` | No | Seconds between hot-refresh `whoami` calls when inbound arrives for an unknown assistant_id. Lower = faster recovery after exposing an agent; higher = less load. |
```

In "First-time pairing", replace step 3's restart implication with a note that exposures hot-load, and add the `--agents` / `--wait-for-exposure` variants of the pair command plus a `hermes-bgos-doctor` verification step. Add a `HERMES_HOME`/profile note near Configuration: for a named-profile Hermes install, point `HERMES_HOME` at the profile dir (e.g. `~/.hermes/profiles/david`) so the secrets file, `bgos_last_id`, and the systemd `EnvironmentFile=` all resolve under the same root.

- [ ] **Step 3: README — Troubleshooting table**

Under "Runtime — message flow", add:

```
**BGOS shows the server connected and the agent selected, but messages get no reply; log shows `inbound for unknown assistant_id=<id>`.** The agent was exposed after the gateway started, so the running adapter hadn't cached that assistant id. On **≥0.8.0** the adapter hot-refreshes the pairing scope on the first such message and self-heals within ~one poll cycle — no restart needed. On older versions, restart the gateway once. Confirm with `hermes-bgos-doctor` (the `pairing_live` line lists exposed assistants).
```

- [ ] **Step 4: Skill — propagate the same changes**

In BOTH `SKILL.md` copies: update step 7 (pair) to use `--agents "<routes>"`; add a step after pairing to run `hermes-bgos-doctor` and read its output; in the troubleshooting table change the "WS push not delivering" / restart rows to note hot-refresh removes the post-exposure restart on ≥0.8.0; add to "Critical gotchas" that exposing an agent after gateway start now self-heals (no restart), and that `hermes-bgos-doctor` is the one-command health check. Update the "When the process improves" section to note the doctor + hot-refresh shipped in 0.8.0.

- [ ] **Step 5: Verify by reading**

Run: `grep -n "hermes-bgos-doctor\|--agents\|hot-refresh\|SCOPE_REFRESH" README.md` — confirm the edits landed.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: README — doctor, --agents, hot-reload, profile env paths"
# (Skill files live outside the repo; commit separately if under version control,
#  otherwise they're edited in place.)
```

---

## Task 10: Full suite + lint pass

**Files:** none (verification)

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (existing + new). Note total counts before/after.

- [ ] **Step 2: Sanity-run the doctor end-to-end**

Run: `.venv/bin/hermes-bgos-doctor --offline` and `.venv/bin/hermes-bgos-doctor --json --offline`
Expected: human + JSON output; exit 1 (fork_patch FAIL in this dev env is expected — the worktree has no patched Hermes).

- [ ] **Step 3: Final commit (if anything uncommitted) + branch ready for PR**

```bash
git status --short
git log --oneline origin/main..HEAD
```
Expected: clean tree; the feature commits listed. Branch is ready to push and open a PR.

---

## Self-Review

**Spec coverage:** Hot-refresh → Task 3. Doctor → Task 7. Pair `--agents` → Task 5. Pair `--wait-for-exposure` → Task 6. Shared parser → Task 1 (+ Task 2 wiring). Startup logs → Task 4. Version/script → Task 8. Docs/skill → Task 9. All spec components mapped.

**Placeholder scan:** No TBD/TODO. Every code step has complete code; every test step has runnable assertions.

**Type/name consistency:** `parse_agents_spec` / `enumerate_agents_from_env` (Task 1) reused verbatim in Tasks 2, 5, 7. `wait_for_exposure` (Task 6) signature matches its tests. `CheckResult` fields (`name, status, detail, fix`) consistent across `doctor.py` and `test_doctor.py`. `check_config` returns `tuple[BgosConfig | None, CheckResult]` and the test unpacks exactly that. Console-script name `hermes-bgos-doctor` consistent across pyproject (Task 8) and docs (Task 9).
