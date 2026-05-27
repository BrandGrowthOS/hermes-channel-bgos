# hermes-channel-bgos

BGOS channel adapter for [Nous Research's Hermes agent](https://github.com/NousResearch/hermes-agent). A Python vendor package paired with a thin private fork of Hermes. Once installed, your Hermes agents show up inside the BGOS mobile/desktop app the same way they would on Telegram — chat, slash commands, approvals, files, the whole shape.

**Status:** Phase 1 — running in production. Message round-trips work end-to-end. Approvals render as 4-button inline bubbles. A handful of gotchas documented below.

**v0.5.7 (2026-05-15) — Hotfix: bridge-local cursor advance.** Bridge-local slash commands (`/new`, `/status`, `/retry`, `/resume`, `/help`) were short-circuiting before the inbound cursor was persisted. The 5-second REST poll's `since_message_id` therefore stayed pinned to a stale value and kept re-fetching the same command from `/api/v1/integrations/inbound`, re-dispatching it on every tick. Caught live as 50+ "Conversation reset" acks for a single `/new`. Fix: `_save_last_id` is now called on every accepted inbound (including bridge-local) before any branch returns. If you're caught in a live loop, upgrade to 0.5.7 and restart Hermes; the cursor advances on the first tick after restart, so the loop stops within ~5s.

**v0.5.0 (2026-05-12) — Telegram-parity round.** This release closes most of the UX gap between BGOS and the Telegram channel. No fork-patch changes required.

- **Gateway-driven tool-progress UI** — emoji-prefixed tool bubbles (🔍 search, 🧠 memory, 🔧 patch, 💻 shell, ⚡…), edit-in-place streaming responses, typing indicators between long tool calls, and intermediate-streaming-preview cleanup all flow automatically because the adapter now overrides three optional `BasePlatformAdapter` methods (`edit_message`, `delete_message`, `send_typing`) — the gates Hermes's gateway probes to decide whether to drive these features. Edit calls are throttled to 1 per 1.5s per chat to mirror the Telegram pattern.
- **In-place approval bubble edits** — when a user taps an approval button, the bubble mutates in place to show the resolution (`✅ Approved once by user_42`) with the buttons removed.
- **Per-user callback authorization** — same gate as inbound text (`BGOS_ALLOW_ALL_USERS` or `BGOS_ALLOWED_USERS`). Fail-closed.
- **`send_slash_confirm` 3-button UI** — `/reload-mcp`-style slash commands that need explicit acknowledgment render with Approve Once / Always 🔒 / Cancel buttons (callback shape `sc:<choice>:<id>`).
- **`send_update_prompt`** — yes/no inline UI for Hermes's gateway update flow (stash restore, config migration).
- **Long-message splitting** — replies exceeding ~10K characters are auto-chunked with `(i/N)` continuations. Buttons + reply-to attach to chunk 1 only.
- **`format_message` MDv2-escape stripping** — Telegram-tuned prompts that emit `\,` `\!` `\.` etc. no longer leak visible backslashes through BGOS's CommonMark renderer. Real CommonMark escapes (`\*` `\_` `\[` `\(`) survive.
- **`send_multiple_images`** — bundles up to 10 images into a single multi-file POST (carousel-rendered).
- **Adaptive inbound text batching** — rapid plain-text messages from the same chat (e.g. mobile-client-split voice-memo transcripts) coalesce into one agent dispatch with an adaptive flush window (≤0.24s for short messages, up to 1.0s for the first chunk of a multi-part paste). Slash commands and file-bearing messages bypass.

Backend dependencies still in flight: `DELETE /api/v1/messages/{id}` (for streaming-preview cleanup; missing → preview stays visible, cosmetic only) and a WS `typing` event handler (missing → no typing indicator, cosmetic only). Both degrade gracefully; no exceptions.

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

Setting this up by hand involves patching Hermes across ~11 files, wiring up a Python package into whatever environment Hermes is using (uv / pipx / venv / system), and getting a bunch of env vars right. Easiest path: paste the prompt below into [Claude Code](https://claude.com/claude-code) running **on the server where your Hermes lives**. It will do the setup, ask you for the missing pieces (pair code, agent routes), and confirm everything's green.

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
> **3. Register BGOS with Hermes.** **First check for plugin support:** `<correct-python> -c "import gateway.platform_registry" && echo PLUGIN_OK`.
> - **If it prints `PLUGIN_OK` (modern Hermes): skip the fork patch.** After installing the pip package (step 4), symlink the plugin in:
>   ```
>   mkdir -p ~/.hermes/plugins
>   ln -sfn ~/hermes-channel-bgos/plugins/platforms/bgos ~/.hermes/plugins/bgos
>   ```
>   No core edits, nothing to rebase. Confirm later with `hermes-bgos-doctor` (`registration: ... via plugin`).
> - **Otherwise (pre-plugin Hermes): apply the fork patch** (~80 lines of registration boilerplate) at `~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch`:
>   ```
>   cd <hermes-install-path>
>   git checkout -b bgos-integration
>   git am ~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch
>   # If that fails with "patch does not apply" (upstream drift), retry with:
>   git am --3way ~/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch
>   ```
>   If you still get conflicts, stop and show me — don't guess.
>
> **4. Install the vendor package into Hermes's Python env.** Install into the **exact** interpreter that runs Hermes — pin it with `--python`:
> - **uv (primary path — most Hermes installs are uv-managed venvs):**
>   ```
>   uv pip install --python <hermes-install>/venv/bin/python -e ~/hermes-channel-bgos
>   ```
>   The `--python` flag targets Hermes's venv explicitly, so it works from any cwd and never lands in the wrong env. (uv-managed venvs usually have no `pip` binary on disk, which is why bare `pip` fails.)
> - **Regular venv with pip:** `<hermes-install>/venv/bin/pip install -e ~/hermes-channel-bgos`.
> - **pipx:** `pipx inject <hermes-package> -e ~/hermes-channel-bgos`.
> - **System Python** (last resort; Debian/Ubuntu may need `--break-system-packages`; avoid if the server also runs other Python tools).
>
> Verify with:
> ```
> <correct-python> -c "from gateway.config import Platform; print(Platform.BGOS)"
> <correct-python> -c "from gateway.platforms.bgos import BGOSAdapter; print(BGOSAdapter.__name__)"
> <correct-python> -c "import hermes_channel_bgos; print(hermes_channel_bgos.__version__)"
> ```
> All three should print successfully.
>
> **5. Ask me for a pair code.** `BGOS-XXXX-XX` is a **placeholder** — there is no real code until I generate one. Tell me to:
> - Open the BGOS app → **Integrations** → ⚡ **Hermes** card → **"Connect a new Hermes server"** — copy the generated code (looks like `BGOS-7F3A-2K`).
> - Report it back to you. **Codes expire in 10 minutes** — if I'm slow, generate a fresh one. Don't run the pair command with the literal `BGOS-XXXX-XX`.
>
> Then pair. If you already know my agent routes (ask me — see step 6), pass them with `--agents` so they're tickable in the Integrations UI immediately, even before the gateway starts:
> ```
> <correct-python> -m hermes_channel_bgos.pair_cli BGOS-XXXX-XX --device-label <this-server-hostname> --agents "default:Hermes"
> ```
> (`--agents` is optional; without it, the catalog is published when the gateway first connects.) Expected output: `Paired. Secret written to ~/.hermes/secrets/bgos.json`
>
> **6. Ask me what my Hermes agents are called.** I'll give you a list of `route:Display Name` pairs (e.g. `default:Hermes` or `hades:Hades,ramy:Ramy`). Use this for both `--agents` above and `BGOS_AGENTS` in the next step.
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
> **8. Start Hermes.** Linux systemd: `systemctl --user daemon-reload && systemctl --user restart hermes-gateway.service`. macOS launchd: `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway` (find the env file Hermes loads with `hermes config env-path` — on macOS it's usually `~/.hermes/.env`, not `<hermes-install>/.env`). Otherwise whatever launch command Kc/I use normally.
>
> **9. Verify it's alive.** Run the bundled doctor (use the same Python that runs Hermes) — it checks the install, pairing, and exposed agents in one shot:
> ```
> <correct-python> -m hermes_channel_bgos.doctor
> ```
> Everything should be `OK`/`WARN`, with `RESULT: OK`. (A `WARN` on `pairing_live` just means I haven't ticked any agents in the UI yet — see step 10.) Also confirm in Hermes's log (journalctl for systemd, or stdout):
> ```
> bgos_ws.connected pairing_id=<N> assistants=[<id>]
> BGOS catalog pushed: <route>:<Name>
> ```
> No `TypeError`, no `KeyError`, no `BGOS pairing token not found`.
>
> **10. Ask me to expose + test.** Tell me to open BGOS → **Integrations** → **Hermes** → tick the agent(s) → **Save**, then send a message to the assistant. I should see a reply within a few seconds. Exposing an agent *after* the gateway started is fine — the adapter hot-loads new agents automatically (no restart needed). If there's no reply, run `hermes-bgos-doctor` and check the `pairing_live` line.
>
> **Troubleshooting reference:** https://github.com/BrandGrowthOS/hermes-channel-bgos#troubleshooting — check this first if any step errors. Common gotchas:
> - Debian `externally-managed-environment` → you used system pip instead of Hermes's venv pip.
> - `TypeError: BasePlatformAdapter.__init__() missing 2 required positional arguments` → vendor pkg is outdated, `git pull` in `~/hermes-channel-bgos`.
> - `KeyError: 'id'` on connect → same, `git pull` and restart.
> - Hermes card's agent catalog stays empty → `BGOS_AGENTS` isn't in the restarted service's env.
> - Messages reach BGOS but never arrive at Hermes → `BGOS_ALLOW_ALL_USERS=true` missing.
> - Agent selected in BGOS but no reply, log shows `inbound for unknown assistant_id=<id>` → you exposed the agent after the gateway started. On ≥0.8.0 this self-heals within a few seconds (hot-refresh); if it persists, run `hermes-bgos-doctor`.
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

Two paths: the **plugin path** (preferred — no core edits, nothing to rebase) on modern Hermes, or the legacy **fork patch** on Hermes that predates the plugin system. Both install this vendor package into Hermes's Python.

### Step 0 — Plugin path (preferred on modern Hermes)

If your Hermes has the plugin system (`<hermes-python> -c "import gateway.platform_registry"` succeeds), **skip the fork patch entirely**:

1. Clone + install the pip package into Hermes's Python (Step 2 below):
   ```bash
   git clone https://github.com/BrandGrowthOS/hermes-channel-bgos.git ~/hermes-channel-bgos
   uv pip install --python <hermes-install>/venv/bin/python -e ~/hermes-channel-bgos
   ```
2. Make the plugin discoverable:
   ```bash
   mkdir -p ~/.hermes/plugins
   ln -sfn ~/hermes-channel-bgos/plugins/platforms/bgos ~/.hermes/plugins/bgos
   ```
3. Pair (Step 4) + set env vars (Step 5), then restart Hermes. Verify with `hermes-bgos-doctor` — the `registration` line should read `via plugin`.

One `ctx.register_platform()` call registers `Platform.BGOS` with the adapter, config, auth, **cron delivery**, prompt hint, and status — **no core-file edits and nothing to rebase on upstream updates**. If `import gateway.platform_registry` fails, your Hermes predates the plugin system — use the fork-patch path below.

### Step 1 — Apply the Hermes fork patch (legacy / pre-plugin Hermes only)

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

Patch modifies **11 files** registering `Platform.BGOS` per Hermes's `gateway/platforms/ADDING_A_PLATFORM.md` (16 candidate touch-points, several of which are no-ops on the current base). The live list is self-verifying — `git apply --numstat hermes-fork-patch/0001-bgos-integration.patch`. See [`hermes-fork-patch/FORK-NOTES.md`](hermes-fork-patch/FORK-NOTES.md) for the per-file breakdown, drift status, and the **plugin-migration path** that may soon replace this patch entirely.

### Step 2 — Install the vendor package into Hermes's Python environment

Use whichever Python actually runs your Hermes. Common cases:

**uv-managed venv at `<hermes>/venv/`** (typical for Hermes installs — the primary path):
```bash
uv pip install --python <hermes-install>/venv/bin/python -e ~/hermes-channel-bgos
```
`--python` pins the exact interpreter Hermes runs, so this works from any directory and can't land in the wrong env. `uv`-managed venvs often have no `pip` binary on disk, which is why bare `pip install` fails — use `uv pip install`.

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
BGOS catalog pushed: <route>:<Name>
```

---

## Configuration (env vars)

Put these wherever Hermes's launcher reads env. **Find the file Hermes actually loads** with:

```bash
hermes config env-path        # prints the active env file path on any platform
```

### Linux (systemd user service)

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

Restart: `systemctl --user daemon-reload && systemctl --user restart hermes-gateway.service`.

### macOS (launchd)

On macOS, Hermes runs as a launchd agent (`ai.hermes.gateway`), and the env file is typically **`~/.hermes/.env`** (confirm with `hermes config env-path` — it may differ from `<hermes-install>/.env`). Edit that file directly (Hermes's launchd plist already loads it), then restart and verify:

```bash
# 1. Find + edit the env file Hermes loads
hermes config env-path                  # e.g. /Users/<you>/.hermes/.env
#    add the BGOS_* vars below to that file (chmod 600 it — contains a token)

# 2. Restart the gateway
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway

# 3. Verify it connected + pushed the catalog (adjust the log path to your install)
log show --predicate 'process == "hermes"' --last 2m 2>/dev/null \
  | grep -iE "bgos_ws.connected|BGOS catalog pushed" \
  || tail -n 200 ~/.hermes/logs/gateway.log | grep -iE "bgos_ws.connected|BGOS catalog pushed"
```

Then run `hermes-bgos-doctor` to confirm everything's green.

| Var | Default | Required? | What |
|---|---|---|---|
| `BGOS_API_KEY` | — | No (fallback reads secrets file) | Pairing token. Written by `hermes-pair-bgos` to `~/.hermes/secrets/bgos.json`. The fork patch auto-reads from the secrets file if this env var is absent, so you usually don't need to set it explicitly. |
| `BGOS_BACKEND_URL` | `https://api.brandgrowthos.ai` | No | BGOS backend base URL. Override only for local dev against a staging backend. |
| `BGOS_AGENTS` | — | **Yes** (or the Integrations UI stays empty) | Comma-separated `route:Display Name` pairs. e.g. `default:Hermes` or `hades:Hades,ramy:Ramy,heartbeat:Heartbeat`. Route must match what Hermes internally calls the agent (check with `hermes agents list` or your Hermes config). |
| `BGOS_AGENTS_JSON` | — | No (alternative to `BGOS_AGENTS`) | Richer JSON-list format: `'[{"agent_route":"default","name":"Hermes","description":"..."}]'`. Use when display names contain commas/colons or you want a description. |
| `BGOS_ALLOW_ALL_USERS` | `false` | **Yes** (unless you set `BGOS_ALLOWED_USERS`) | The fork's `BasePlatformAdapter._is_user_authorized` rejects all inbound messages unless this is `true`, OR the sender's Clerk user_id is in `BGOS_ALLOWED_USERS`. Without this, your messages silently never reach Hermes. |
| `BGOS_ALLOWED_USERS` | — | No (alternative) | Comma-separated Clerk user IDs authorized to chat with this Hermes. |
| `BGOS_POLL_INTERVAL` | `5` | No | Seconds between REST-poll backup checks for inbound messages. Only relevant while the WS push path has server-side gaps. Set to `0` to disable polling once WS push is reliable. |
| `BGOS_SCOPE_REFRESH_COOLDOWN` | `10` | No | Seconds between hot-refresh `whoami` calls when inbound arrives for an unknown `assistant_id` (i.e. an agent exposed after the gateway started). Lower = faster recovery; higher = less backend load. |
| `HERMES_HOME` | `~/.hermes` | No | Root for Hermes config + secrets + the persisted `bgos_last_id` cursor. **For a named-profile Hermes install, point this at the profile dir** (e.g. `~/.hermes/profiles/david`) so the secrets file, the `bgos_last_id` cursor, and the systemd `EnvironmentFile=` all resolve under the same root. |

**Minimum working `.env`:**
```bash
BGOS_AGENTS=default:Hermes
BGOS_ALLOW_ALL_USERS=true
```

---

## First-time pairing

1. In BGOS → **Integrations** → ⚡ **Hermes card** → **"Connect a new Hermes server"**. Copy the generated code. `BGOS-XXXX-XX` throughout this README is a **placeholder** — your real code looks like `BGOS-7F3A-2K`. It **expires in 10 minutes**; generate a fresh one if it lapses.

2. On the Hermes server, run — using the same Python that runs Hermes:
   ```bash
   <hermes-python> -m hermes_channel_bgos.pair_cli BGOS-XXXX-XX --device-label <this-server-hostname>
   ```
   Or, if `hermes-pair-bgos` is on the same PATH as Hermes:
   ```bash
   hermes-pair-bgos BGOS-XXXX-XX --device-label <this-server-hostname>
   ```
   Expected output: `Paired. Secret written to ~/.hermes/secrets/bgos.json` (mode 0600).

   Two optional flags make this smoother:
   - `--agents "default:Hermes"` (or a comma list) publishes the agent catalog **at pair time**, so the agents are tickable in the Integrations UI before the gateway even starts.
   - `--wait-for-exposure` then polls until you tick an agent in BGOS and prints the bound assistants — handy when scripting the whole flow:
     ```bash
     hermes-pair-bgos BGOS-XXXX-XX --device-label <host> --agents "default:Hermes" --wait-for-exposure
     ```

3. Back in BGOS → Integrations → the Hermes card shows your server under **Paired devices**. With the catalog published (via `--agents` or once the gateway connects), the **"Pick which agents to expose"** checklist populates; tick what you want, click Save, and those agents appear as assistants in your sidebar. Exposing an agent *after* the gateway is already running is fine — the adapter hot-loads new agents automatically (≥0.8.0), no restart required. Verify any time with `hermes-bgos-doctor`.

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
Fix: set `BGOS_AGENTS=<route>:<Display Name>` in the systemd env. Restart. Re-check logs for `BGOS catalog pushed: <route>:<Name>`.

**BGOS shows the server connected and the agent selected, but messages get no reply; log shows `inbound for unknown assistant_id=<id>`.** The agent was exposed *after* the gateway started, so the running adapter hadn't cached that assistant id. On **≥0.8.0** the adapter hot-refreshes the pairing scope on the first such message and self-heals within ~one poll cycle — no restart needed (tune via `BGOS_SCOPE_REFRESH_COOLDOWN`). On older versions, restart the gateway once. Confirm with `hermes-bgos-doctor` — the `pairing_live` line lists the exposed assistants the adapter can see.

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
- `StateStore` (`state_store.py`) — in-process state: assistant→route map, retry cache, conversation bindings. Not persisted; rebuilt from `whoami()` + REST backfill on reconnect. When inbound arrives for an unknown `assistant_id`, the adapter re-runs `whoami()` and reconciles the map in place (hot-refresh), so agents exposed after startup work without a restart.
- `bgos_last_id` file (`$HERMES_HOME/bgos_last_id`) — persisted cursor for the last seen message id. Monotonically advances. Prevents history-replay on restart.
- `commands_sync.py` — merges Hermes's native slash manifest with the 3 bridge-local commands, pushes via `PUT /integrations/assistants/:id/commands`.
- `agents.py` — shared `route:Display Name` spec parser + env enumeration; the single source of truth used by the adapter's catalog push, the pair CLI's `--agents`, and the doctor.
- `pair_cli.py` — the `hermes-pair-bgos` console script (`--agents` publishes the catalog at pair time; `--wait-for-exposure` polls until you tick agents).
- `doctor.py` — the `hermes-bgos-doctor` console script. Non-interactive health check (package, fork patch, pairing token, env, catalog, live `whoami`, gateway process) with inline fixes; `--json` for scripted/agent use, exit 1 on any FAIL.
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

Expected on v0.5.0: **153 passed, 1 skipped**. The reconnect test needs a real BGOS backend.
