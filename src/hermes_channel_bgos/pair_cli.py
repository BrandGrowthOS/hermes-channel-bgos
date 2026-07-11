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
from .config import BgosConfig


def secrets_path() -> Path:
    """Resolve the BGOS secrets file path.

    Defaults to `~/.hermes/secrets/bgos.json`. Overridable by setting the
    `HERMES_HOME` env var (useful for multi-install or tests).
    """
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "secrets" / "bgos.json"


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


async def _run(
    code: str, device_label: str, base_url: str, integration: str, agents: str,
    wait_for_exposure_flag: bool = False, wait_timeout: float = 180.0,
    wait_interval: float = 4.0,
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
            click.secho(
                f"Catalog push failed (non-fatal): HTTP {exc.status}",
                fg="yellow", err=True,
            )
        finally:
            await auth_api.close()

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
