"""Bridge BGOS Skills Store RPC frames to the local Hermes skills machinery."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import httpx

from .config import BgosConfig


log = logging.getLogger(__name__)

_RPC_ROOT = "/api/v1/integrations/skills-rpc"
_HTTP_TIMEOUT_SECONDS = 10.0
_CATALOG_TIMEOUT_SECONDS = 15.0
_INSTALL_TIMEOUT_SECONDS = 240.0
_SEEN_RPC_CAP = 256
_SUPPORTED_OPS = {"list_installed", "catalog", "install", "remove"}
_MACHINERY_UNAVAILABLE = "skills machinery unavailable on this host"


@dataclass(frozen=True)
class _Machinery:
    do_install: Callable[..., Any]
    do_uninstall: Callable[..., Any]
    browse_skills: Callable[..., Any]
    inspect_skill: Callable[..., Any]
    unified_search: Callable[..., Any]
    hub_lock_file: type
    tools_module: ModuleType


@dataclass(frozen=True)
class _LocalSkill:
    directory_name: str
    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class _InstalledSnapshot:
    hub_records: dict[str, dict[str, Any]]
    bundled_names: set[str]
    local_names: set[str]

    @property
    def names(self) -> set[str]:
        return set(self.hub_records) | self.bundled_names | self.local_names


class _CaptureConsole:
    """Small Rich Console substitute used by non-interactive calls."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, *args: Any, **_kwargs: Any) -> None:
        self.messages.append(" ".join(str(arg) for arg in args))

    def lines(self) -> list[str]:
        lines: list[str] = []
        for message in self.messages:
            lines.extend(line.strip() for line in message.splitlines() if line.strip())
        return lines


def _load_machinery() -> _Machinery:
    """Import optional Hermes modules only when a supported frame arrives."""
    try:
        cli_module = importlib.import_module("hermes_cli.skills_hub")
        tools_module = importlib.import_module("tools.skills_hub")
        lock_type = getattr(cli_module, "HubLockFile", None)
        if lock_type is None:
            lock_type = getattr(tools_module, "HubLockFile")
        return _Machinery(
            do_install=getattr(cli_module, "do_install"),
            do_uninstall=getattr(cli_module, "do_uninstall"),
            browse_skills=getattr(cli_module, "browse_skills"),
            inspect_skill=getattr(cli_module, "inspect_skill"),
            unified_search=getattr(tools_module, "unified_search"),
            hub_lock_file=lock_type,
            tools_module=tools_module,
        )
    except (ImportError, AttributeError) as exc:
        raise ImportError(_MACHINERY_UNAVAILABLE) from exc


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"


def _skills_dir() -> Path:
    return _hermes_home() / "skills"


def _mapping_records(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, Mapping):
        installed = raw.get("installed")
        if isinstance(installed, Mapping):
            raw = installed
        records: dict[str, dict[str, Any]] = {}
        for raw_name, raw_record in raw.items():
            name = str(raw_name).strip()
            if not name:
                continue
            record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
            record.setdefault("name", name)
            records[name] = record
        return records

    records = {}
    if isinstance(raw, (list, tuple)):
        for raw_record in raw:
            if not isinstance(raw_record, Mapping):
                continue
            record = dict(raw_record)
            name = str(record.get("name") or "").strip()
            if name:
                records[name] = record
    return records


def _hub_records(lock_type: type) -> dict[str, dict[str, Any]]:
    lock = lock_type()
    records: dict[str, dict[str, Any]] = {}
    list_installed = getattr(lock, "list_installed", None)
    if callable(list_installed):
        records = _mapping_records(list_installed())
    if not records:
        load = getattr(lock, "load", None)
        if callable(load):
            records = _mapping_records(load())
    return records


def _hub_record(lock_type: type, name: str) -> dict[str, Any] | None:
    lock = lock_type()
    get_installed = getattr(lock, "get_installed", None)
    if callable(get_installed):
        record = get_installed(name)
        if record is not None:
            return dict(record) if isinstance(record, Mapping) else {"name": name}
        return None
    return _hub_records(lock_type).get(name)


def _read_bundled_manifest(skills_dir: Path) -> set[str]:
    manifest = skills_dir / ".bundled_manifest"
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()

    stripped = text.strip()
    if not stripped:
        return set()

    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        decoded = None

    if isinstance(decoded, Mapping):
        contents: Any = decoded.get("skills", decoded)
        if isinstance(contents, Mapping):
            return {str(name).strip() for name in contents if str(name).strip()}
        if isinstance(contents, list):
            return _manifest_list_names(contents)
    if isinstance(decoded, list):
        return _manifest_list_names(decoded)

    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(":", 1)[0].strip()
        if name:
            names.add(name)
    return names


def _manifest_list_names(items: list[Any]) -> set[str]:
    names: set[str] = set()
    for item in items:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
        else:
            name = ""
        if name:
            names.add(name)
    return names


def _frontmatter_block(content: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def _minimal_frontmatter(block: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        if raw_line.startswith((" ", "\t")) or ":" not in raw_line:
            index += 1
            continue
        raw_key, raw_value = raw_line.split(":", 1)
        key = raw_key.strip()
        if key not in {"name", "description"}:
            index += 1
            continue
        value = raw_value.strip()
        if key == "description" and value in {">", ">-", ">+", "|", "|-", "|+"}:
            continuation: list[str] = []
            index += 1
            while index < len(lines) and (
                lines[index].startswith((" ", "\t")) or not lines[index].strip()
            ):
                continuation.append(lines[index].strip())
                index += 1
            value = " ".join(part for part in continuation if part)
        else:
            index += 1
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _parse_skill(skill_md: Path) -> tuple[str, str]:
    directory_name = skill_md.parent.name
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return directory_name, ""
    block = _frontmatter_block(content)
    parsed: Mapping[str, Any] = {}
    if block:
        try:
            import yaml
        except ImportError:
            parsed = _minimal_frontmatter(block)
        else:
            try:
                loaded = yaml.safe_load(block)
            except Exception:
                parsed = _minimal_frontmatter(block)
            else:
                parsed = loaded if isinstance(loaded, Mapping) else {}

    name = str(parsed.get("name") or directory_name).strip() or directory_name
    description = str(parsed.get("description") or "").strip()[:300]
    return name, description


def _local_skills(skills_dir: Path) -> list[_LocalSkill]:
    if not skills_dir.is_dir():
        return []
    rows: list[_LocalSkill] = []
    try:
        skill_files = sorted(skills_dir.rglob("SKILL.md"))
    except OSError:
        return []
    for skill_md in skill_files:
        try:
            relative = skill_md.relative_to(skills_dir)
        except ValueError:
            continue
        if any(part.startswith(".") for part in relative.parts[:-1]):
            continue
        name, description = _parse_skill(skill_md)
        rows.append(
            _LocalSkill(
                directory_name=skill_md.parent.name,
                name=name,
                description=description,
                path=skill_md.parent,
            )
        )
    return rows


def _installed_snapshot(lock_type: type) -> _InstalledSnapshot:
    skills_dir = _skills_dir()
    local_names: set[str] = set()
    for local in _local_skills(skills_dir):
        local_names.add(local.name)
        local_names.add(local.directory_name)
    return _InstalledSnapshot(
        hub_records=_hub_records(lock_type),
        bundled_names=_read_bundled_manifest(skills_dir),
        local_names=local_names,
    )


def _record_value(record: Mapping[str, Any], *keys: str) -> Any:
    metadata = record.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
        value = metadata_map.get(key)
        if value is not None and value != "":
            return value
    return None


def _as_wire_string(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _add_hub_fields(item: dict[str, Any], record: Mapping[str, Any]) -> None:
    field_aliases = {
        "source": ("source",),
        "publisher": ("publisher", "provider"),
        "category": ("category",),
        "version": ("version",),
        "trust": ("trust", "trust_level"),
        "installedAt": ("installedAt", "installed_at"),
    }
    for wire_name, aliases in field_aliases.items():
        value = _record_value(record, *aliases)
        if value is not None:
            item[wire_name] = _as_wire_string(value)


def _learned_at(path: Path) -> str | None:
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(modified, tz=timezone.utc).isoformat()


def _list_installed(lock_type: type) -> list[dict[str, Any]]:
    skills_dir = _skills_dir()
    hub_records = _hub_records(lock_type)
    bundled_names = _read_bundled_manifest(skills_dir)
    local_skills = _local_skills(skills_dir)
    items: dict[str, dict[str, Any]] = {}

    for local in local_skills:
        hub_name = next(
            (name for name in (local.name, local.directory_name) if name in hub_records),
            None,
        )
        bundled_name = next(
            (name for name in (local.name, local.directory_name) if name in bundled_names),
            None,
        )
        if hub_name is not None:
            provenance = "hub"
        elif bundled_name is not None:
            provenance = "bundled"
        else:
            provenance = "self_taught"

        item: dict[str, Any] = {
            "name": local.name,
            "description": local.description,
            "provenance": provenance,
            "removable": provenance == "hub",
        }
        if hub_name is not None:
            _add_hub_fields(item, hub_records[hub_name])
        elif provenance == "self_taught":
            learned_at = _learned_at(local.path)
            if learned_at is not None:
                item["learnedAt"] = learned_at
        items[local.name] = item

    return sorted(items.values(), key=lambda item: item["name"].casefold())


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _item_extra(item: Any) -> Mapping[str, Any]:
    extra = _item_value(item, "extra")
    return extra if isinstance(extra, Mapping) else {}


def _optional_item_value(item: Any, *keys: str) -> Any:
    extra = _item_extra(item)
    for key in keys:
        value = _item_value(item, key)
        if value is not None and value != "":
            return value
        value = extra.get(key)
        if value is not None and value != "":
            return value
    return None


def _catalog_item(item: Any, installed_names: set[str]) -> dict[str, Any]:
    name = str(_item_value(item, "name") or "")
    identifier = str(_item_value(item, "identifier") or name)
    mapped: dict[str, Any] = {
        "identifier": identifier,
        "name": name,
        "description": str(_item_value(item, "description") or ""),
        "source": str(_item_value(item, "source") or "hub"),
        "installed": name in installed_names,
    }
    publisher = _optional_item_value(item, "publisher", "provider")
    category = _optional_item_value(item, "category")
    trust = _optional_item_value(item, "trust", "trust_level")
    if publisher is not None:
        mapped["publisher"] = str(publisher)
    if category is not None:
        mapped["category"] = str(category)
    if trust is not None:
        mapped["trust"] = str(trust)
    return mapped


def _coerce_page(value: Any, default: int = 1) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return default
    return page if page > 0 else default


def _catalog_payload(raw: Any, page: int, installed_names: set[str]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        raw_items = raw.get("items")
        items = raw_items if isinstance(raw_items, (list, tuple)) else []
        wire_page = _coerce_page(raw.get("page"), page)
    elif isinstance(raw, (list, tuple)):
        items = raw
        wire_page = page
    else:
        items = []
        wire_page = page

    payload: dict[str, Any] = {
        "items": [_catalog_item(item, installed_names) for item in items],
        "page": wire_page,
    }
    if isinstance(raw, Mapping):
        total_pages = raw.get("totalPages", raw.get("total_pages"))
        total = raw.get("total")
        if total_pages is not None:
            payload["totalPages"] = total_pages
        if total is not None:
            payload["total"] = total
    return payload


def _search_catalog(machinery: _Machinery, query: str) -> Any:
    search = machinery.unified_search
    try:
        signature = inspect.signature(search)
    except (TypeError, ValueError):
        signature = None
    sources = signature.parameters.get("sources") if signature is not None else None
    if sources is not None and sources.default is inspect.Parameter.empty:
        create_source_router = getattr(machinery.tools_module, "create_source_router", None)
        if not callable(create_source_router):
            raise RuntimeError("skills source router is unavailable on this host")
        return search(query=query, sources=create_source_router())
    return search(query=query)


def _fetch_catalog(machinery: _Machinery, query: str | None, page: int) -> Any:
    if query is not None:
        return _search_catalog(machinery, query)
    return machinery.browse_skills(page=page, page_size=30)


def _expected_skill_name(identifier: str, name_override: str) -> str:
    if name_override:
        return name_override
    return identifier.rstrip("/").rsplit("/", 1)[-1].strip()


def _snapshot_has_skill(
    snapshot: _InstalledSnapshot,
    expected_name: str,
    identifier: str,
) -> bool:
    if expected_name in snapshot.names:
        return True
    for record in snapshot.hub_records.values():
        if str(record.get("identifier") or "") == identifier:
            return True
    return False


def _verify_install(lock_type: type, expected_name: str) -> bool:
    if _hub_record(lock_type, expected_name) is not None:
        return True
    snapshot = _installed_snapshot(lock_type)
    return expected_name in snapshot.local_names


def _short_message(value: str, fallback: str) -> str:
    compact = " ".join(value.split()).strip()
    return (compact or fallback)[:300]


def _install_failure(capture: _CaptureConsole) -> dict[str, Any]:
    lines = capture.lines()
    for line in reversed(lines):
        lowered = line.casefold()
        if "blocked" in lowered or "dangerous" in lowered:
            return _error("scan_blocked", _short_message(line, "skill scan blocked installation"))
    for line in reversed(lines):
        lowered = line.casefold()
        if (
            "not found" in lowered
            or "no skill" in lowered
            or "could not fetch" in lowered
        ):
            return _error("not_found", _short_message(line, "skill was not found"))
    for line in reversed(lines):
        if "already installed" in line.casefold():
            return _error(
                "already_installed",
                _short_message(line, "skill is already installed"),
            )
    detail = " | ".join(lines[-3:])
    return _error(
        "install_failed",
        _short_message(detail, "skill installation could not be verified"),
    )


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "payload": payload}


def _exception_message(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return _short_message(
            f"skills operation failed on the agent host: {detail}",
            "skills operation failed on the agent host",
        )
    return "skills operation failed on the agent host"


class SkillsBridge:
    """Handle Skills Store RPC frames for one BGOS pairing."""

    def __init__(self, config: BgosConfig) -> None:
        self._config = config
        self._inflight_rpc_ids: set[str] = set()
        self._recent_rpc_ids: set[str] = set()
        self._recent_rpc_order: deque[str] = deque()

    async def handle_frame(self, frame: dict) -> None:
        if not isinstance(frame, dict):
            log.warning("skills_bridge dropped non-object frame")
            return
        rpc_id = frame.get("rpcId")
        op = frame.get("op")
        if not isinstance(rpc_id, str) or not rpc_id.strip():
            log.warning("skills_bridge dropped frame without rpcId")
            return
        if not isinstance(op, str) or not op.strip():
            log.warning("skills_bridge dropped frame without op rpc=%s", rpc_id)
            return
        if not self._claim_rpc(rpc_id):
            log.info("skills_bridge duplicate frame ignored rpc=%s", rpc_id)
            return

        try:
            try:
                await self._post(f"{_RPC_ROOT}/{rpc_id}/ack", {})
                if op not in _SUPPORTED_OPS:
                    result = _error("install_failed", "unsupported op")
                else:
                    machinery = await asyncio.to_thread(_load_machinery)
                    payload = frame.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}
                    result = await self._dispatch(op, rpc_id, payload, machinery)
            except ImportError:
                result = _error("install_failed", _MACHINERY_UNAVAILABLE)
            except Exception as exc:
                log.exception(
                    "skills_bridge operation failed rpc=%s op=%s",
                    rpc_id,
                    op,
                )
                result = _error("install_failed", _exception_message(exc))

            await self._post_result(rpc_id, result)
        finally:
            self._complete_rpc(rpc_id)

    def _claim_rpc(self, rpc_id: str) -> bool:
        if rpc_id in self._inflight_rpc_ids or rpc_id in self._recent_rpc_ids:
            return False
        self._inflight_rpc_ids.add(rpc_id)
        return True

    def _complete_rpc(self, rpc_id: str) -> None:
        self._inflight_rpc_ids.discard(rpc_id)
        if rpc_id in self._recent_rpc_ids:
            return
        if len(self._recent_rpc_order) >= _SEEN_RPC_CAP:
            oldest = self._recent_rpc_order.popleft()
            self._recent_rpc_ids.discard(oldest)
        self._recent_rpc_order.append(rpc_id)
        self._recent_rpc_ids.add(rpc_id)

    async def _dispatch(
        self,
        op: str,
        rpc_id: str,
        payload: dict[str, Any],
        machinery: _Machinery,
    ) -> dict[str, Any]:
        if op == "list_installed":
            skills = await asyncio.to_thread(_list_installed, machinery.hub_lock_file)
            return _ok({"skills": skills})
        if op == "catalog":
            return await self._handle_catalog(payload, machinery)
        if op == "install":
            return await self._handle_install(rpc_id, payload, machinery)
        return await self._handle_remove(payload, machinery)

    async def _handle_catalog(
        self,
        payload: dict[str, Any],
        machinery: _Machinery,
    ) -> dict[str, Any]:
        raw_query = payload.get("query")
        query = (
            raw_query.strip()
            if "query" in payload and isinstance(raw_query, str)
            else None
        )
        page = _coerce_page(payload.get("page"), 1)
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_fetch_catalog, machinery, query, page),
                timeout=_CATALOG_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return _error(
                "install_failed",
                "catalog fetch timed out on the agent host",
            )
        snapshot = await asyncio.to_thread(_installed_snapshot, machinery.hub_lock_file)
        return _ok(_catalog_payload(raw, page, snapshot.names))

    async def _handle_install(
        self,
        rpc_id: str,
        payload: dict[str, Any],
        machinery: _Machinery,
    ) -> dict[str, Any]:
        raw_identifier = payload.get("identifier")
        if not isinstance(raw_identifier, str) or not raw_identifier.strip():
            return _error("not_found", "skill identifier is required")
        identifier = raw_identifier.strip()
        raw_name = payload.get("name")
        name_override = raw_name.strip() if isinstance(raw_name, str) else ""
        expected_name = _expected_skill_name(identifier, name_override)
        if not expected_name:
            return _error("not_found", "skill name could not be determined")

        snapshot = await asyncio.to_thread(_installed_snapshot, machinery.hub_lock_file)
        if _snapshot_has_skill(snapshot, expected_name, identifier):
            return _error("already_installed", "skill is already installed")

        await self._post_progress(rpc_id, "starting")
        capture = _CaptureConsole()
        await self._post_progress(rpc_id, "installing")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    machinery.do_install,
                    identifier,
                    skip_confirm=True,
                    console=capture,
                    name_override=name_override,
                ),
                timeout=_INSTALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return _error(
                "install_failed",
                "skill installation timed out on the agent host",
            )

        await self._post_progress(rpc_id, "verifying")
        verified = await asyncio.to_thread(
            _verify_install,
            machinery.hub_lock_file,
            expected_name,
        )
        if verified:
            return _ok({"name": expected_name})
        return _install_failure(capture)

    async def _handle_remove(
        self,
        payload: dict[str, Any],
        machinery: _Machinery,
    ) -> dict[str, Any]:
        raw_name = payload.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return _error("install_failed", "skill name is required")
        name = raw_name.strip()
        installed = await asyncio.to_thread(_hub_record, machinery.hub_lock_file, name)
        if installed is None:
            return _error(
                "install_failed",
                "only store-installed skills can be removed here",
            )

        capture = _CaptureConsole()
        await asyncio.to_thread(
            machinery.do_uninstall,
            name,
            skip_confirm=True,
            console=capture,
        )
        remaining = await asyncio.to_thread(_hub_record, machinery.hub_lock_file, name)
        if remaining is not None:
            detail = " | ".join(capture.lines()[-3:])
            return _error(
                "install_failed",
                _short_message(detail, "skill removal could not be verified"),
            )
        return _ok({})

    async def _post_progress(
        self,
        rpc_id: str,
        stage: str,
        detail: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"stage": stage}
        if detail:
            body["detail"] = detail
        await self._post(f"{_RPC_ROOT}/{rpc_id}/progress", body)

    async def _post_result(self, rpc_id: str, body: dict[str, Any]) -> None:
        path = f"{_RPC_ROOT}/{rpc_id}/result"
        for attempt in range(2):
            try:
                await self._post(path, body)
                return
            except (httpx.TransportError, ConnectionError):
                if attempt == 0:
                    log.warning(
                        "skills_bridge result connection failed, retrying rpc=%s",
                        rpc_id,
                    )
                    continue
                log.exception("skills_bridge result post failed after retry rpc=%s", rpc_id)
                return
            except Exception:
                log.exception("skills_bridge result post failed rpc=%s", rpc_id)
                return

    async def _post(self, path: str, body: dict[str, Any]) -> None:
        if not self._config.pairing_token:
            raise RuntimeError("pairing token required for skills RPC endpoint")
        headers = {
            "Content-Type": "application/json",
            "X-BGOS-Pairing": self._config.pairing_token,
        }
        async with httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=_HTTP_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(path, json=body, headers=headers)
            response.raise_for_status()
