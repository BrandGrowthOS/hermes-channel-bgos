"""Tests for the BGOS Skills Store control-plane bridge."""
from __future__ import annotations

import os
import sys
import asyncio
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from hermes_channel_bgos.config import BgosConfig


pytestmark = pytest.mark.asyncio


def _write_skill(
    skills_dir: Path,
    name: str,
    description: str,
    *,
    frontmatter_name: str | None = None,
) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    display_name = frontmatter_name or name
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {display_name}\ndescription: {description}\n---\n\nInstructions.\n",
        encoding="utf-8",
    )
    return skill_dir


def _install_fake_machinery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hub_records: dict[str, dict[str, Any]] | None = None,
    browse_result: Any = None,
    search_result: Any = None,
    install_impl: Callable[..., None] | None = None,
    uninstall_impl: Callable[..., None] | None = None,
) -> dict[str, Any]:
    records = hub_records if hub_records is not None else {}
    state: dict[str, Any] = {
        "records": records,
        "browse_calls": [],
        "search_calls": [],
        "install_calls": [],
        "uninstall_calls": [],
    }

    class FakeHubLockFile:
        def load(self) -> dict[str, Any]:
            return {"version": 1, "installed": dict(records)}

        def list_installed(self) -> list[dict[str, Any]]:
            return [
                {"name": name, **record}
                for name, record in records.items()
            ]

        def get_installed(self, name: str) -> dict[str, Any] | None:
            return records.get(name)

    cli_module = ModuleType("hermes_cli.skills_hub")
    tools_module = ModuleType("tools.skills_hub")

    def browse_skills(*, page: int, page_size: int) -> Any:
        state["browse_calls"].append({"page": page, "page_size": page_size})
        if browse_result is not None:
            return browse_result
        return {"items": [], "page": page}

    def inspect_skill(_identifier: str) -> None:
        return None

    def do_install(
        identifier: str,
        *,
        skip_confirm: bool,
        console: Any,
        name_override: str,
    ) -> None:
        state["install_calls"].append(
            {
                "identifier": identifier,
                "skip_confirm": skip_confirm,
                "console": console,
                "name_override": name_override,
            }
        )
        if install_impl is not None:
            install_impl(identifier, console, name_override)

    def do_uninstall(
        name: str,
        *,
        skip_confirm: bool,
        console: Any,
    ) -> None:
        state["uninstall_calls"].append(
            {
                "name": name,
                "skip_confirm": skip_confirm,
                "console": console,
            }
        )
        if uninstall_impl is not None:
            uninstall_impl(name, console)

    def unified_search(*, query: str) -> Any:
        state["search_calls"].append(query)
        return search_result if search_result is not None else []

    cli_module.do_install = do_install  # type: ignore[attr-defined]
    cli_module.do_uninstall = do_uninstall  # type: ignore[attr-defined]
    cli_module.browse_skills = browse_skills  # type: ignore[attr-defined]
    cli_module.inspect_skill = inspect_skill  # type: ignore[attr-defined]
    tools_module.unified_search = unified_search  # type: ignore[attr-defined]
    tools_module.HubLockFile = FakeHubLockFile  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "hermes_cli.skills_hub", cli_module)
    monkeypatch.setitem(sys.modules, "tools.skills_hub", tools_module)
    return state


def _make_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    from hermes_channel_bgos.skills_bridge import SkillsBridge

    bridge = SkillsBridge(
        BgosConfig(base_url="https://bgos.test", pairing_token="pair_xyz")
    )
    posts: list[tuple[str, dict[str, Any]]] = []

    async def record_post(path: str, body: dict[str, Any]) -> None:
        posts.append((path, body))

    monkeypatch.setattr(bridge, "_post", record_post)
    return bridge, posts


def _post_kind(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _result_bodies(posts: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [body for path, body in posts if _post_kind(path) == "result"]


async def test_unknown_op_posts_ack_then_unsupported_result(monkeypatch):
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame({"rpcId": "rpc-unknown", "op": "wat", "payload": {}})

    assert [_post_kind(path) for path, _body in posts] == ["ack", "result"]
    assert posts[0][1] == {}
    assert posts[1][1] == {
        "ok": False,
        "error": {"code": "install_failed", "message": "unsupported op"},
    }


async def test_list_installed_classifies_bundled_hub_and_self_taught(monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / ".bundled_manifest").write_text(
        "bundled-skill:0123456789abcdef\n",
        encoding="utf-8",
    )
    _write_skill(skills_dir, "bundled-skill", "Bundled description")
    _write_skill(skills_dir, "hub-skill", "Hub description")
    _write_skill(skills_dir, "official-skill", "Official description")
    _write_skill(skills_dir, "learned-skill", "Learned description")
    _install_fake_machinery(
        monkeypatch,
        hub_records={
            "hub-skill": {
                "source": "github",
                "trust_level": "trusted",
                "installed_at": "2026-07-01T12:00:00Z",
            },
            "official-skill": {"source": "official"},
        },
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-list", "op": "list_installed", "payload": {}}
    )

    result = _result_bodies(posts)[0]
    assert result["ok"] is True
    by_name = {item["name"]: item for item in result["payload"]["skills"]}
    assert set(by_name) == {
        "bundled-skill",
        "hub-skill",
        "official-skill",
        "learned-skill",
    }
    assert {item["identifier"] for item in by_name.values()} == {
        "bundled-skill",
        "hub-skill",
        "official-skill",
        "learned-skill",
    }
    assert (by_name["bundled-skill"]["provenance"], by_name["bundled-skill"]["removable"]) == (
        "bundled",
        False,
    )
    assert (by_name["hub-skill"]["provenance"], by_name["hub-skill"]["removable"]) == (
        "hub",
        True,
    )
    assert by_name["hub-skill"]["source"] == "github"
    assert by_name["hub-skill"]["trust"] == "trusted"
    assert by_name["hub-skill"]["installedAt"] == "2026-07-01T12:00:00Z"
    assert (
        by_name["official-skill"]["provenance"],
        by_name["official-skill"]["removable"],
    ) == ("official", True)
    assert by_name["official-skill"]["source"] == "official"
    assert (by_name["learned-skill"]["provenance"], by_name["learned-skill"]["removable"]) == (
        "self_taught",
        False,
    )
    assert "learnedAt" in by_name["learned-skill"]


async def test_list_installed_uses_lock_install_path_before_leaf_name(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
    _write_skill(
        skills_dir,
        "shared-leaf",
        "Root skill",
        frontmatter_name="Root Display",
    )
    _write_skill(
        skills_dir / "category",
        "shared-leaf",
        "Nested store skill",
        frontmatter_name="Nested Display",
    )
    _install_fake_machinery(
        monkeypatch,
        hub_records={
            "shared-leaf": {
                "source": "github",
                "install_path": "category/shared-leaf",
            }
        },
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-install-path-list", "op": "list_installed", "payload": {}}
    )

    skills = _result_bodies(posts)[0]["payload"]["skills"]
    by_name = {item["name"]: item for item in skills}
    assert by_name["Root Display"]["provenance"] == "self_taught"
    assert by_name["Root Display"]["removable"] is False
    assert by_name["Nested Display"]["provenance"] == "hub"
    assert by_name["Nested Display"]["removable"] is True


async def test_catalog_without_query_browses_and_maps_installed_flags(monkeypatch):
    state = _install_fake_machinery(
        monkeypatch,
        hub_records={"installed-skill": {"source": "github"}},
        browse_result={
            "items": [
                {
                    "identifier": "owner/repo/installed-skill",
                    "name": "installed-skill",
                    "description": "Already here",
                    "source": "github",
                    "publisher": "Owner",
                    "category": "productivity",
                    "trust": "trusted",
                },
                {
                    "identifier": "owner/repo/new-skill",
                    "name": "new-skill",
                    "description": "Not installed",
                    "source": "github",
                },
            ],
            "page": 2,
            "total_pages": 4,
            "total": 91,
        },
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-catalog", "op": "catalog", "payload": {"page": 2}}
    )

    assert state["browse_calls"] == [{"page": 2, "page_size": 30}]
    assert state["search_calls"] == []
    payload = _result_bodies(posts)[0]["payload"]
    assert payload["page"] == 2
    assert payload["totalPages"] == 4
    assert payload["total"] == 91
    assert payload["items"][0] == {
        "identifier": "owner/repo/installed-skill",
        "name": "installed-skill",
        "description": "Already here",
        "source": "github",
        "publisher": "Owner",
        "category": "productivity",
        "trust": "trusted",
        "installed": True,
    }
    assert payload["items"][1]["installed"] is False


async def test_catalog_with_query_uses_unified_search(monkeypatch):
    state = _install_fake_machinery(
        monkeypatch,
        search_result=[
            SimpleNamespace(
                identifier="owner/repo/search-skill",
                name="search-skill",
                description="Search result",
                source="github",
                publisher="Owner",
                category="research",
                trust_level="community",
            )
        ],
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-search",
            "op": "catalog",
            "payload": {"query": "search me"},
        }
    )

    assert state["search_calls"] == ["search me"]
    assert state["browse_calls"] == []
    payload = _result_bodies(posts)[0]["payload"]
    assert payload["page"] == 1
    assert payload["items"] == [
        {
            "identifier": "owner/repo/search-skill",
            "name": "search-skill",
            "description": "Search result",
            "source": "github",
            "publisher": "Owner",
            "category": "research",
            "trust": "community",
            "installed": False,
        }
    ]


async def test_catalog_with_present_empty_query_uses_unified_search(monkeypatch):
    state = _install_fake_machinery(monkeypatch, search_result=[])
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-empty-search", "op": "catalog", "payload": {"query": ""}}
    )

    assert state["search_calls"] == [""]
    assert state["browse_calls"] == []
    assert _result_bodies(posts)[0]["ok"] is True


async def test_install_happy_path_posts_progress_and_verified_result(monkeypatch):
    records: dict[str, dict[str, Any]] = {}

    def install(identifier: str, _console: Any, _name_override: str) -> None:
        records["good-skill"] = {
            "source": "github",
            "identifier": identifier,
        }

    state = _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        install_impl=install,
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-install",
            "op": "install",
            "payload": {"identifier": "owner/repo/good-skill"},
        }
    )

    assert [(_post_kind(path), body.get("stage")) for path, body in posts] == [
        ("ack", None),
        ("progress", "starting"),
        ("progress", "installing"),
        ("progress", "verifying"),
        ("result", None),
    ]
    assert len(state["install_calls"]) == 1
    call = state["install_calls"][0]
    assert call["identifier"] == "owner/repo/good-skill"
    assert call["skip_confirm"] is True
    assert call["name_override"] == ""
    assert _result_bodies(posts)[0] == {
        "ok": True,
        "payload": {"name": "good-skill"},
    }


async def test_install_display_name_hint_verifies_new_slug_lock_record(monkeypatch):
    records: dict[str, dict[str, Any]] = {}

    def install(identifier: str, _console: Any, _name_override: str) -> None:
        records["internal-bundle-key"] = {
            "source": "github",
            "identifier": identifier,
        }

    _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        install_impl=install,
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-display-name",
            "op": "install",
            "payload": {
                "identifier": "owner/repo/source-skill",
                "name": "Catalog Display Name",
            },
        }
    )

    assert _result_bodies(posts)[0] == {
        "ok": True,
        "payload": {"name": "Catalog Display Name"},
    }


async def test_install_verifies_new_local_directory_against_identifier_slug(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"

    def install(_identifier: str, _console: Any, _name_override: str) -> None:
        _write_skill(
            skills_dir,
            "local-slug",
            "Installed locally",
            frontmatter_name="Internal Frontmatter Name",
        )

    _install_fake_machinery(monkeypatch, install_impl=install)
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-local-slug",
            "op": "install",
            "payload": {
                "identifier": "owner/repo/local-slug",
                "name": "Catalog Local Name",
            },
        }
    )

    assert _result_bodies(posts)[0] == {
        "ok": True,
        "payload": {"name": "Catalog Local Name"},
    }


async def test_install_does_not_treat_rewritten_local_path_as_new(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
    _write_skill(
        skills_dir,
        "existing-directory",
        "Existing local skill",
        frontmatter_name="Old Frontmatter Name",
    )

    def install(_identifier: str, _console: Any, _name_override: str) -> None:
        _write_skill(
            skills_dir,
            "existing-directory",
            "Rewritten local skill",
            frontmatter_name="target-slug",
        )

    _install_fake_machinery(monkeypatch, install_impl=install)
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-rewritten-local",
            "op": "install",
            "payload": {"identifier": "owner/repo/target-slug"},
        }
    )

    assert _result_bodies(posts)[0]["error"]["code"] == "install_failed"


async def test_concurrent_installs_run_machinery_sequentially(monkeypatch):
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def install(identifier: str, _console: Any, _name_override: str) -> None:
        slug = identifier.rsplit("/", 1)[-1]
        order.append(f"start:{slug}")
        if slug == "first-skill":
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            second_started.set()
        records[slug] = {"source": "github", "identifier": identifier}
        order.append(f"end:{slug}")

    _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        install_impl=install,
    )
    bridge, _posts = _make_bridge(monkeypatch)
    first_task = asyncio.create_task(
        bridge.handle_frame(
            {
                "rpcId": "rpc-first-concurrent",
                "op": "install",
                "payload": {"identifier": "owner/repo/first-skill"},
            }
        )
    )
    second_task: asyncio.Task[None] | None = None

    try:
        assert await asyncio.to_thread(first_started.wait, 1)
        second_task = asyncio.create_task(
            bridge.handle_frame(
                {
                    "rpcId": "rpc-second-concurrent",
                    "op": "install",
                    "payload": {"identifier": "owner/repo/second-skill"},
                }
            )
        )
        await asyncio.sleep(0.05)
        assert not second_started.is_set()
    finally:
        release_first.set()
        tasks = [first_task]
        if second_task is not None:
            tasks.append(second_task)
        await asyncio.gather(*tasks)

    assert order == [
        "start:first-skill",
        "end:first-skill",
        "start:second-skill",
        "end:second-skill",
    ]


async def test_install_already_installed_does_not_call_installer(monkeypatch):
    state = _install_fake_machinery(
        monkeypatch,
        hub_records={"existing-skill": {"source": "github"}},
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-existing",
            "op": "install",
            "payload": {"identifier": "owner/repo/existing-skill"},
        }
    )

    assert state["install_calls"] == []
    assert [_post_kind(path) for path, _body in posts] == ["ack", "result"]
    assert _result_bodies(posts)[0]["error"]["code"] == "already_installed"


async def test_install_blocked_console_output_maps_to_scan_blocked(monkeypatch):
    def blocked(_identifier: str, console: Any, _name_override: str) -> None:
        console.print("BLOCKED: dangerous payload")

    _install_fake_machinery(monkeypatch, install_impl=blocked)
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-blocked",
            "op": "install",
            "payload": {"identifier": "owner/repo/bad-skill"},
        }
    )

    error = _result_bodies(posts)[0]["error"]
    assert error["code"] == "scan_blocked"
    assert "BLOCKED: dangerous" in error["message"]


async def test_install_could_not_fetch_console_output_maps_to_not_found(monkeypatch):
    def not_found(identifier: str, console: Any, _name_override: str) -> None:
        console.print(f"Could not fetch '{identifier}' from any source")

    _install_fake_machinery(monkeypatch, install_impl=not_found)
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-not-found",
            "op": "install",
            "payload": {"identifier": "owner/repo/missing-skill"},
        }
    )

    assert _result_bodies(posts)[0]["error"]["code"] == "not_found"


async def test_install_rejects_unrelated_new_lock_record(monkeypatch):
    records: dict[str, dict[str, Any]] = {}

    def install(identifier: str, _console: Any, _name_override: str) -> None:
        records["source-skill"] = {
            "identifier": "owner/repo/unrelated-skill",
            "source": "github",
        }

    _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        install_impl=install,
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-wrong-name",
            "op": "install",
            "payload": {
                "identifier": "owner/repo/source-skill",
                "name": "expected-name",
            },
        }
    )

    assert _result_bodies(posts)[0]["error"]["code"] == "install_failed"


async def test_ack_failure_does_not_skip_install(monkeypatch):
    records: dict[str, dict[str, Any]] = {}

    def install(identifier: str, _console: Any, _name_override: str) -> None:
        records["ack-skill"] = {"source": "github", "identifier": identifier}

    state = _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        install_impl=install,
    )
    bridge, posts = _make_bridge(monkeypatch)

    async def fail_ack(path: str, body: dict[str, Any]) -> None:
        posts.append((path, body))
        if path.endswith("/ack"):
            raise RuntimeError("ack unavailable")

    monkeypatch.setattr(bridge, "_post", fail_ack)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-ack-failure",
            "op": "install",
            "payload": {"identifier": "owner/repo/ack-skill"},
        }
    )

    assert len(state["install_calls"]) == 1
    assert _result_bodies(posts)[0] == {
        "ok": True,
        "payload": {"name": "ack-skill"},
    }


@pytest.mark.parametrize("failed_stage", ["starting", "installing", "verifying"])
async def test_progress_failure_is_advisory_for_install(monkeypatch, failed_stage):
    records: dict[str, dict[str, Any]] = {}

    def install(identifier: str, _console: Any, _name_override: str) -> None:
        records["progress-skill"] = {"source": "github", "identifier": identifier}

    _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        install_impl=install,
    )
    bridge, posts = _make_bridge(monkeypatch)

    async def fail_progress(path: str, body: dict[str, Any]) -> None:
        posts.append((path, body))
        if path.endswith("/progress") and body.get("stage") == failed_stage:
            raise RuntimeError("progress unavailable")

    monkeypatch.setattr(bridge, "_post", fail_progress)

    await bridge.handle_frame(
        {
            "rpcId": f"rpc-progress-{failed_stage}",
            "op": "install",
            "payload": {"identifier": "owner/repo/progress-skill"},
        }
    )

    assert _result_bodies(posts)[0] == {
        "ok": True,
        "payload": {"name": "progress-skill"},
    }


async def test_install_timeout_is_aligned_with_broker_cap():
    from hermes_channel_bgos import skills_bridge

    assert 290.0 <= skills_bridge._INSTALL_TIMEOUT_SECONDS < 300.0


async def test_import_error_reports_machinery_unavailable(monkeypatch):
    monkeypatch.delitem(sys.modules, "hermes_cli.skills_hub", raising=False)
    monkeypatch.delitem(sys.modules, "tools.skills_hub", raising=False)
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-no-machinery", "op": "list_installed", "payload": {}}
    )

    assert [_post_kind(path) for path, _body in posts] == ["ack", "result"]
    assert _result_bodies(posts)[0] == {
        "ok": False,
        "error": {
            "code": "install_failed",
            "message": "skills machinery unavailable on this host",
        },
    }


async def test_duplicate_rpc_id_is_dropped_after_first_frame(monkeypatch):
    bridge, posts = _make_bridge(monkeypatch)
    frame = {"rpcId": "rpc-duplicate", "op": "wat", "payload": {}}

    await bridge.handle_frame(frame)
    await bridge.handle_frame(frame)

    assert [_post_kind(path) for path, _body in posts] == ["ack", "result"]


async def test_inflight_rpc_is_not_evicted_by_recent_history_cap(monkeypatch):
    bridge, posts = _make_bridge(monkeypatch)
    first_ack_started = asyncio.Event()
    release_first_ack = asyncio.Event()
    first_ack_count = 0

    async def record_with_block(path: str, body: dict[str, Any]) -> None:
        nonlocal first_ack_count
        posts.append((path, body))
        if path.endswith("/rpc-inflight/ack"):
            first_ack_count += 1
            if first_ack_count == 1:
                first_ack_started.set()
                await release_first_ack.wait()

    monkeypatch.setattr(bridge, "_post", record_with_block)
    first_frame = {"rpcId": "rpc-inflight", "op": "wat", "payload": {}}
    first_task = asyncio.create_task(bridge.handle_frame(first_frame))
    await first_ack_started.wait()

    for index in range(256):
        await bridge.handle_frame(
            {"rpcId": f"rpc-later-{index}", "op": "wat", "payload": {}}
        )
    await bridge.handle_frame(first_frame)
    release_first_ack.set()
    await first_task

    assert first_ack_count == 1


async def test_result_post_retries_one_transport_error(monkeypatch):
    bridge, posts = _make_bridge(monkeypatch)
    result_attempts = 0

    async def flaky_result(path: str, body: dict[str, Any]) -> None:
        nonlocal result_attempts
        posts.append((path, body))
        if path.endswith("/result"):
            result_attempts += 1
            if result_attempts == 1:
                raise httpx.WriteError(
                    "connection reset",
                    request=httpx.Request("POST", "https://bgos.test/result"),
                )

    monkeypatch.setattr(bridge, "_post", flaky_result)

    await bridge.handle_frame(
        {"rpcId": "rpc-retry-result", "op": "wat", "payload": {}}
    )

    assert result_attempts == 2


async def test_result_post_retries_one_server_error(monkeypatch):
    bridge, posts = _make_bridge(monkeypatch)
    result_attempts = 0

    async def flaky_result(path: str, body: dict[str, Any]) -> None:
        nonlocal result_attempts
        posts.append((path, body))
        if path.endswith("/result"):
            result_attempts += 1
            if result_attempts == 1:
                request = httpx.Request("POST", "https://bgos.test/result")
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError(
                    "service unavailable",
                    request=request,
                    response=response,
                )

    monkeypatch.setattr(bridge, "_post", flaky_result)

    await bridge.handle_frame(
        {"rpcId": "rpc-retry-server", "op": "wat", "payload": {}}
    )

    assert result_attempts == 2


async def test_result_post_does_not_retry_client_error(monkeypatch):
    bridge, posts = _make_bridge(monkeypatch)
    result_attempts = 0

    async def rejected_result(path: str, body: dict[str, Any]) -> None:
        nonlocal result_attempts
        posts.append((path, body))
        if path.endswith("/result"):
            result_attempts += 1
            request = httpx.Request("POST", "https://bgos.test/result")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError(
                "conflict",
                request=request,
                response=response,
            )

    monkeypatch.setattr(bridge, "_post", rejected_result)

    await bridge.handle_frame(
        {"rpcId": "rpc-client-error", "op": "wat", "payload": {}}
    )

    assert result_attempts == 1


async def test_list_installed_ignores_stale_records_without_skill_dirs(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / ".bundled_manifest").write_text(
        "deleted-bundled:0123456789abcdef\n",
        encoding="utf-8",
    )
    _install_fake_machinery(
        monkeypatch,
        hub_records={"deleted-hub": {"source": "github"}},
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-stale-list", "op": "list_installed", "payload": {}}
    )

    assert _result_bodies(posts)[0]["payload"]["skills"] == []


async def test_list_installed_skips_dependency_and_skill_support_trees(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
    _write_skill(skills_dir, "parent-skill", "Real parent skill")
    for support_dir in ("references", "templates", "assets", "scripts"):
        _write_skill(
            skills_dir / "parent-skill" / support_dir,
            f"phantom-{support_dir}",
            "Nested support package",
        )
    for excluded_dir in (
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    ):
        _write_skill(
            skills_dir / excluded_dir,
            f"phantom-{excluded_dir.replace('.', 'dot')}",
            "Dependency package",
        )
    _write_skill(
        skills_dir / "references",
        "legit-category-skill",
        "A support-like category is valid here",
    )
    _install_fake_machinery(monkeypatch)
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-filter-list", "op": "list_installed", "payload": {}}
    )

    skills = _result_bodies(posts)[0]["payload"]["skills"]
    assert {item["name"] for item in skills} == {
        "parent-skill",
        "legit-category-skill",
    }


async def test_list_installed_combines_machinery_and_fallback_exclusions(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
    _write_skill(skills_dir, "kept-skill", "Visible skill")
    _write_skill(skills_dir, "machinery-hidden", "Hidden by machinery")
    _write_skill(skills_dir, "parent-skill", "Parent skill")
    _write_skill(
        skills_dir / "parent-skill" / "references",
        "fallback-hidden",
        "Hidden by the fallback",
    )
    calls: list[Path] = []

    def legacy_excluder(path: Path) -> bool:
        calls.append(path)
        return "machinery-hidden" in path.parts

    _install_fake_machinery(monkeypatch)
    tools_module = sys.modules["tools.skills_hub"]
    tools_module.is_excluded_skill_path = legacy_excluder  # type: ignore[attr-defined]
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-combined-filter", "op": "list_installed", "payload": {}}
    )

    skills = _result_bodies(posts)[0]["payload"]["skills"]
    assert {item["name"] for item in skills} == {"kept-skill", "parent-skill"}
    assert len(calls) == 4


async def test_list_and_remove_use_canonical_lock_key_for_display_name(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
    _write_skill(
        skills_dir,
        "canonical-slug",
        "Friendly description",
        frontmatter_name="Friendly Display",
    )
    records: dict[str, dict[str, Any]] = {
        "canonical-slug": {"source": "github"},
    }

    def uninstall(name: str, _console: Any) -> None:
        records.pop(name, None)

    state = _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        uninstall_impl=uninstall,
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {"rpcId": "rpc-canonical-list", "op": "list_installed", "payload": {}}
    )
    await bridge.handle_frame(
        {
            "rpcId": "rpc-canonical-remove",
            "op": "remove",
            "payload": {"name": "Friendly Display"},
        }
    )

    listed = _result_bodies(posts)[0]["payload"]["skills"]
    assert listed == [
        {
            "name": "Friendly Display",
            "identifier": "canonical-slug",
            "description": "Friendly description",
            "provenance": "hub",
            "removable": True,
            "source": "github",
        }
    ]
    assert state["uninstall_calls"][0]["name"] == "canonical-slug"
    assert _result_bodies(posts)[1] == {"ok": True, "payload": {}}


async def test_remove_prefers_identifier_over_display_name(monkeypatch):
    records: dict[str, dict[str, Any]] = {
        "canonical-slug": {"source": "official"},
    }

    def uninstall(name: str, _console: Any) -> None:
        records.pop(name, None)

    state = _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        uninstall_impl=uninstall,
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-identifier-remove",
            "op": "remove",
            "payload": {
                "identifier": "canonical-slug",
                "name": "Wrong Display Name",
            },
        }
    )

    assert state["uninstall_calls"][0]["name"] == "canonical-slug"
    assert _result_bodies(posts)[0] == {"ok": True, "payload": {}}


async def test_remove_display_name_wins_over_another_skill_lock_key(monkeypatch):
    skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
    _write_skill(
        skills_dir,
        "first-key",
        "First skill",
        frontmatter_name="second-key",
    )
    _write_skill(
        skills_dir,
        "second-key",
        "Second skill",
        frontmatter_name="Second Display",
    )
    records: dict[str, dict[str, Any]] = {
        "first-key": {"source": "github"},
        "second-key": {"source": "github"},
    }

    def uninstall(name: str, _console: Any) -> None:
        records.pop(name, None)

    state = _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        uninstall_impl=uninstall,
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-colliding-display",
            "op": "remove",
            "payload": {"name": "second-key"},
        }
    )

    assert state["uninstall_calls"][0]["name"] == "first-key"
    assert "second-key" in records
    assert _result_bodies(posts)[0] == {"ok": True, "payload": {}}


async def test_remove_refuses_non_hub_and_removes_hub_skill(monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    skills_dir = home / "skills"
    _write_skill(skills_dir, "local-skill", "Local only")
    records: dict[str, dict[str, Any]] = {
        "hub-skill": {"source": "github"},
    }

    def uninstall(name: str, _console: Any) -> None:
        records.pop(name, None)

    state = _install_fake_machinery(
        monkeypatch,
        hub_records=records,
        uninstall_impl=uninstall,
    )
    bridge, posts = _make_bridge(monkeypatch)

    await bridge.handle_frame(
        {
            "rpcId": "rpc-remove-local",
            "op": "remove",
            "payload": {"name": "local-skill"},
        }
    )
    await bridge.handle_frame(
        {
            "rpcId": "rpc-remove-hub",
            "op": "remove",
            "payload": {"name": "hub-skill"},
        }
    )

    results = _result_bodies(posts)
    assert results[0] == {
        "ok": False,
        "error": {
            "code": "install_failed",
            "message": "only store-installed skills can be removed here",
        },
    }
    assert len(state["uninstall_calls"]) == 1
    assert state["uninstall_calls"][0]["name"] == "hub-skill"
    assert state["uninstall_calls"][0]["skip_confirm"] is True
    assert results[1] == {"ok": True, "payload": {}}
