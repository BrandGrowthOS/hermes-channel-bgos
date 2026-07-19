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
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from . import __version__
from .agents import enumerate_agents_from_env
from .bgos_api import BgosApi, BgosApiError
from .config import (
    PAIRING_TOKEN_PREFIX, BgosConfig, TokenChoice, choose_pairing_token,
    looks_like_pairing_token, normalize_base_url, redact_token,
)

OK, WARN, FAIL = "OK", "WARN", "FAIL"

_ENV_LINE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$",
)


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    fix: str = ""


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, optional `export `, optional quotes).

    Intentionally forgiving - a malformed line is skipped, never fatal. The
    doctor only needs the handful of BGOS_* keys, not full dotenv semantics.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _hermes_install_candidates() -> list[Path]:
    """Mirror install.sh's Hermes-install search order (for the legacy
    project-.env fallback below). An explicit $HERMES_INSTALL wins outright,
    exactly as it does for install.sh."""
    override = os.environ.get("HERMES_INSTALL", "").strip()
    if override:
        return [Path(override)]
    return [
        Path.home() / ".hermes" / "hermes-agent",
        Path.home() / "hermes-agent",
        Path("/opt/hermes-agent"),
        Path("/opt/hermes/hermes-agent"),
    ]


def gateway_env() -> tuple[dict[str, str], Path | None]:
    """The environment as the RUNNING GATEWAY sees it, not just this process.

    The Hermes gateway loads `$HERMES_HOME/.env` into os.environ itself at
    startup with override=True (gateway/run.py → hermes_cli/env_loader.py
    `load_hermes_dotenv`), and the hermes-agent project `.env` as a fill-in
    fallback. A doctor that reads only its own os.environ therefore
    false-alarms "unset" for vars the gateway definitely has (the launchd
    plist / login shell rarely export BGOS_*). Mirror the gateway's order:

    1. this process's environ (a deliberate shell export still shows up)
    2. `$HERMES_HOME/.env` overrides it (same override=True the gateway uses)
    3. a `<hermes-install>/.env` fills remaining gaps (legacy installs where
       install.sh wrote the project env / systemd EnvironmentFile)

    Returns (effective_env, env_file_used_or_None).
    """
    env: dict[str, str] = dict(os.environ)
    used: Path | None = None
    user_env = _hermes_home() / ".env"
    if user_env.is_file():
        env.update(_parse_env_file(user_env))
        used = user_env
    else:
        for install in _hermes_install_candidates():
            project_env = install / ".env"
            if project_env.is_file():
                for key, value in _parse_env_file(project_env).items():
                    env.setdefault(key, value)
                used = project_env
                break
    return env, used


def check_package() -> CheckResult:
    return CheckResult("package", OK, f"hermes_channel_bgos {__version__}")


def _plugin_link_status() -> tuple[bool, str]:
    """Inspect the installed plugin dir/symlink at `$HERMES_HOME/plugins/bgos`.

    Returns (valid, human detail). Valid means: the path exists, resolves,
    and the target carries the three files a Hermes directory plugin needs
    (plugin.yaml manifest, __init__.py with register(), adapter.py shim).
    """
    link = _hermes_home() / "plugins" / "bgos"
    if not link.is_symlink() and not link.exists():
        return False, f"no plugin at {link}"
    if not link.is_dir():
        try:
            target = os.readlink(link)
        except OSError:
            target = "?"
        return False, f"{link} is a broken symlink (target {target} missing)"
    resolved = link.resolve()
    required = ("plugin.yaml", "__init__.py", "adapter.py")
    missing = [name for name in required if not (link / name).is_file()]
    if missing:
        return False, f"{link} -> {resolved} is missing {', '.join(missing)}"
    return True, f"{link} -> {resolved} (plugin.yaml + register shim present)"


def check_registration() -> CheckResult:
    """Is BGOS registered with Hermes — via the plugin system OR the fork patch?

    HONESTY NOTE: the doctor runs in its own process; the authoritative
    platform registry lives inside the RUNNING gateway process and cannot be
    inspected from here. So a failed in-process discovery probe is not proof
    of a broken install. When the probe fails but the installed plugin
    symlink is valid and this package imports, the install is considered OK -
    the gateway registers the plugin itself at startup. Real FAILs are
    reserved for: gateway modules not importable from this Python, or no
    valid plugin/patch present at all.
    """
    link_ok, link_detail = _plugin_link_status()
    try:
        from gateway.config import Platform  # type: ignore
    except Exception as exc:
        return CheckResult(
            "registration", FAIL,
            f"Hermes gateway not importable ({exc.__class__.__name__}); "
            f"plugin files: {link_detail}",
            fix="Run this from Hermes's Python env. Then either install the BGOS "
                "plugin (symlink plugins/platforms/bgos into ~/.hermes/plugins/) "
                "or apply the fork patch.",
        )
    platform = getattr(Platform, "BGOS", None)
    via = "patch"
    probe_error: str | None = None
    if platform is None:
        try:
            from hermes_cli.plugins import discover_plugins  # type: ignore
            discover_plugins(force=True)
            platform = Platform("bgos")
            via = "plugin"
        except Exception as exc:
            probe_error = exc.__class__.__name__
            platform = None
    if platform is not None:
        # Distinguish plugin vs patch: the registry records plugin entries.
        try:
            from gateway.platform_registry import platform_registry  # type: ignore
            via = "plugin" if platform_registry.is_registered("bgos") else via
        except Exception:
            pass
        return CheckResult(
            "registration", OK, f"Platform.BGOS registered (via {via})",
        )
    if link_ok:
        return CheckResult(
            "registration", OK,
            f"plugin installed: {link_detail}; in-process probe unavailable "
            f"({probe_error}) - the gateway registers it itself at startup",
            fix="",
        )
    return CheckResult(
        "registration", FAIL,
        f"Platform.BGOS not registered and {link_detail}",
        fix="Enable BGOS: plugin path - symlink plugins/platforms/bgos into "
            "~/.hermes/plugins/bgos and restart; or legacy - apply "
            "hermes-fork-patch/0001-bgos-integration.patch.",
    )


def _read_secrets(sp: Path) -> dict:
    if not sp.is_file():
        return {}
    try:
        return _json.loads(sp.read_text())
    except (OSError, ValueError):
        return {}


def _resolve_token_state() -> tuple[TokenChoice, Path, dict[str, str]]:
    """Resolve the effective token the same way the adapter does, keeping the
    provenance. Returns (choice, secrets_path, gateway_effective_env)."""
    from .pair_cli import secrets_path
    sp = secrets_path()
    secrets = _read_secrets(sp)
    env, _ = gateway_env()
    choice = choose_pairing_token(
        env.get("BGOS_API_KEY"), secrets.get("pairing_token"),
    )
    return choice, sp, env


def token_source_label(choice: TokenChoice, sp: Path) -> str:
    """Human-readable provenance of the token in use."""
    if choice.source == "env":
        return "env $BGOS_API_KEY"
    if choice.source == "secrets":
        return f"secrets file {sp}"
    return "nowhere"


def check_token_hygiene() -> CheckResult | None:
    """Token-source honesty check. Emits a finding when the token situation
    can silently break auth; returns None when everything is normal.

    The incident this guards: a stale BGOS_API_KEY env export holding a BGOS
    USER api key (not a pair_ pairing token) shadowed a freshly paired
    secrets-file token, whoami 401 forever, and the doctor said nothing.
    """
    choice, sp, _ = _resolve_token_state()
    if choice.ignored_env_token is not None:
        return CheckResult(
            "token_source", WARN,
            f"$BGOS_API_KEY is set to {redact_token(choice.ignored_env_token)} "
            f"which does not look like a pairing token (no {PAIRING_TOKEN_PREFIX} "
            f"prefix); it would shadow the real pairing_token in {sp}. "
            f"The gateway and this doctor now IGNORE it and use the secrets token.",
            fix="Remove the stale export: unset BGOS_API_KEY (check your shell "
                "profile and $HERMES_HOME/.env), then restart the gateway. "
                "If whoami still fails afterwards, re-pair: "
                "hermes-pair-bgos <CODE> --device-label <host>",
        )
    if choice.token and not looks_like_pairing_token(choice.token):
        env_note = (
            f" and no secrets-file pairing_token exists at {sp}"
            if choice.source == "env" else ""
        )
        return CheckResult(
            "token_source", WARN,
            f"token {redact_token(choice.token)} from "
            f"{token_source_label(choice, sp)} does not look like a pairing "
            f"token (no {PAIRING_TOKEN_PREFIX} prefix){env_note}.",
            fix="If whoami fails with 401, unset BGOS_API_KEY, restart the "
                "gateway, and re-pair: hermes-pair-bgos <CODE> "
                "--device-label <host>",
        )
    return None


def check_config() -> tuple[BgosConfig | None, CheckResult]:
    """Resolve the pairing config from env + the secrets file. Returns the
    config (or None) plus a CheckResult. A missing token is the canonical
    'not paired' state and yields the get-a-code instruction.

    Mirrors the adapter's env+secrets precedence (pair_ shaped `BGOS_API_KEY`
    wins; a non-pairing env value yields to the secrets-file token;
    `BGOS_BACKEND_URL` → secrets-file base_url → prod
    default) — the subset that applies when running standalone, where there's
    no Hermes config object to read. Deliberately does NOT import the adapter
    module: it pulls in Hermes/mock shims that aren't importable outside the
    gateway runtime or the test harness.
    """
    choice, sp, env = _resolve_token_state()
    secrets = _read_secrets(sp)
    raw_base_url = (
        env.get("BGOS_BACKEND_URL")
        or secrets.get("base_url")
        or "https://api.brandgrowthos.ai"
    )
    base_url = normalize_base_url(raw_base_url)
    base_note = "" if base_url == raw_base_url else f" (normalized from {raw_base_url})"
    if not choice.token:
        return None, CheckResult(
            "config", FAIL,
            f"no pairing token found (checked $BGOS_API_KEY and {sp})",
            fix="Not paired. In BGOS open Integrations → Hermes → 'Connect a "
                "new Hermes server', copy the BGOS-XXXX-XX code, then run: "
                "hermes-pair-bgos <CODE> --device-label <host>",
        )
    return BgosConfig(base_url=base_url, pairing_token=choice.token), CheckResult(
        "config", OK,
        f"token {redact_token(choice.token, 6)} from {token_source_label(choice, sp)}; "
        f"base_url={base_url}{base_note}; "
        f"secrets={sp} (exists={sp.exists()})",
    )


def check_env() -> CheckResult:
    env, env_file = gateway_env()
    src = f" (sources: process env + {env_file})" if env_file else ""
    if env.get("BGOS_ALLOW_ALL_USERS", "").lower() == "true":
        return CheckResult("auth", OK, f"BGOS_ALLOW_ALL_USERS=true{src}")
    allowed = env.get("BGOS_ALLOWED_USERS", "").strip()
    if allowed:
        n = len([u for u in allowed.split(",") if u.strip()])
        return CheckResult("auth", OK, f"BGOS_ALLOWED_USERS set ({n} user(s)){src}")
    return CheckResult(
        "auth", WARN,
        f"neither BGOS_ALLOW_ALL_USERS nor BGOS_ALLOWED_USERS set{src}",
        fix="Set BGOS_ALLOW_ALL_USERS=true (or BGOS_ALLOWED_USERS=<clerk id>) "
            "or inbound messages are dropped by the auth gate.",
    )


def check_catalog() -> CheckResult:
    env, env_file = gateway_env()
    src = f" (sources: process env + {env_file})" if env_file else ""
    agents = enumerate_agents_from_env(env)
    if not agents:
        return CheckResult(
            "catalog", WARN, f"no agents configured (BGOS_AGENTS unset){src}",
            fix="Set BGOS_AGENTS=route:Name (e.g. default:David) so the "
                "Integrations UI can offer agents to expose.",
        )
    listing = ", ".join(
        f"{a['agent_route']}:{a.get('name', a['agent_route'])}" for a in agents
    )
    return CheckResult("catalog", OK, f"{len(agents)} configured: {listing}")


async def check_whoami(cfg: BgosConfig, token_source: str = "") -> CheckResult:
    api = BgosApi(cfg)
    src = f" using token from {token_source}" if token_source else ""
    try:
        me = await api.whoami()
    except BgosApiError as exc:
        if exc.status == 401:
            fix = "Pairing revoked/expired. Delete the secrets file and re-pair."
            if token_source.startswith("env"):
                fix = (
                    "The rejected token came from the BGOS_API_KEY env var. "
                    "Unset it, restart the gateway, and re-pair if needed: "
                    "hermes-pair-bgos <CODE> --device-label <host>"
                )
            return CheckResult(
                "pairing_live", FAIL,
                f"whoami 401 ({exc.code or 'unauthorized'}){src}",
                fix=fix,
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
    results = [check_package(), check_registration()]
    cfg, cfg_result = check_config()
    results.append(cfg_result)
    hygiene = check_token_hygiene()
    if hygiene is not None:
        results.append(hygiene)
    results.append(check_env())
    results.append(check_catalog())
    if cfg is not None and not offline:
        choice, sp, _ = _resolve_token_state()
        results.append(
            await check_whoami(cfg, token_source=token_source_label(choice, sp)),
        )
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
