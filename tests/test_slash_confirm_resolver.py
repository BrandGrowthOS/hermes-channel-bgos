"""Slash-command confirm buttons (/reload-mcp) against the real Hermes resolver shape.

Hermes ships the resolver as ``tools.slash_confirm.resolve`` (never
``gateway.slash_confirm``). It is a coroutine function,
``resolve(session_key, confirm_id, choice, timeout=300) -> Optional[str]``:
it runs the handler the gateway registered and returns the handler's text
(``None`` when the confirm is stale, already answered or superseded).
Hermes's own button adapters (Telegram, WhatsApp, the relay) post that text
back into the chat after the tap.

These tests load ``tests/hermes_stubs/slash_confirm.py`` (a copy of the
upstream module) as ``tools.slash_confirm`` instead of monkeypatching the
adapter's resolver with a fake, so the module path, the await and the return
value are all exercised for real.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

import hermes_channel_bgos.bgos_adapter as adapter_mod
from hermes_channel_bgos.bgos_adapter import BGOSAdapter
from hermes_channel_bgos.config import BgosConfig

STUB = Path(__file__).parent / "hermes_stubs" / "slash_confirm.py"


def _install_hermes_slash_confirm(monkeypatch) -> types.ModuleType:
    """Make ``import tools.slash_confirm`` find the upstream-shaped module."""
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # a package, so the submodule import resolves
    spec = importlib.util.spec_from_file_location("tools.slash_confirm", STUB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools_pkg.slash_confirm = module
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.slash_confirm", module)
    return module


def _require_import_time_fallback() -> None:
    """Two tests need the adapter as loaded without Hermes (the fallback
    resolver bound at import). With a real Hermes installed it binds
    upstream's resolve instead, and those tests have nothing to show."""
    if getattr(adapter_mod.resolve_slash_confirm, "__module__", None) != adapter_mod.__name__:
        pytest.skip("a real tools.slash_confirm was importable when the adapter loaded")


def _block_hermes_slash_confirm(monkeypatch) -> None:
    """An older Hermes (or none at all): ``import tools.slash_confirm`` fails."""
    monkeypatch.setitem(sys.modules, "tools.slash_confirm", None)


class _Recorder:
    def __init__(self) -> None:
        self.patches: list[dict] = []
        self.posts: list[dict] = []

    async def patch_message(self, mid, *, text=None, options=None, **kw):
        self.patches.append({"message_id": mid, "text": text, "options": options})
        return {"id": mid}

    async def post_message(self, **kw):
        self.posts.append(kw)
        return {"id": 900 + len(self.posts)}


def _adapter(monkeypatch) -> tuple[BGOSAdapter, _Recorder]:
    monkeypatch.setenv("BGOS_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("BGOS_CHAT_STYLE", "everything")
    adapter = BGOSAdapter(BgosConfig(base_url="http://x", pairing_token="pair_xyz"))
    rec = _Recorder()
    monkeypatch.setattr(adapter._api, "patch_message", rec.patch_message)
    monkeypatch.setattr(adapter._api, "post_message", rec.post_message)
    return adapter, rec


def _tap(choice: str, confirm_id: str) -> dict:
    return {
        "callback_data": f"sc:{choice}:{confirm_id}",
        "user_id": "u_1",
        "message_id": 50,
        "chat_id": 1,
    }


def _reload_handler(calls: list[str]):
    async def handler(choice: str):
        calls.append(choice)
        if choice == "cancel":
            return None
        return "🔄 Reloaded 3 MCP servers."
    return handler


async def test_tap_reaches_tools_slash_confirm_installed_after_import(monkeypatch):
    """The plugin can be imported before Hermes's ``tools`` package is on the
    path (the tests do exactly that). The tap must still find the real
    resolver, await it, run the gateway's handler and post its text."""
    _require_import_time_fallback()
    sc = _install_hermes_slash_confirm(monkeypatch)
    calls: list[str] = []
    sc.register("sess-1", "conf-1", "reload-mcp", _reload_handler(calls))
    adapter, rec = _adapter(monkeypatch)
    await adapter.send_slash_confirm(
        chat_id=1, title="Reload MCP?", message="This invalidates the prompt cache.",
        session_key="sess-1", confirm_id="conf-1",
    )
    rec.posts.clear()

    await adapter._handle_callback(_tap("once", "conf-1"))

    assert calls == ["once"]
    assert sc.get_pending("sess-1") is None
    assert rec.patches[0]["text"].startswith("✅ Approved once by u_1")
    assert rec.patches[0]["options"] == []
    assert [p["text"] for p in rec.posts] == ["🔄 Reloaded 3 MCP servers."]
    assert rec.posts[0]["chat_id"] == 1
    assert rec.posts[0]["sender"] == "assistant"


async def test_tap_with_resolver_bound_at_import_posts_the_handler_text(monkeypatch):
    """On a real Hermes the import succeeds at module load and the adapter's
    resolver IS ``tools.slash_confirm.resolve``. Its return value is the
    command's answer and must reach the chat."""
    sc = _install_hermes_slash_confirm(monkeypatch)
    monkeypatch.setattr(adapter_mod, "resolve_slash_confirm", sc.resolve)
    calls: list[str] = []
    sc.register("sess-2", "conf-2", "reload-mcp", _reload_handler(calls))
    adapter, rec = _adapter(monkeypatch)
    adapter._slash_confirm_state["conf-2"] = "sess-2"

    await adapter._handle_callback(_tap("always", "conf-2"))

    assert calls == ["always"]
    assert rec.patches[0]["text"].startswith("🔒 Always approve by u_1")
    assert [p["text"] for p in rec.posts] == ["🔄 Reloaded 3 MCP servers."]


async def test_cancel_or_superseded_confirm_posts_nothing_extra(monkeypatch):
    """resolve returns None for a cancel whose handler says nothing and for a
    confirm another prompt superseded; neither posts a follow-up or raises."""
    sc = _install_hermes_slash_confirm(monkeypatch)
    monkeypatch.setattr(adapter_mod, "resolve_slash_confirm", sc.resolve)
    calls: list[str] = []
    adapter, rec = _adapter(monkeypatch)

    sc.register("sess-3", "conf-3", "reload-mcp", _reload_handler(calls))
    adapter._slash_confirm_state["conf-3"] = "sess-3"
    await adapter._handle_callback(_tap("cancel", "conf-3"))
    assert calls == ["cancel"]

    # A newer prompt for the same session supersedes conf-old.
    sc.register("sess-3", "conf-new", "reload-mcp", _reload_handler(calls))
    adapter._slash_confirm_state["conf-old"] = "sess-3"
    await adapter._handle_callback(_tap("once", "conf-old"))
    assert calls == ["cancel"], "a superseded confirm must not run the newer handler"
    assert sc.get_pending("sess-3")["confirm_id"] == "conf-new"

    assert rec.posts == []
    assert len(rec.patches) == 2


async def test_missing_resolver_logs_once_and_points_to_typed_answers(monkeypatch, caplog):
    """A Hermes without tools.slash_confirm: the tap never raises, the bubble
    says to type the answer instead, and the gateway log says why exactly
    once, however many taps arrive."""
    _require_import_time_fallback()
    _block_hermes_slash_confirm(monkeypatch)
    monkeypatch.setattr(adapter_mod, "_slash_confirm_missing_logged", False, raising=False)
    adapter, rec = _adapter(monkeypatch)
    caplog.set_level(logging.WARNING, logger=adapter_mod.log.name)

    for confirm_id in ("conf-a", "conf-b"):
        adapter._slash_confirm_state[confirm_id] = "sess-4"
        await adapter._handle_callback(_tap("once", confirm_id))

    assert len(rec.patches) == 2
    for patch in rec.patches:
        assert "/approve" in patch["text"] and "/cancel" in patch["text"]
        assert not patch["text"].startswith("✅")
        assert patch["options"] == []
    assert rec.posts == []
    missing = [r for r in caplog.records if "tools.slash_confirm" in r.getMessage()]
    assert len(missing) == 1, [r.getMessage() for r in caplog.records]


async def test_resolver_error_is_logged_and_never_claims_approval(monkeypatch, caplog):
    async def broken(session_key, confirm_id, choice):
        raise ValueError("boom")

    monkeypatch.setattr(adapter_mod, "resolve_slash_confirm", broken)
    adapter, rec = _adapter(monkeypatch)
    caplog.set_level(logging.WARNING, logger=adapter_mod.log.name)
    adapter._slash_confirm_state["conf-e"] = "sess-5"

    await adapter._handle_callback(_tap("once", "conf-e"))

    assert len(rec.patches) == 1
    assert not rec.patches[0]["text"].startswith("✅")
    assert rec.posts == []
    assert any("slash-confirm resolve failed" in r.getMessage() for r in caplog.records)


def test_stub_matches_the_real_resolver_shape():
    """Guard the stub itself: an async resolve with the upstream signature."""
    import inspect

    spec = importlib.util.spec_from_file_location("_sc_shape", STUB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert inspect.iscoroutinefunction(module.resolve)
    params = list(inspect.signature(module.resolve).parameters)
    assert params == ["session_key", "confirm_id", "choice", "timeout"]


@pytest.fixture(autouse=True)
def _reset_missing_log_flag(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_slash_confirm_missing_logged", False, raising=False)
