# BGOS Hermes Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship BGOS as a drop-in Hermes plugin (`plugins/platforms/bgos/`) that registers via `ctx.register_platform()` — replacing the 11-file fork patch on plugin-capable Hermes (patch kept as fallback) and fixing cron delivery.

**Architecture:** A thin plugin dir (`plugin.yaml` + `adapter.py` with `register(ctx)`) imports the existing `hermes_channel_bgos` pip package. Registration hooks (`env_enablement`, `standalone_send`, platform hint) live in a new `hermes_channel_bgos/plugin.py` so they're versioned + unit-tested. No adapter behavior changes.

**Tech Stack:** Python 3.11+, `click`, `httpx`, `pytest` (`asyncio_mode=auto`), PyYAML (test-only, for plugin.yaml validation), in-repo aiohttp mock backend.

**Working dir:** `/Users/kc/Projects/BGOS/hermes-channel-bgos/.claude/worktrees/bgos-hermes-plugin` (branch `feat/bgos-hermes-plugin`, off `main` @ v0.8.0).

**Run tests with:** `.venv/bin/python -m pytest` (create venv first: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]" PyYAML`).

**Reference:** the real upstream `ntfy` plugin (`NousResearch/hermes-agent:plugins/platforms/ntfy/adapter.py`) is the canonical example for every API shape used below.

---

## File Structure

- **Create** `src/hermes_channel_bgos/plugin.py` — `BGOS_PLATFORM_HINT`, `env_enablement()`, `standalone_send()`, `resolve_pairing()`.
- **Create** `plugins/platforms/bgos/{__init__.py, plugin.yaml, adapter.py}` — the Hermes plugin (thin shim + `register(ctx)`).
- **Modify** `src/hermes_channel_bgos/doctor.py` — `registration` check (plugin | patch | none).
- **Modify** `pyproject.toml` + `src/hermes_channel_bgos/__init__.py` — version 0.8.0 → 0.9.0.
- **Modify** `README.md` + the `bgos-integrate-hermes-agent` skill — plugin path primary, patch fallback.
- **Create** `tests/test_plugin.py`; **extend** `tests/test_doctor.py`.

---

## Task 1: `plugin.py` — pairing resolver + `env_enablement`

**Files:**
- Create: `src/hermes_channel_bgos/plugin.py`
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plugin.py
"""Tests for the Hermes-plugin registration hooks."""
from __future__ import annotations

import json

import pytest

from hermes_channel_bgos.plugin import env_enablement, resolve_pairing


def test_resolve_pairing_from_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    monkeypatch.delenv("BGOS_BACKEND_URL", raising=False)
    secrets = tmp_path / "secrets" / "bgos.json"
    secrets.parent.mkdir(parents=True)
    secrets.write_text(json.dumps({"pairing_token": "tok", "base_url": "http://x"}))
    token, base_url = resolve_pairing()
    assert token == "tok"
    assert base_url == "http://x"


def test_resolve_pairing_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "envtok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://env")
    token, base_url = resolve_pairing()
    assert token == "envtok"
    assert base_url == "http://env"


def test_resolve_pairing_none_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    token, base_url = resolve_pairing()
    assert token is None
    assert base_url == "https://api.brandgrowthos.ai"  # prod default


def test_env_enablement_seeds_home_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_HOME_CHANNEL", "830")
    monkeypatch.setenv("BGOS_HOME_CHANNEL_NAME", "Ops")
    seed = env_enablement()
    assert seed is not None
    assert seed["home_channel"] == {"chat_id": "830", "name": "Ops"}


def test_env_enablement_none_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    assert env_enablement() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_plugin.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_channel_bgos.plugin'`.

- [ ] **Step 3: Implement (resolver + env_enablement + hint placeholder)**

```python
# src/hermes_channel_bgos/plugin.py
"""Hermes plugin registration hooks for the BGOS platform.

The plugin directory (`plugins/platforms/bgos/adapter.py`) is a thin shim that
imports these from the installed pip package and passes them to
`ctx.register_platform(...)`. Keeping the hooks here (not in the plugin dir)
means they're versioned and unit-tested with the rest of the package.

A single `ctx.register_platform()` call replaces the entire fork patch on
plugin-capable Hermes; the patch stays as a fallback for older installs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_PROD_BASE_URL = "https://api.brandgrowthos.ai"


def _secrets_path() -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "secrets" / "bgos.json"


def resolve_pairing() -> tuple[str | None, str]:
    """Resolve (pairing_token, base_url) from env + the secrets file — the same
    precedence the adapter uses (`BGOS_API_KEY` → secrets token; `BGOS_BACKEND_URL`
    → secrets base_url → prod default). Returns token=None when not paired.
    Standalone (no adapter import) so it works in the cron path and in tests."""
    secrets: dict = {}
    sp = _secrets_path()
    if sp.is_file():
        try:
            secrets = json.loads(sp.read_text())
        except (OSError, ValueError):
            secrets = {}
    token = os.environ.get("BGOS_API_KEY") or secrets.get("pairing_token")
    base_url = (
        os.environ.get("BGOS_BACKEND_URL")
        or secrets.get("base_url")
        or _PROD_BASE_URL
    )
    return token, base_url


def env_enablement() -> dict | None:
    """Seed `PlatformConfig.extra` from env during gateway config load (before
    adapter construction), so `hermes gateway status` reflects env-only setups.
    Returns None when BGOS isn't minimally configured (no pairing token). The
    special `home_channel` key is promoted to a `HomeChannel` dataclass by the
    plugin registry's core hook. Mirrors ntfy's `_env_enablement`."""
    token, base_url = resolve_pairing()
    if not token:
        return None
    seed: dict[str, Any] = {"backend_url": base_url}
    home = os.environ.get("BGOS_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.environ.get("BGOS_HOME_CHANNEL_NAME", home),
        }
    return seed


# Replaced in Task 2 + Task 3. Defined here so imports in those tasks resolve.
BGOS_PLATFORM_HINT = ""  # set in Task 3 (extracted from the fork patch)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_plugin.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/plugin.py tests/test_plugin.py
git commit -m "feat(plugin): pairing resolver + env_enablement hook"
```

---

## Task 2: `plugin.py` — `standalone_send` (cron delivery fix)

**Files:**
- Modify: `src/hermes_channel_bgos/plugin.py`
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plugin.py`:

```python
async def test_standalone_send_posts_via_bgos_api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BGOS_API_KEY", "tok")
    monkeypatch.setenv("BGOS_BACKEND_URL", "http://x")

    from hermes_channel_bgos import plugin as plugin_mod

    captured = {}

    class FakeApi:
        def __init__(self, config):
            captured["base_url"] = config.base_url
            captured["token"] = config.pairing_token

        async def post_message(self, *, chat_id, text, **kw):
            captured["chat_id"] = chat_id
            captured["text"] = text
            return {"id": 4321}

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(plugin_mod, "BgosApi", FakeApi)

    result = await plugin_mod.standalone_send(None, "830", "scheduled hello")
    assert result["success"] is True
    assert result["message_id"] == 4321
    assert captured["chat_id"] == 830          # coerced to int
    assert captured["text"] == "scheduled hello"
    assert captured["token"] == "tok"
    assert captured["closed"] is True


async def test_standalone_send_errors_when_unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BGOS_API_KEY", raising=False)
    from hermes_channel_bgos import plugin as plugin_mod
    result = await plugin_mod.standalone_send(None, "830", "hi")
    assert "error" in result
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_plugin.py -q -k standalone`
Expected: FAIL — `AttributeError: module 'hermes_channel_bgos.plugin' has no attribute 'standalone_send'`.

- [ ] **Step 3: Implement**

Add the `BgosApi`/`BgosConfig` imports at the top of `plugin.py` (after `from typing import Any`):

```python
from .bgos_api import BgosApi, BgosApiError
from .config import BgosConfig
```

Add the function at the end of `plugin.py` (before the `BGOS_PLATFORM_HINT` line):

```python
async def standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict:
    """Out-of-process send for cron / `send_message_tool` when the gateway
    runner isn't in this process. Without this hook, `deliver=bgos` cron jobs
    fail with "No live adapter for platform" — and historically the fork left
    bgos sending unimplemented ("Direct sending not yet implemented for bgos").

    `thread_id` / `media_files` / `force_document` are accepted for signature
    parity with the registry's sender protocol; BGOS delivers text here.
    """
    token, base_url = resolve_pairing()
    if not token:
        return {"error": "bgos standalone send: not paired (no token)"}
    api = BgosApi(BgosConfig(base_url=base_url, pairing_token=token))
    try:
        resp = await api.post_message(chat_id=int(chat_id), text=message)
    except BgosApiError as exc:
        return {"error": f"bgos standalone send: HTTP {exc.status}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"bgos standalone send failed: {exc.__class__.__name__}"}
    finally:
        await api.close()
    msg_id = resp.get("id") if isinstance(resp, dict) else None
    return {"success": True, "platform": "bgos", "chat_id": chat_id, "message_id": msg_id}
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_plugin.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/plugin.py tests/test_plugin.py
git commit -m "feat(plugin): standalone_send hook (cron delivery fix)"
```

---

## Task 3: The plugin directory + `register(ctx)`

**Files:**
- Create: `plugins/platforms/bgos/__init__.py` (empty)
- Create: `plugins/platforms/bgos/plugin.yaml`
- Create: `plugins/platforms/bgos/adapter.py`
- Modify: `src/hermes_channel_bgos/plugin.py` (set `BGOS_PLATFORM_HINT`)
- Test: `tests/test_plugin.py`

- [ ] **Step 1: Set `BGOS_PLATFORM_HINT` from the canonical hint text**

The agent-facing hint already exists as the authored `bgos` entry in the fork patch's `PLATFORM_HINTS`. Extract it verbatim (strip the leading `+`, drop the `"bgos": """` opener and the closing `""",`) so there's a single source of truth:

```bash
sed -n '31,100p' hermes-fork-patch/0001-bgos-integration.patch \
  | sed 's/^+//' > /tmp/bgos_hint_block.txt
# /tmp/bgos_hint_block.txt line 1 starts: '"bgos": """You are speaking...'
# last line ends: '...not supported."""'
head -1 /tmp/bgos_hint_block.txt; tail -1 /tmp/bgos_hint_block.txt
```

Replace the `BGOS_PLATFORM_HINT = ""` line in `plugin.py` with a triple-quoted literal containing the extracted text **between** the `"""` markers (i.e. from `You are speaking with the user through BGOS …` through `… are not supported.`). Concretely:

```python
BGOS_PLATFORM_HINT = """You are speaking with the user through BGOS — a mobile-first chat app (iOS, Android, desktop) polished like Telegram or iMessage. The user sees your responses as chat bubbles in a rich UI.
<PASTE THE REMAINING EXTRACTED LINES VERBATIM — the implementer copies lines 2..N of /tmp/bgos_hint_block.txt, stopping before the closing triple-quote>
Each BGOS chat maps to a single Hermes conversation. DMs only — no group threads, no forum topics. The user can wipe context via `/new`. Typing indicators, stickers, reactions, and message editing by the user are not supported."""
```

(The implementer pastes the full extracted body; the first and last lines above are the exact anchors to match.)

- [ ] **Step 2: Write the failing test**

Append to `tests/test_plugin.py`:

```python
def test_plugin_yaml_is_valid_and_declares_platform():
    import pathlib
    import yaml
    repo = pathlib.Path(__file__).resolve().parent.parent
    data = yaml.safe_load((repo / "plugins/platforms/bgos/plugin.yaml").read_text())
    assert data["kind"] == "platform"
    assert data["name"]
    req = {e["name"] for e in data.get("requires_env", [])}
    opt = {e["name"] for e in data.get("optional_env", [])}
    assert "BGOS_AGENTS" in (req | opt)
    assert "BGOS_ALLOW_ALL_USERS" in (req | opt)


def test_register_wires_all_hooks(monkeypatch):
    import importlib.util
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "bgos_plugin_adapter", repo / "plugins/platforms/bgos/adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}

    class FakeCtx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    mod.register(FakeCtx())
    assert captured["name"] == "bgos"
    assert captured["cron_deliver_env_var"] == "BGOS_HOME_CHANNEL"
    assert captured["allow_all_env"] == "BGOS_ALLOW_ALL_USERS"
    assert captured["allowed_users_env"] == "BGOS_ALLOWED_USERS"
    assert callable(captured["adapter_factory"])
    assert callable(captured["env_enablement_fn"])
    assert callable(captured["standalone_sender_fn"])
    assert captured["platform_hint"]
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_plugin.py -q -k "plugin_yaml or register"`
Expected: FAIL — plugin dir / files don't exist yet (FileNotFoundError / import error).

- [ ] **Step 4: Create the plugin files**

`plugins/platforms/bgos/__init__.py`:
```python
```
(empty file)

`plugins/platforms/bgos/plugin.yaml`:
```yaml
name: bgos-platform
label: BGOS
kind: platform
version: 0.9.0
description: >
  BGOS (Brand GrowthOS) channel adapter for Hermes — chat with your Hermes
  agents from the BGOS mobile/desktop app. Pairs via `hermes-pair-bgos`;
  all logic lives in the `hermes-channel-bgos` pip package (install it into
  Hermes's Python first). This plugin is the drop-in alternative to the
  legacy fork patch on plugin-capable Hermes.
author: Brand GrowthOS
requires_env: []
optional_env:
  - name: BGOS_AGENTS
    description: "Comma-separated route:Name agents to expose (e.g. default:Hermes)"
    prompt: "BGOS agents (route:Name, comma-separated)"
    password: false
  - name: BGOS_ALLOW_ALL_USERS
    description: "Allow any BGOS user to message this agent (true/false)"
    prompt: "Allow all BGOS users? (true/false)"
    password: false
  - name: BGOS_ALLOWED_USERS
    description: "Comma-separated Clerk user IDs allowed (allowlist alternative)"
    prompt: "Allowed BGOS user IDs (comma-separated)"
    password: false
  - name: BGOS_BACKEND_URL
    description: "BGOS backend base URL (default https://api.brandgrowthos.ai)"
    prompt: "BGOS backend URL (or empty)"
    password: false
  - name: BGOS_HOME_CHANNEL
    description: "BGOS chat ID for cron / notification delivery"
    prompt: "Home channel chat ID (or empty)"
    password: false
  - name: BGOS_HOME_CHANNEL_NAME
    description: "Human label for the home channel"
    prompt: "Home channel display name (or empty)"
    password: false
```

`plugins/platforms/bgos/adapter.py`:
```python
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
    """The pip package import above is the only hard requirement; if this
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
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_plugin.py -q`
Expected: PASS (9 tests). If `register_platform` rejects an unknown kwarg when run against a *real* Hermes later, drop that kwarg — the FakeCtx test won't catch that, so note it for the on-server verification step.

- [ ] **Step 6: Commit**

```bash
git add plugins/ src/hermes_channel_bgos/plugin.py tests/test_plugin.py
git commit -m "feat(plugin): bgos plugin dir + register(ctx) wiring all touch-points"
```

---

## Task 4: Doctor `registration` check (plugin | patch | none)

**Files:**
- Modify: `src/hermes_channel_bgos/doctor.py` (replace `check_fork_patch`)
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_doctor.py`:

```python
def test_check_registration_reports_none_without_hermes(monkeypatch):
    # No `gateway` importable in the test env, and Platform.BGOS not present.
    from hermes_channel_bgos.doctor import check_registration, FAIL
    r = check_registration()
    assert r.name == "registration"
    assert r.status == FAIL
    assert "plugin" in r.fix.lower() or "patch" in r.fix.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_doctor.py -q -k registration`
Expected: FAIL — `ImportError: cannot import name 'check_registration'`.

- [ ] **Step 3: Replace `check_fork_patch` with `check_registration`**

In `doctor.py`, replace the entire `check_fork_patch` function with:

```python
def check_registration() -> CheckResult:
    """Is BGOS registered with Hermes — via the plugin system OR the fork patch?
    Reports which path is active, or FAIL with how to enable one."""
    try:
        from gateway.config import Platform  # type: ignore
    except Exception as exc:
        return CheckResult(
            "registration", FAIL,
            f"Hermes gateway not importable ({exc.__class__.__name__})",
            fix="Run this from Hermes's Python env. Then either install the BGOS "
                "plugin (symlink plugins/platforms/bgos into ~/.hermes/plugins/) "
                "or apply the fork patch.",
        )
    if getattr(Platform, "BGOS", None) is None:
        return CheckResult(
            "registration", FAIL, "Platform.BGOS not registered",
            fix="Enable BGOS: plugin path — symlink plugins/platforms/bgos into "
                "~/.hermes/plugins/bgos and restart; or legacy — apply "
                "hermes-fork-patch/0001-bgos-integration.patch.",
        )
    # Distinguish plugin vs patch: the plugin loader records loaded plugins.
    try:
        import gateway.platform_registry as _reg  # type: ignore
        loaded = getattr(_reg, "loaded_platform_plugins", lambda: [])()
        via = "plugin" if any("bgos" in str(p).lower() for p in loaded) else "patch"
    except Exception:
        via = "patch"  # registry not present → must be the fork-patch build
    return CheckResult("registration", OK, f"Platform.BGOS registered (via {via})")
```

Update `run_checks` to call the renamed function — change `check_fork_patch()` to `check_registration()` in the `results = [...]` list.

- [ ] **Step 4: Run to verify pass (+ no regression)**

Run: `.venv/bin/python -m pytest tests/test_doctor.py -q`
Expected: PASS. (The old `test_doctor_main_exits_1_when_unconfigured` still passes — `registration` FAILs in the no-Hermes test env, so exit is still 1; update that test's `assert "fork_patch" in result.output` to `assert "registration" in result.output`.)

- [ ] **Step 5: Commit**

```bash
git add src/hermes_channel_bgos/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): registration check reports plugin|patch|none"
```

---

## Task 5: Version bump + docs (plugin primary, patch fallback)

**Files:**
- Modify: `pyproject.toml`, `src/hermes_channel_bgos/__init__.py`
- Modify: `README.md`
- Modify: both `bgos-integrate-hermes-agent/SKILL.md` copies (`~/.claude/skills/...` and `/Users/kc/Projects/BGOS/.claude/skills/...`)

- [ ] **Step 1: Bump version 0.8.0 → 0.9.0**

In `pyproject.toml` change `version = "0.8.0"` → `version = "0.9.0"`. In `src/hermes_channel_bgos/__init__.py` change `__version__ = "0.8.0"` → `__version__ = "0.9.0"`.

- [ ] **Step 2: README — add the plugin path as primary install**

In the "Manual setup" section intro (after the Quick-start prompt), add a new subsection **before** "Step 1 — Apply the Hermes fork patch":

```markdown
### Step 0 — Plugin path (preferred on modern Hermes)

If your Hermes has the plugin system (`<hermes-python> -c "import gateway.platform_registry"` succeeds), you can **skip the fork patch entirely**:

1. Install the pip package into Hermes's Python (Step 2 below).
2. Make the plugin discoverable:
   ```bash
   mkdir -p ~/.hermes/plugins
   ln -sfn ~/hermes-channel-bgos/plugins/platforms/bgos ~/.hermes/plugins/bgos
   ```
3. Restart Hermes. Verify with `hermes-bgos-doctor` (the `registration` line should read `via plugin`).

This registers `Platform.BGOS` (adapter, config, auth, cron delivery, prompt hint, status) with **no core-file edits and nothing to rebase on upstream updates**. If `import gateway.platform_registry` fails, your Hermes predates the plugin system — use the fork-patch steps below instead.
```

Also update the Quick-start prompt's patch step (step 3) to lead with: "If `import gateway.platform_registry` succeeds, prefer the plugin path (symlink `plugins/platforms/bgos` into `~/.hermes/plugins/`) and skip the patch."

- [ ] **Step 3: Skill — same plugin-first guidance**

In BOTH `SKILL.md` copies, add a step before "3. Apply the fork patch to Hermes":

```markdown
### 2b. Prefer the plugin path (modern Hermes)

Check: `<hermes-python> -c "import gateway.platform_registry" && echo PLUGIN_OK`.
If it prints `PLUGIN_OK`, skip the fork patch (step 3) — instead, after installing the pip package (step 4):

```bash
mkdir -p ~/.hermes/plugins
ln -sfn ~/hermes-channel-bgos/plugins/platforms/bgos ~/.hermes/plugins/bgos
```

Then restart Hermes and confirm `hermes-bgos-doctor` shows `registration: ... via plugin`. Only fall back to the fork patch (step 3) if the import fails.
```

Mirror the file: `cp ~/.claude/skills/bgos-integrate-hermes-agent/SKILL.md /Users/kc/Projects/BGOS/.claude/skills/bgos-integrate-hermes-agent/SKILL.md`.

- [ ] **Step 4: Verify + commit**

Run: `grep -n "platform_registry\|~/.hermes/plugins" README.md`
Expected: the new plugin-path lines present.

```bash
git add pyproject.toml src/hermes_channel_bgos/__init__.py README.md
git commit -m "chore: v0.9.0 + docs for plugin install path (patch as fallback)"
```

---

## Task 6: Full suite + finish

- [ ] **Step 1: Reinstall + full suite**

Run:
```bash
.venv/bin/pip install -e . -q
.venv/bin/python -m pytest -q
```
Expected: all PASS (existing + new plugin/doctor tests).

- [ ] **Step 2: Sanity — plugin imports + register works offline**

Run:
```bash
.venv/bin/python -c "import importlib.util, pathlib; \
s=importlib.util.spec_from_file_location('a','plugins/platforms/bgos/adapter.py'); \
m=importlib.util.module_from_spec(s); s.loader.exec_module(m); \
print('register callable:', callable(m.register))"
```
Expected: `register callable: True`.

- [ ] **Step 3: Confirm clean tree + commits**

Run: `git status --short && git log --oneline origin/main..HEAD`
Expected: clean tree; the feature commits listed.

---

## Self-Review

**Spec coverage:** plugin.py hooks → Tasks 1–2; plugin dir + register → Task 3; doctor registration → Task 4; version + docs (plugin primary, patch fallback) → Task 5; testing throughout. All spec components mapped.

**Placeholder scan:** the only intentional `<PASTE …>` is the platform-hint body in Task 3 — unavoidable (it's a 70-line verbatim copy), with exact first/last-line anchors + the extraction command given, so it's reproducible, not hand-wavy.

**Type/name consistency:** `resolve_pairing()`, `env_enablement()`, `standalone_send()`, `BGOS_PLATFORM_HINT` defined in Tasks 1–2 and imported by name in Task 3's `adapter.py`. `check_registration` (Task 4) replaces `check_fork_patch` and `run_checks` is updated to match. `_DEFAULT_MAX_MESSAGE_LENGTH` imported from `bgos_adapter` (exists at bgos_adapter.py:164).

**On-server verification (carried from spec):** the `register_platform` kwargs + `home_channel` seed shape + plugin auto-discovery from `~/.hermes/plugins/` must be confirmed against the *installed* Hermes (unit tests use a FakeCtx and can't catch a rejected kwarg). This is the same "verify on a real server" step the v0.8.0 work ended on — not a blocker for building + unit-testing the package.
