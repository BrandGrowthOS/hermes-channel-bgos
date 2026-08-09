"""Command-line pairing tool: `hermes-pair-bgos BGOS-XXXX-XX`.

Exchanges the BGOS-issued pair code for a pairing token and writes the
result to `~/.hermes/secrets/bgos.json` with mode 0600 on POSIX. Respects
the `HERMES_HOME` env var for non-default installs.

Exposed as a pyproject.toml console script; also usable directly via
`python -m hermes_channel_bgos.pair_cli BGOS-XXXX-XX --device-label foo`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import click

from .agents import parse_agents_spec
from .bgos_api import BgosApi, BgosApiError
from .config import BgosConfig, normalize_base_url
from .topology import (
    FAIL as TOPO_FAIL,
    local_topology_findings,
    overlapping_pairing_findings,
)



def server_error_detail(body: str | None) -> str:
    """The server's own explanation from an error body, or empty.

    Nest error bodies are JSON with a `message` that is a string or a list.
    The pair-exchange 409, for example, names the exact live pairing to
    revoke; swallowing it cost a real user a working explanation (2026-08-09,
    the CLI printed only "HTTP 409" while the body said which pairing served
    the agent and what to do). Raw non-JSON bodies are passed through
    truncated.
    """
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body.strip()[:500]
    msg = parsed.get("message") if isinstance(parsed, dict) else None
    if isinstance(msg, list):
        msg = "; ".join(str(m) for m in msg)
    return str(msg).strip()[:500] if msg else ""


def secrets_path() -> Path:
    """Resolve the BGOS secrets file path.

    Defaults to `~/.hermes/secrets/bgos.json`. Overridable by setting the
    `HERMES_HOME` env var (useful for multi-install or tests).
    """
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "secrets" / "bgos.json"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _print_findings(findings, *, header: str) -> None:
    click.secho(header, fg="red", err=True, bold=True)
    for f in findings:
        color = "red" if f.severity == TOPO_FAIL else "yellow"
        click.secho(f"  [{f.severity}] {f.detail}", fg=color, err=True)
        if f.fix:
            click.secho(f"        fix: {f.fix}", fg=color, err=True)


def enforce_local_topology(catalog: list[dict]) -> list:
    """Pre-exchange topology gate. Returns the FAIL findings (empty = go).

    Runs the disk-only checks from `topology` against the catalog this
    pairing is about to declare. The caller aborts BEFORE the pair exchange
    on any FAIL: the whole point of the guard is that the 2026-08-04 broken
    topology (missing profile, multiplex off, stray per-profile pairing
    file) must be impossible to pair into silently.
    """
    routes = [entry["agent_route"] for entry in catalog]
    findings = local_topology_findings(_hermes_home(), routes)
    fails = [f for f in findings if f.severity == TOPO_FAIL]
    if findings:
        _print_findings(
            findings,
            header=(
                "Topology check found problems on this host "
                "(HERMES_HOME=" + str(_hermes_home()) + "):"
            ),
        )
    return fails


async def _report_pairing_overlap(
    *, base_url: str, token: str, self_pairing_id: int | None,
    catalog: list[dict], device_label: str,
) -> None:
    """Post-exchange duplicate-pairing report. Loud, honest, never fatal."""
    api = BgosApi(BgosConfig(base_url=base_url, pairing_token=token))
    try:
        pairings = await api.list_pairings()
    except Exception:
        click.secho(
            "Could not verify this host against existing pairings (the "
            "pairings listing was unavailable). If an OLD pairing for this "
            "machine is still active, every reply will arrive twice - check "
            "BGOS -> Integrations -> Hermes -> Paired devices and revoke "
            "stale entries.",
            fg="yellow", err=True,
        )
        return
    finally:
        await api.close()
    findings = overlapping_pairing_findings(
        pairings,
        self_pairing_id=self_pairing_id,
        routes=[entry["agent_route"] for entry in catalog],
        device_label=device_label,
    )
    if findings:
        _print_findings(
            findings,
            header="Duplicate-pairing check found existing active pairings:",
        )
    else:
        others = sum(
            1 for p in pairings if p.get("id") != self_pairing_id
        )
        click.secho(
            f"Duplicate-pairing check clean "
            f"({others} other active pairing(s), no overlap).",
            fg="green",
        )


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


@click.command("hermes-pair-bgos")
@click.argument("code")
@click.option(
    "--device-label", required=True,
    help="Short label for this device, e.g. 'hades-box'.",
)
@click.option(
    "--base-url", default="https://api.brandgrowthos.ai",
    envvar="BGOS_API_URL", show_default=True,
    help="BGOS API base URL. Defaults to production; override for local dev.",
)
@click.option(
    "--integration", default="hermes", show_default=True,
    help="Integration type. Leave default.",
)
@click.option(
    "--agents", default="", show_default=False,
    help="Comma-separated route:Name agents to publish to BGOS at pair time, "
         "e.g. 'default:David' or 'hades:Hades,ramy:Ramy'. Lets you tick "
         "agents in the Integrations UI before the gateway even starts.",
)
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
@click.option(
    "--skip-topology-check", is_flag=True,
    help="Pair even when the multi-agent topology check fails. The broken "
         "state WILL misbehave (wrong persona / double answers) until fixed.",
)
def main(
    code: str, device_label: str, base_url: str, integration: str, agents: str,
    wait_for_exposure_flag: bool, wait_timeout: float, wait_interval: float,
    skip_topology_check: bool,
) -> None:
    """Exchange a BGOS pairing code for a pairing token.

    The token is written to ~/.hermes/secrets/bgos.json (mode 0600 on POSIX).
    Re-run to re-pair — the old file is overwritten.
    """
    asyncio.run(_run(
        code, device_label, base_url, integration, agents,
        wait_for_exposure_flag, wait_timeout, wait_interval,
        skip_topology_check=skip_topology_check,
    ))


async def _run(
    code: str, device_label: str, base_url: str, integration: str, agents: str,
    wait_for_exposure_flag: bool = False, wait_timeout: float = 180.0,
    wait_interval: float = 4.0, *, skip_topology_check: bool = False,
) -> None:
    # Normalize up front so both the live calls AND the persisted secrets file
    # carry the origin form. Pasting the app-facing base (which ends in
    # `/api/v1`) used to persist a base_url that doubled the API prefix on
    # every later request and 404d the whole pairing.
    base_url = normalize_base_url(base_url)
    catalog = parse_agents_spec(agents)

    # Install-time topology guard (0.23.0). Runs BEFORE the exchange so a
    # broken multi-agent topology never mints a pairing: the 2026-08-04
    # Achilles/Shadow incident was exactly this state, created silently and
    # only warned about in a connect-time log nobody read.
    if not skip_topology_check:
        fails = enforce_local_topology(catalog)
        if fails:
            click.secho(
                "Not pairing into a broken topology. Apply the fixes above "
                "and re-run, or override with --skip-topology-check.",
                fg="red", err=True, bold=True,
            )
            sys.exit(1)
    elif catalog:
        click.secho(
            "Topology check SKIPPED (--skip-topology-check).",
            fg="yellow", err=True,
        )

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
        detail = server_error_detail(exc.body)
        if detail:
            click.secho(f"Server says: {detail}", fg="yellow", err=True)
        sys.exit(1)
    finally:
        await api.close()

    path = secrets_path()
    # SECURITY: create the token file 0600 from the first byte rather than
    # write-then-chmod, so the pairing token is never briefly world/group
    # readable (the node channel plugins write their secrets atomically at 0600;
    # this matches that hygiene). 0o600 has no group/other bits, so umask cannot
    # widen it. The parent dir is created 0700 up front for the same reason.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {
        "pairing_token": resp["pairing_token"],
        "pairing_id": resp["pairing_id"],
        "base_url": base_url,
    }
    payload = json.dumps(data, indent=2)
    if os.name == "posix":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        # Belt and suspenders: enforce 0600 even if the file pre-existed with
        # looser perms, and tighten the parent dir if it already existed.
        os.chmod(path, 0o600)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            # Non-fatal — secrets file itself is already 0600
            pass
    else:
        path.write_text(payload)

    click.secho(f"Paired. Secret written to {path}", fg="green")

    # Publish the catalog with the authenticated token too, so it lands even
    # if the pre-auth pair-exchange didn't persist agentCatalog. This makes
    # the agents tickable in the Integrations UI before the gateway ever
    # starts.
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
            detail = server_error_detail(exc.body)
            click.secho(
                f"Catalog push failed (non-fatal): HTTP {exc.status}"
                + (f" - {detail}" if detail else ""),
                fg="yellow", err=True,
            )
        finally:
            await auth_api.close()

    # Duplicate-pairing check: a re-pair leftover (another ACTIVE pairing
    # with the same agent catalog) answers every inbound twice. Needs the
    # authenticated token, so it can only run AFTER the exchange - hence
    # warn-loud rather than abort. Never fatal: the new pairing stands.
    if not skip_topology_check:
        await _report_pairing_overlap(
            base_url=base_url,
            token=resp["pairing_token"],
            self_pairing_id=resp["pairing_id"],
            catalog=catalog,
            device_label=device_label,
        )

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


if __name__ == "__main__":  # pragma: no cover
    main()
