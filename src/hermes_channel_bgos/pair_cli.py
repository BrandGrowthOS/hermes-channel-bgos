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
from pathlib import Path

import click

from .bgos_api import BgosApi, BgosApiError
from .config import BgosConfig


def secrets_path() -> Path:
    """Resolve the BGOS secrets file path.

    Defaults to `~/.hermes/secrets/bgos.json`. Overridable by setting the
    `HERMES_HOME` env var (useful for multi-install or tests).
    """
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return root / "secrets" / "bgos.json"


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
def main(code: str, device_label: str, base_url: str, integration: str) -> None:
    """Exchange a BGOS pairing code for a pairing token.

    The token is written to ~/.hermes/secrets/bgos.json (mode 0600 on POSIX).
    Re-run to re-pair — the old file is overwritten.
    """
    asyncio.run(_run(code, device_label, base_url, integration))


async def _run(
    code: str, device_label: str, base_url: str, integration: str,
) -> None:
    api = BgosApi(BgosConfig(base_url=base_url, pairing_token=None))
    try:
        resp = await api.pair_exchange(
            code=code, device_label=device_label, integration=integration,
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
        # Tighten parent perms on POSIX too — don't weaken them on Windows
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            # Non-fatal — secrets file itself is already 0600
            pass

    click.secho(f"Paired. Secret written to {path}", fg="green")


if __name__ == "__main__":  # pragma: no cover
    main()
