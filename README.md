# hermes-channel-bgos

BGOS channel adapter for [Nous Research's Hermes agent](https://github.com/NousResearch/hermes-agent). A Python vendor package paired with a thin private fork of Hermes. Once installed, your Hermes agents show up inside the BGOS mobile/desktop app the same way they would on Telegram — chat, slash commands, approvals, files, the whole shape.

**Status:** Phase 1 — running in production. Message round-trips work end-to-end. Approvals render as 4-button inline bubbles. A handful of gotchas documented below.

**v0.5.0 (2026-05-12):** unlocks the gateway-driven tool-progress UI for BGOS — emoji-prefixed tool bubbles, edit-in-place streaming, typing indicators between long tool calls, and intermediate-streaming-preview cleanup all flow automatically through the standard Hermes gateway. No fork-patch changes required: the new adapter overrides three optional `BasePlatformAdapter` methods (`edit_message`, `delete_message`, `send_typing`), which is what Hermes's gateway probes to decide whether to drive those features.

## Contents
- [Quick start with Claude Code (recommended)](#quick-start-with-claude-code-recommended)
- [Prerequisites](#prerequisites)
- [Manual setup](#manual-setup)
- [Configuration (env vars)](#configuration-env-vars)
- [First-time pairing](#first-time-pairing)
- [Bridge-local slash commands](#bridge-local-slash-commands)
- [Approvals](#approvals)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)
- [Running the tests](#running-the-tests)

---

## Quick start with Claude Code (recommended)

Setting this up by hand involves patching Hermes across ~16 files, wiring up a Python package into whatever environment Hermes is using (uv / pipx / venv / system), and getting a bunch of env vars right. Easiest path: paste the prompt below into [Claude Code](https://claude.com/claude-code) running **on the server where your Hermes lives**. It will do the setup, ask you for the missing pieces (pair code, agent routes), and confirm everything's green.

> ### Prompt to paste to Claude Code on your Hermes server
>
> I want to connect my Hermes agent to BGOS so I can chat with it from the BGOS app. Please install the [`hermes-channel-bgos`](https://github.com/BrandGrowthOS/hermes-channel-bgos) vendor package on this server and get it running with Hermes.
>
> Full task:
>
> **1. Figure out my setup.** Detect:
> - Where Hermes is installed (check `~/.hermes/hermes-agent`, `/opt/hermes*`, `which hermes`, `pipx list`, `systemctl --user list-units | grep -i hermes`).
> - Which Python environment runs Hermes (uv-managed venv at `<install>/venv/`? pipx? system?). Find the actual `python` and `pip` binaries it uses — not system pip.
> - How Hermes is normally started (systemd user service? tmux? cron?).
> - Python version — must be ≥ 3.11.
>
> Report what you find before proceeding so I can confirm.
>
> **2. Clone the vendor package.**
> ```
> git clone https://github.com/BrandGrowthOS/hermes-channel-bgos.git ~/hermes-channel-bgos
> ```
>
> **3. Apply the Hermes fork patch.** The integration requires ~80 lines of registration boilerplate in the Hermes install. Patch is at `~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch`:
> ```
> cd <hermes-install-path>
> git checkout -b bgos-integration
> git am ~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch
> # If that fails with "patch does not apply" (upstream drift), retry with:
> git am --3way ~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch
> ```
> If you still get conflicts, stop and show me — don't guess.
>
> **4. Install the vendor package into Hermes's Python env.** Use the actual Python that runs Hermes. Examples:
> - uv-managed venv: `<hermes-install>/venv/bin/pip install -e ~/hermes-channel-bgos` (may not have pip; use `uv pip install -e ~/hermes-channel-bgos` from inside `<hermes-install>` instead).
> - pipx: `pipx inject <hermes-package> -e ~/hermes-channel-bgos`.
> - System Python (Debian/Ubuntu may need `--break-system-packages`; avoid if the server also runs other Python tools).
>
> Verify with:
> ```
> <correct-python> -c "from gateway.config import Platform; print(Platform.BGOS)"
> <correct-python> -c "from gateway.platforms.bgos import BGOSAdapter; print(BGOSAdapter.__name__)"
> <correct-python> -c "import hermes_channel_bgos; print(hermes_channel_bgos.__version__)"
> ```
> All three should print successfully.
>
> **5. Ask me for a pair code.** Tell me to:
> - Open the BGOS app → **Integrations** → ⚡ **Hermes** card → **"Connect a new Hermes server"** — copy the `BGOS-XXXX-XX` code.
> - Report it back to you. Codes expire in 10 minutes.
>
> Then pair:
> ```
> <correct-python> -m hermes_channel_bgos.pair_cli BGOS-XXXX-XX --device-label <this-server-hostname>
> ```
> Expected output: `Paired. Secret written to ~/.hermes/secrets/bgos.json`
>
> **6. Ask me what my Hermes agents are called.** I'll give you a list of `route:Display Name` pairs (e.g. `default:Hermes` or `hades:Hades,ramy:Ramy`). Use this for `BGOS_AGENTS` in the next step.
>
> **7. Configure Hermes's environment.** The adapter needs these env vars. Set them so they survive restarts — usually via the Hermes systemd user service's `EnvironmentFile=` directive, or a service drop-in at `~/.config/systemd/user/hermes-gateway.service.d/bgos-env.conf`:
> ```
> [Service]
> EnvironmentFile=<hermes-install>/.env
> ```
> Then create/update the `.env` (mode 0600 — contains a pairing token):
> ```
> BGOS_AGENTS=<the agents I gave you in step 6>
> BGOS_ALLOW_ALL_USERS=true
> ```
> Note `BGOS_API_KEY` is NOT strictly required — the fork patch now auto-reads the pairing token from `~/.hermes/secrets/bgos.json`. But if you're paranoid, include it too.
>
> **8. Start Hermes.** If a systemd service existed, `systemctl --user daemon-reload && systemctl --user restart hermes-gateway.service`. Otherwise whatever launch command Kc/I use normally.
>
> **9. Verify it's alive.** In Hermes's log (journalctl for systemd, or stdout) look for:
> ```
> bgos_ws.connected pairing_id=<N> assistants=[<id>]
> pushed agent catalog: <N> entries
> ```
> No `TypeError`, no `KeyError`, no `BGOS pairing token not found`, no silent drops.
>
> **10. Ask me to send a test message** from the BGOS app to the Hermes assistant. I should see the agent reply in a few seconds. If not, tail the Hermes log and report what you see.
>
> **Troubleshooting reference:** https://github.com/BrandGrowthOS/hermes-channel-bgos#troubleshooting — check this first if any step errors. Common gotchas:
> - Debian `externally-managed-environment` → you used system pip instead of Hermes's venv pip.
> - `TypeError: BasePlatformAdapter.__init__() missing 2 required positional arguments` → vendor pkg is outdated, `git pull` in `~/hermes-channel-bgos`.
> - `KeyError: 'id'` on connect → same, `git pull` and restart.
> - Hermes card's agent catalog stays empty → `BGOS_AGENTS` isn't in the restarted service's env.
> - Messages reach BGOS but never arrive at Hermes → `BGOS_ALLOW_ALL_USERS=true` missing.
> - Every restart replays all message history → vendor pkg predates commit `c571cd6`, `git pull`.
>
> Report progress at each numbered step. Confirm before moving to the next one if anything's ambiguous.

That's the whole setup. When it's done, you're paired, agents are bound, messages round-trip. Rest of this README is reference material if you're doing it by hand or hitting edge cases.

---

## Prerequisites

- A running Hermes install. If you don't have one yet, set that up first — `https://github.com/NousResearch/hermes-agent`.
- Python **3.11+** (matches Hermes's own `requires-python`).
- A BGOS account with pairing codes enabled (Integrations screen → Hermes card).
- Shell access to the machine where Hermes runs. Setup is not possible from the BGOS app side alone.

---

## Manual setup

Two installs: a patched Hermes (one-time per machine) + this vendor package.

### Step 1 — Apply the Hermes fork patch

```bash
# If you're setting up Hermes from scratch, clone + install it first
# (this package assumes Hermes already works).

cd <wherever-Hermes-is-cloned>
git checkout -b bgos-integration

# Clone this vendor package (we'll install from it in step 2)
git clone https://github.com/BrandGrowthOS/hermes-channel-bgos.git ~/hermes-channel-bgos

# Apply the integration patch. Try plain am first; if upstream has
# drifted, --3way auto-merges around context shifts.
git am ~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch \
  || git am --3way ~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch
```

Patch touches 16 files registering `Platform.BGOS` per Hermes's `gateway/platforms/ADDING_A_PLATFORM.md`. See [`hermes-fork-patch/FORK-NOTES.md`](hermes-fork-patch/FORK-NOTES.md) for the per-file breakdown and drift status.

### Step 2 — Install the vendor package into Hermes's Python environment

Use whichever Python actually runs your Hermes. Common cases:

**uv-managed venv at `<hermes>/venv/`** (typical for Hermes installs):
```bash
cd <hermes-install-path>
uv pip install -e ~/hermes-channel-bgos
```
`uv`-managed venvs often don't have `pip` on disk — use `uv pip install` from inside the Hermes dir.

**pipx-managed Hermes:**
```bash
pipx inject <hermes-package-name> -e ~/hermes-channel-bgos
```
Replace `<hermes-package-name>` with whatever `pipx list` calls it (e.g. `hermes-agent`).

**System Python (Debian/Ubuntu with PEP 668 guard):**
```bash
pip install --break-system-packages -e ~/hermes-channel-bgos
```
⚠️ Don't use this if your server also runs other Python tooling — it installs system-wide.

### Step 3 — Verify imports from Hermes's Python

```bash
<python-that-runs-hermes> -c "from gateway.config import Platform; assert Platform.BGOS.value == 'bgos'; print('enum OK')"
<python-that-runs-hermes> -c "from gateway.platforms.bgos import BGOSAdapter; print('shim OK:', BGOSAdapter.__name__)"
<python-that-runs-hermes> -c "import hermes_channel_bgos; print('vendor pkg:', hermes_channel_bgos.__version__)"
```

All three should succeed. If any fail with `ModuleNotFoundError`, you installed into the wrong Python.

### Step 4 — Pair

See [First-time pairing](#first-time-pairing) below.

### Step 5 — Configure env vars

See [Configuration](#configuration-env-vars) below. Add them to Hermes's systemd service's `EnvironmentFile=` or equivalent so they persist across restarts.

### Step 6 — Start Hermes and watch for success signals

```
bgos_ws.connected pairing_id=<N> assistants=[...]
pushed agent catalog: <N> entries
```

---

## Configuration (env vars)

Put these wherever Hermes's launcher reads env. For a systemd user service, create a drop-in:

```bash
mkdir -p ~/.config/systemd/user/hermes-gateway.service.d
cat > ~/.config/systemd/user/hermes-gateway.service.d/bgos-env.conf <<EOF
[Service]
EnvironmentFile=%h/.hermes/hermes-agent/.env
EOF

# Then create the .env (mode 0600 — contains a sensitive token)
touch <hermes-install>/.env && chmod 600 <hermes-install>/.env
# Add the vars below to that file
```

| Var | Default | Required? | What |
|---|---|---|---|
| `BGOS_API_KEY` | — | No (fallback reads secrets file) | Pairing token. Written by `hermes-pair-bgos` to `~/.hermes/secrets/bgos.json`. The fork patch auto-reads from the secrets file if this env var is absent, so you usually don't need to set it explicitly. |
| `BGOS_BACKEND_URL` | `https://api.brandgrowthos.ai` | No | BGOS backend base URL. Override only for local dev against a staging backend. |
| `BGOS_AGENTS` | — | **Yes** (or the Integrations UI stays empty) | Comma-separated `route:Display Name` pairs. e.g. `default:Hermes` or `hades:Hades,ramy:Ramy,heartbeat:Heartbeat`. Route must match what Hermes internally calls the agent (check with `hermes agents list` or your Hermes config). |
| `BGOS_AGENTS_JSON` | — | No (alternative to `BGOS_AGENTS`) | Richer JSON-list format: `'[{"agent_route":"default","name":"Hermes","description":"..."}]'`. Use when display names contain commas/colons or you want a description. |
| `BGOS_ALLOW_ALL_USERS` | `false` | **Yes** (unless you set `BGOS_ALLOWED_USERS`) | The fork's `BasePlatformAdapter._is_user_authorized` rejects all inbound messages unless this is `true`, OR the sender's Clerk user_id is in `BGOS_ALLOWED_USERS`. Without this, your messages silently never reach Hermes. |
| `BGOS_ALLOWED_USERS` | — | No (alternative) | Comma-separated Clerk user IDs authorized to chat with this Hermes. |
| `BGOS_POLL_INTERVAL` | `5` | No | Seconds between REST-poll backup checks for inbound messages. Only relevant while the WS push path has server-side gaps. Set to `0` to disable polling once WS push is reliable. |
| `HERMES_HOME` | `~/.hermes` | No | Root for Hermes config + secrets + the persisted `bgos_last_id` cursor. |

**Minimum working `.env`:**
```bash
BGOS_AGENTS=default:Hermes
BGOS_ALLOW_ALL_USERS=true
```

---

## First-time pairing

1. In BGOS → **Integrations** → ⚡ **Hermes card** → **"Connect a new Hermes server"**. Copy the `BGOS-XXXX-XX` code. It expires in 10 minutes.

2. On the Hermes server, run — using the same Python that runs Hermes:
   ```bash
   <hermes-python> -m hermes_channel_bgos.pair_cli BGOS-XXXX-XX --device-label <this-server-hostname>
   ```
   Or, if `hermes-pair-bgos` is on the same PATH as Hermes:
   ```bash
   hermes-pair-bgos BGOS-XXXX-XX --device-label <this-server-hostname>
   ```
   Expected output: `Paired. Secret written to ~/.hermes/secrets/bgos.json` (mode 0600).

3. Back in BGOS → Integrations → the Hermes card should now show your server under **Paired devices**. When Hermes is running with `BGOS_AGENTS` configured, the **"Pick which agents to expose"** checklist populates; tick what you want, click Save, and those agents appear as assistants in your sidebar.

4. Open the assistant's chat in the app and send a message. You should see a reply within a few seconds.

---

## Bridge-local slash commands

Three commands are handled by this adapter locally — they never reach your Hermes agent. Useful in every paired chat:

| Command | What it does |
|---|---|
| `/new` | Reset this chat's conversation binding. Your next message starts fresh. |
| `/retry` | Resend the last user message in this chat through the agent. |
| `/status` | Show adapter health: pairing id, assistants bound, last message id seen, pending approvals. |

Everything else (`/help`, `/stop`, `/approve`, `/deny`, etc.) goes straight to Hermes as-is.

---

## Approvals

When your Hermes agent wants to run a dangerous command (shell exec, file write, etc.), the gateway renders an approval prompt with 4 buttons — same behavior as Telegram:

- **Allow once** — this command only
- **Allow for session** — any similar command until the session ends
- **Always allow** — never ask again for this tool
- **Deny** — kill the command

Hermes's default timeout is 60s, fail-closed. If you miss the window, the command is denied.

The adapter has a 15-second hard budget to post the approval bubble; if BGOS is slow, Hermes falls back to posting a plain-text "`/approve` or `/deny`" prompt.

---

## Troubleshooting

Every issue below was caught during real-world integration testing. If something breaks, check here first.

### Install / environment

**`error: externally-managed-environment` (Debian/Ubuntu).** You're running `pip install` against system Python with PEP 668 active. You want Hermes's Python, not the system one. Use `<hermes-install>/venv/bin/pip`, `uv pip install -e ...` from inside the Hermes dir, or `pipx inject` — not bare `pip`.

**`hermes-pair-bgos: command not found`.** The console script didn't land on PATH (usually because you installed into a venv whose `bin/` isn't on `$PATH`). Invoke via module instead:
```bash
<hermes-python> -m hermes_channel_bgos.pair_cli BGOS-XXXX-XX --device-label <label>
```

**uv-managed venv has no `pip`.** Use `uv pip install -e ~/hermes-channel-bgos` from inside the Hermes directory. `uv` manages the install itself; there's no pip binary on disk.

### Hermes startup

**`TypeError: BasePlatformAdapter.__init__() missing 2 required positional arguments: 'config' and 'platform'`.** Your vendor pkg is older than commit `1416948`. `cd ~/hermes-channel-bgos && git pull` then restart Hermes.

**`KeyError: 'id'` during `BGOSAdapter.connect()`.** Vendor pkg is older than commit `b73edd6` (read assistant_id from whoami, not id). `git pull` then restart.

**`BGOS pairing token not found`.** The adapter couldn't find a pairing token anywhere — not in env, not in secrets file. Did you run `hermes-pair-bgos`? Is `HERMES_HOME` pointing to a different dir than where the pair CLI wrote? Check:
```bash
ls -la ~/.hermes/secrets/bgos.json
```
If that exists and is non-empty, the adapter should pick it up automatically.

### Runtime — message flow

**Every Hermes restart sends duplicate replies forever.** You're on a vendor pkg older than `c571cd6`. The first-connect backfill was hardcoded to `since_message_id=0`, replaying all history on every restart. `git pull` and restart — newer versions persist the cursor to `~/.hermes/bgos_last_id`.

**Hermes card's agent checklist stays empty.** The adapter on connect logs:
```
no Hermes agents discovered — Hermes Integrations card will show an empty catalog
```
Fix: set `BGOS_AGENTS=<route>:<Display Name>` in the systemd env. Restart. Re-check logs for `pushed agent catalog: N entries`.

**Messages sent from BGOS never reach Hermes, but `/api/v1/integrations/inbound` REST endpoint shows them.** Most likely `BGOS_ALLOW_ALL_USERS=true` isn't set. The fork's auth gate silently drops inbound messages otherwise. There's also a known server-side WS-push gap that the adapter works around via a 5-second REST-poll loop (see `BGOS_POLL_INTERVAL`). If neither helps, tail Hermes's log for `bgos_ws` and `is_user_authorized` lines.

**`401 PAIRING_REVOKED` in the adapter log.** Someone (you?) revoked the pairing in BGOS. Delete `~/.hermes/secrets/bgos.json`, generate a new code, re-run `hermes-pair-bgos`.

**WS connected but immediately disconnected.** Token mismatch or server-side auth reject. The adapter's pairing token got overwritten or corrupted. Re-pair.

### Outbound messages

**Hermes replies silently 400 on the backend.** Wire-format drift — your vendor pkg predates `834ad9d` (camelCase sender/messageType, correct option/file shape). `git pull` and restart.

**Approval bubbles render as plain text.** Backend doesn't yet accept `approvalMeta` / `style` / `row_index` on messages (Phase F backend work). The prompt still gets through; just without the colored buttons. Deny / approve by clicking the button text — it'll still work as a regular callback.

### Systemd / environment

**Env vars set via `systemctl --user set-environment` get lost on reboot.** Expected — that's a runtime-only mechanism. Persist via a drop-in with `EnvironmentFile=`:
```
~/.config/systemd/user/hermes-gateway.service.d/bgos-env.conf:
[Service]
EnvironmentFile=<hermes-install>/.env
```
Then `systemctl --user daemon-reload && systemctl --user restart hermes-gateway.service`.

**`hermes status` shows BGOS ✗ even though the adapter is running.** Display-only bug in Hermes CLI — it re-evaluates config from scratch in a separate process, doesn't see the running service's runtime env. Check via `journalctl --user -u hermes-gateway | grep bgos_ws.connected` instead.

---

## Architecture

- `BgosApi` (`bgos_api.py`) — async httpx client for the BGOS backend's integration endpoints. Sends `X-BGOS-Pairing` on every authenticated call. Wire format matches backend DTOs: camelCase (`chatId`, `messageType`, `approvalMeta`), options `{text, callbackData, style?}`, files `{fileName, fileMimeType, fileData? | s3Key?}`.
- `BgosWs` (`bgos_ws.py`) — python-socketio client. Handshake with `?pairingToken=…`, joins `pairing:<id>` + `assistant:<id>` rooms, exponential-backoff reconnect, REST backfill hook on reconnect.
- `BGOSAdapter` (`bgos_adapter.py`) — subclass of `BasePlatformAdapter`. Resolves config from (priority order) BgosConfig → Hermes config attrs → env vars → `~/.hermes/secrets/bgos.json`. Implements the 4 abstract methods + optional `send_image/voice/video/document/animation` + `send_exec_approval` with 4-button Telegram parity.
- `StateStore` (`state_store.py`) — in-process state: assistant→route map, retry cache, conversation bindings. Not persisted; rebuilt from `whoami()` + REST backfill on reconnect.
- `bgos_last_id` file (`$HERMES_HOME/bgos_last_id`) — persisted cursor for the last seen message id. Monotonically advances. Prevents history-replay on restart.
- `commands_sync.py` — merges Hermes's native slash manifest with the 3 bridge-local commands, pushes via `PUT /integrations/assistants/:id/commands`.
- `pair_cli.py` — the `hermes-pair-bgos` console script.
- `hermes-fork-patch/` — the one-time fork patch (`0001-bgos-integration.patch`) + notes.

Full design spec: [`../docs/superpowers/specs/2026-04-23-hermes-bgos-integration-design.md`](../docs/superpowers/specs/2026-04-23-hermes-bgos-integration-design.md) (in the BGOS monorepo).

---

## Running the tests

```bash
cd hermes-channel-bgos
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # POSIX
pip install -e ".[dev]"
pytest -v
```

Expected: **72 passed, 2 skipped**. The reconnect test needs a real BGOS backend; the mode-0600 permission check is POSIX-only.
