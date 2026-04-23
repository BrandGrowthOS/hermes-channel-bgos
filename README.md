# hermes-channel-bgos

BGOS channel adapter for [Nous Research's Hermes agent](https://github.com/NousResearch/hermes-agent). Vendor pip package paired with a thin private fork of `hermes-agent` — see [distribution decision](docs/distribution-decision.md) for the full rationale.

**What this does:** lets you talk to your Hermes agents from BGOS (mobile/desktop chat) exactly the way you'd talk to them from Telegram. Slash commands, inline buttons, dangerous-command approvals (4-button Telegram parity), file attachments, real-time reconnect + backfill.

---

## Install

Two pieces needed: a private fork of Hermes (one-time) and this vendor package.

### One-time: apply the BGOS fork patch

```bash
# Clone Hermes to wherever you normally keep source
git clone https://github.com/NousResearch/hermes-agent ~/hermes-agent
cd ~/hermes-agent
git checkout -b bgos-integration

# Apply the 16-point BGOS integration patch (produced in Phase 4)
git am <path-to-bgos-monorepo>/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch

pip install -e .
```

See [`hermes-fork-patch/README.md`](hermes-fork-patch/README.md) for the fork workflow and the 16 touch-points per upstream's `gateway/platforms/ADDING_A_PLATFORM.md`. Nightly auto-rebase keeps the fork current.

### Install this package

```bash
pip install -e <path-to-bgos-monorepo>/hermes-channel-bgos
```

Hermes will now see `Platform.BGOS` and auto-route BGOS chats through `BGOSAdapter`.

---

## First-time pairing

1. Open BGOS → **Integrations** → Hermes card → **"Pair new device"**. Copy the `BGOS-XXXX-XX` code.
2. On your Hermes host:
   ```bash
   hermes-pair-bgos BGOS-XXXX-XX --device-label hades-box
   ```
3. Back in BGOS, tick which of your Hermes agents you want exposed as BGOS assistants. You're paired.

The token is stored at `~/.hermes/secrets/bgos.json` (mode 0600 on POSIX). Re-run the pair CLI to rotate or re-pair; the file is overwritten.

---

## Configuration

| Var | Default | What |
|---|---|---|
| `BGOS_API_URL` | `https://api.brandgrowthos.ai` | BGOS backend base URL. Override for local dev. |
| `HERMES_HOME` | `~/.hermes` | Root for Hermes config + secrets. |

Hermes itself gets the pairing token by reading `$HERMES_HOME/secrets/bgos.json` when it instantiates the adapter (via the fork's 5-line shim at `gateway/platforms/bgos.py`).

---

## Bridge-local slash commands

Three commands are handled by this adapter, not forwarded to your Hermes agent. They work in every paired BGOS chat:

| Command | What it does |
|---|---|
| `/new` | Reset this chat's conversation binding. Your next message starts fresh, as if you paired today. |
| `/retry` | Resend the last user message in this chat through the agent. |
| `/status` | Show adapter health: pairing id, assistants bound, last message id seen, pending approvals. |

Everything else (`/help`, `/stop`, `/approve`, `/deny`, etc.) is Hermes-native and forwarded to the agent as-is. If a name collides (only expected clash is `/status`), the bridge-local wins — the full-stack health picture is more useful than Hermes's alone.

---

## Approvals

When your Hermes agent wants to run a dangerous command, the gateway calls `send_exec_approval` on this adapter. We render a BGOS message bubble with **4 inline buttons**, matching Telegram parity exactly:

- **Allow once** — this command only
- **Allow for session** — any similar command until the session ends
- **Always allow** — never ask again for this tool
- **Deny** — kill the command

Tap a button; the verdict reaches Hermes synchronously via `tools.approval.resolve_gateway_approval`.

**Timeouts:** Hermes's default is 60 seconds, fail-closed. If you miss the window, the command is denied. Stale buttons (already resolved or timed out) no-op when clicked.

**15-second send deadline:** Hermes gives the adapter 15s to post the approval bubble; if BGOS is slow, Hermes falls back to posting a plain-text `/approve` or `/deny` prompt.

---

## Troubleshooting

**"401 PAIRING_REVOKED" in the adapter log.** Your pairing was revoked in BGOS. Delete `~/.hermes/secrets/bgos.json` and re-run `hermes-pair-bgos` with a fresh code.

**Messages from BGOS aren't arriving.** Check WS connectivity — the adapter uses exponential-backoff reconnect (1s → 2s → 4s, cap 30s). Check the Hermes log for `bgos_ws.disconnected` / `bgos_ws.connected` lines. After a reconnect the adapter automatically backfills missed messages via `GET /api/v1/integrations/inbound?since_message_id=…`.

**"no_mock_route" on 501.** You're pointing at the test `MockBgosServer`, not a real backend. Check `BGOS_API_URL` and the `base_url` in your secrets file.

**Stale approval buttons do nothing.** Expected — the approval already resolved (timed out, approved elsewhere, or the adapter restarted). Ask the agent again.

---

## Running the tests

```bash
cd hermes-channel-bgos
python -m venv .venv
.venv/Scripts/activate          # or .venv/bin/activate on POSIX
pip install -e ".[dev]"
pytest -v
```

Expected: all tests pass, 2 skipped (the reconnect test needs a real BGOS backend; the mode-0600 test is POSIX-only).

---

## Architecture

Brief:

- `BgosApi` (`bgos_api.py`) — async httpx client for the BGOS backend's integration endpoints. Sends `X-BGOS-Pairing` on every authenticated call.
- `BgosWs` (`bgos_ws.py`) — python-socketio client. Handshake with `?pairingToken=…`, joins `pairing:<id>` + `assistant:<id>` rooms, handles exponential-backoff reconnect, triggers REST backfill via `on_reconnect`.
- `BGOSAdapter` (`bgos_adapter.py`) — subclass of `BasePlatformAdapter`. Implements the 4 abstract methods (`connect`, `disconnect`, `send`, `get_chat_info`) plus the optional media / approval overrides Hermes duck-types at runtime.
- `StateStore` (`state_store.py`) — in-process state: assistant→route map, retry cache, conversation bindings. Not persisted; rebuilt from `whoami()` + backfill.
- `commands_sync.py` — merges Hermes's native slash manifest with the 3 bridge-local commands for `PUT /integrations/assistants/:id/commands`.
- `pair_cli.py` — the `hermes-pair-bgos` console script.

Full design: [`../docs/superpowers/specs/2026-04-23-hermes-bgos-integration-design.md`](../docs/superpowers/specs/2026-04-23-hermes-bgos-integration-design.md).
