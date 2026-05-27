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
