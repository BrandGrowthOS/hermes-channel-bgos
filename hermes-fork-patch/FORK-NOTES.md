# Fork touch-points — what changes and why

Source of truth: upstream's `gateway/platforms/ADDING_A_PLATFORM.md`. Line numbers are **approximate**, grounded in what the research agent read on `main` on 2026-04-23 — verify against the actual file contents at patch-time (Phase 4).

Goal: each entry should be the smallest possible change to register BGOS as a platform. If a change grows beyond "add one line" or "add one branch", we've drifted from the design and should reconsider.

Total expected diff: ~80 lines added, 0 removed, across 16 files.

## The 16 touch-points

| # | File | Change | Rough location |
|---|---|---|---|
| 1 | `gateway/config.py` | Add `BGOS = "bgos"` to the `Platform` enum. | Lines 48–69 (the Platform(Enum) class body) |
| 2 | `gateway/run.py` | Add an `elif platform == Platform.BGOS: from gateway.platforms.bgos import BGOSAdapter; return BGOSAdapter(config)` branch to `GatewayRunner._create_adapter`. | ~Line 2737 |
| 3 | `gateway/run.py` | Register BGOS in the first auth/feature map (one line) referenced near the factory call-site. | ~Line 2075 — grep for existing `Platform.TELEGRAM:` entries near that line |
| 4 | `gateway/run.py` | Register BGOS in the second auth/feature map (one line). | ~Line 2463 — same idea |
| 5 | `gateway/session.py` | Add BGOS to the `SessionSource` enum / source-type map. Mirror whatever `MATRIX` or `TELEGRAM` look like. | Search for `class SessionSource` + follow the entries |
| 6 | `agent/prompt_builder.py` | Add a BGOS entry to the `PLATFORM_HINTS` dict. Hint text should be short — something like `"You are speaking to the user through BGOS, a mobile-first chat app. Keep replies concise and use markdown sparingly."` | Search for `PLATFORM_HINTS` |
| 7 | `toolsets.py` | Add BGOS to the platform-availability map for toolsets (if Hermes gates any toolsets by platform). Default: make all toolsets available — add BGOS wherever TELEGRAM or MATRIX appear. | Search for `Platform.TELEGRAM` in `toolsets.py` |
| 8 | `cron/scheduler.py` | Add BGOS to the platform dispatch so scheduled/cron jobs can deliver via BGOS. | Search for `Platform.TELEGRAM` in the scheduler |
| 9 | `tools/send_message_tool.py` | Add BGOS to the platform whitelist for the `send_message` tool so the agent can send messages to itself / other BGOS chats. | Search for `Platform.TELEGRAM` in that file |
| 10 | `tools/cronjob_tools.py` | Add BGOS to the cronjob delivery platform whitelist. | Search for `Platform.TELEGRAM` |
| 11 | `gateway/channel_directory.py` | Register BGOS in the channel-directory metadata map (used by the status CLI + agent prompt to describe available channels). Provide a name, description, and icon ref. | Search for `Platform.TELEGRAM` |
| 12 | `hermes_cli/status.py` | Add BGOS to the status-banner platform list so `hermes status` mentions it when connected. | Search for `Platform.TELEGRAM` |
| 13 | `hermes_cli/gateway.py` | Add BGOS as a platform choice in the setup wizard (`hermes setup` or equivalent). | Search for `Platform.TELEGRAM` in that file |
| 14 | `hermes_cli/platforms.py` | If this file exists and registers platforms for CLI management commands, add BGOS. | Check if file exists; if not, skip |
| 15 | `agent/redact.py` | Add any BGOS-specific redaction rules. For v1, just add the platform name string to whatever map exists. Likely one-line change. | Search for `Platform.TELEGRAM` |
| 16 | `gateway/platforms/bgos.py` | **NEW file.** Drop in `gateway-platforms-bgos.py` from this directory verbatim (renaming). It's a 5-line shim that imports `BGOSAdapter` from the vendor package. | n/a |

## Shim contents (touch-point #16)

See `gateway-platforms-bgos.py` in this directory — that's the file to drop in.

## Patch discipline

1. **One commit.** All 16 changes land in a single commit on the fork's `bgos-integration` branch.
2. **Commit message:** `feat: add BGOS as a channel platform (via hermes-channel-bgos vendor pkg)`.
3. **No BGOS business logic in the fork.** If a touch-point requires anything more than registration boilerplate + a string literal `"bgos"` / `"BGOS"`, stop — the right home for that code is the vendor package.
4. **No upstream PR yet.** The fork is private. Upstream PR is Phase 4+ and gated on BGOS's public launch.
5. **Keep the shim import minimal.** `from hermes_channel_bgos.bgos_adapter import BGOSAdapter` only. Any helper import pulls BGOSAdapter into the import chain at Hermes startup — a failing install of the vendor package would then brick Hermes. Keep this boundary clean.

## When a touch-point turns out to be wrong

If a file listed above doesn't exist on the upstream `main` at patch time, or its shape differs from what the research agent reported, **don't improvise**:

1. Skip that touch-point and continue with the others.
2. Record the discrepancy at the bottom of this file (`## Drift from upstream`).
3. Decide at the end whether the missing registration breaks anything material (cron delivery, setup wizard, etc.). If so, grep for the feature in the upstream repo to find the correct current location.

Better to ship 15 correct registrations and one TODO than 16 guesses that one of which breaks silently.

## Drift from upstream

Patch produced 2026-04-23 (Phase 4 Task 11).

- **Upstream base:** `NousResearch/hermes-agent` `main` @ `b6ca3c28dc434d1d0dca3bd2a029f394014eefbc`
  (commit: "Merge pull request #14640 from NousResearch/bb/fix-tui-glyph-ghosting")
- **Hermes version at patch time:** `0.10.0` (from `pyproject.toml`)
- **Patch artifact:** `0001-bgos-integration.patch` — 11 files changed, 74 insertions, 3 deletions

### Per-touch-point status

| # | File | Status | Notes |
|---|---|---|---|
| 1 | `gateway/config.py` | applied cleanly | Added `BGOS = "bgos"` to `Platform` enum. Also added a BGOS block to `_apply_env_overrides` reading `BGOS_API_KEY`, `BGOS_BACKEND_URL`, `BGOS_HOME_CHANNEL` — this is standard registration per the checklist §2. `_token_env_names` (line 803) not updated because BGOS auths via `api_key`, not `token`; `get_connected_platforms()` already handles `api_key` generically. |
| 2 | `gateway/run.py` — `_create_adapter` | applied cleanly | Factory branch lives near line 2890 (end of elif chain before `return None`), not 2737 as the original FORK-NOTES approximated. |
| 3 | `gateway/run.py` — first auth map | applied cleanly | `platform_env_map` at line ~2920 (was ~2075 in FORK-NOTES — map moved). |
| 4 | `gateway/run.py` — second auth map | applied cleanly | `platform_allow_all_map` at line ~2941 and a third `platform_env_map` at line ~3074 in `_resolve_unauthorized_dm_behavior`. Both updated. Third map wasn't in original FORK-NOTES — **new touch-point added by upstream**. |
| 5 | `gateway/session.py` | **skipped (no change needed)** | Current upstream shape uses `Platform` enum directly in `SessionSource.platform`; there is no separate `SessionSource` enum / source-type map to register into. BGOS needs no extra identity fields (no phone-number-plus-UUID pattern), so just adding BGOS to `Platform` is sufficient. Drift from FORK-NOTES's description but no risk. |
| 6 | `agent/prompt_builder.py` | applied cleanly | Added `"bgos"` entry to `PLATFORM_HINTS` with the specified hint text. |
| 7 | `toolsets.py` | applied cleanly | Added `hermes-bgos` toolset and included it in the `hermes-gateway` composite. |
| 8 | `cron/scheduler.py` | applied cleanly | Added `"bgos"` to `_KNOWN_DELIVERY_PLATFORMS` AND `"bgos": Platform.BGOS` to the `platform_map` in `_deliver_result`. The `_KNOWN_DELIVERY_PLATFORMS` frozenset wasn't in original FORK-NOTES — **new touch-point added by upstream** (validates user-supplied deliver targets). |
| 9 | `tools/send_message_tool.py` — `platform_map` | applied cleanly | Registered BGOS in the tool's platform_map. |
| 9b | `tools/send_message_tool.py` — `_send_to_platform` routing | **skipped by design** | Implementing `_send_bgos()` + an elif routing branch would require BGOS business logic (backend HTTP client, auth) in the fork. Per patch discipline rule #3, this code lives in the vendor package. Consequence: `send_message_tool` + cron delivery for BGOS will return `"Direct sending not yet implemented for bgos"` until the vendor package adds a route (likely via an adapter.send() delegation from the gateway runner path). Documented as a follow-up. |
| 10 | `tools/cronjob_tools.py` | applied cleanly | Added `'bgos:<chat_id>'` to the `deliver` parameter description's example list. The checklist said "update description" only — no platform enum map exists here. |
| 11 | `gateway/channel_directory.py` | **skipped (no change needed)** | Current upstream shape auto-iterates `for plat in Platform:` and falls through to `_build_from_sessions()` for any platform not in the skip list. BGOS inherits session-based discovery for free from the enum addition. FORK-NOTES mentioned "name, description, icon ref" — no such map exists in the current file. Drift from FORK-NOTES but no functional gap. |
| 12 | `hermes_cli/status.py` | applied cleanly | Added `"BGOS": ("BGOS_API_KEY", "BGOS_HOME_CHANNEL")` to the `platforms` dict. |
| 13 | `hermes_cli/gateway.py` | applied cleanly | Added a full `_PLATFORMS` entry with `key`, `label`, `emoji`, `token_var`, `setup_instructions`, and `vars`. No custom `_setup_bgos()` — the standard flow suffices since pairing is done externally via `hermes-pair-bgos`. |
| 14 | `hermes_cli/platforms.py` | applied cleanly | File exists. Registered `("bgos", PlatformInfo(label="📱 BGOS", default_toolset="hermes-bgos"))`. |
| 15 | `agent/redact.py` | **skipped (no change needed)** | BGOS uses opaque chat IDs (bigints) and Clerk user IDs, not phone numbers or tokens that need regex redaction. Per ADDING_A_PLATFORM.md §14 "If your platform uses sensitive identifiers (phone numbers, etc.)" — not applicable. No per-platform map exists to register into either; redact.py is all regex-based. |
| 16 | `gateway/platforms/bgos.py` | applied cleanly | Shim file copied verbatim from `gateway-platforms-bgos.py`. |

### Smoke-test output

```
$ python -c "from gateway.config import Platform; assert Platform.BGOS.value == 'bgos'; print('enum OK')"
enum OK: Platform.BGOS = bgos

$ python -c "from gateway.platforms.bgos import BGOSAdapter; print('shim OK:', BGOSAdapter.__name__)"
shim parsed OK, fails at inner import as expected: No module named 'hermes_channel_bgos'
```

Both outcomes expected per the README — the shim only resolves once `hermes-channel-bgos` is installed.

### New touch-points discovered in upstream that weren't in the original 16

1. **Third `platform_env_map` dict in `_resolve_unauthorized_dm_behavior`** (`gateway/run.py` ~line 3074) — registered.
2. **`_KNOWN_DELIVERY_PLATFORMS` frozenset in `cron/scheduler.py`** — a security validator for user-supplied deliver targets. Registered.
3. **`_HOME_TARGET_ENV_VARS` dict in `cron/scheduler.py`** (~line 54) — maps platform names to home-channel env vars. **Not registered** — BGOS can still be set via `BGOS_HOME_CHANNEL` (we wire that in `gateway/config.py`), but bare `deliver="bgos"` without home-channel lookup will require the vendor package to handle it or `_KNOWN_DELIVERY_PLATFORMS` alone to suffice. Flagged as a follow-up if BGOS users report "cron delivery silently drops."

> Updated 2026-04-24: PLATFORM_HINTS BGOS entry expanded from a one-line placeholder to a ~450-word agent-facing capabilities doc (markdown, MEDIA: file syntax, inbound files[], approvals, slash commands, NOT-YET-WIRED status for inline buttons + ask_user_input). `cron/scheduler.py` `_HOME_TARGET_ENV_VARS` now includes `bgos: "BGOS_HOME_CHANNEL"` so `deliver="bgos"` resolves to the BGOS home chat instead of silently falling back to another platform.
>
> Updated 2026-04-24 (round 2): Inline buttons now WIRED via `[[BGOS_BUTTONS]]...[[/BGOS_BUTTONS]]` marker block. PLATFORM_HINTS section rewritten from "NOT YET WIRED" to full syntax docs (pipe-separated label|value, max 6, click delivery via `inbound_click` WS event → synthetic user MessageEvent). `ask_user_input` modal is still NOT YET WIRED — documented as a follow-up. Corresponding adapter changes live in vendor pkg v0.3.0; backend change emits `inbound_click` to `assistant:<id>` on every paired-assistant button click.
>
> Updated 2026-04-25: Inbound file surfacing now WIRED. Backend `emitInboundMessage` payload's files entries gain `url` (presigned S3 GET, 1h TTL) and `dataUri` (inline base64 for <500KB files). Adapter inlines attachments into the agent-visible text — images as markdown image syntax (vision models pick them up automatically), other files as labeled link lines under `## Attachments from user`. PLATFORM_HINTS "Receiving files from the user" section rewritten to describe the actual surfacing path. Vendor pkg v0.4.0 + backend SendMessageService change ship together.
>
> Updated 2026-05-03: **Cross-channel agent-to-agent (P2P discussion) WIRED.** Hermes agents can now collaborate with the user's other BGOS assistants via four bridge-local slash commands (`/peers`, `/peer-status`, `/peer-send`, `/peer-complete`) AND two inline marker blocks the agent embeds in its reply text:
>
> ```
> [[BGOS_PEER_SEND name="Hades" text="Can you create a bucket?" wait="false" turn="expecting_reply"]]
> [[BGOS_PEER_COMPLETE summary="Hades created bgos-dev-uploads in us-east-1"]]
> ```
>
> The adapter strips the markers from the user-visible reply, posts the cleaned reply, then dispatches the parsed directives against the new BGOS peer endpoints (`GET /api/v1/peers`, `POST /api/v1/peers/:id/send`, `POST /api/v1/peers/conversations/close`, etc.) using the new `X-Caller-Assistant-Id` header.
>
> PLATFORM_HINTS section needs a new **"Collaborating with other agents"** subsection appended — see `hermes-channel-bgos/docs/bgos-agent-capabilities.md` §11 for the agent-facing wording. Vendor pkg v0.5.0 ships the adapter changes (`bgos_api.py` peer methods, marker parser in `bgos_adapter._extract_peer_directives`, bridge-local handlers in `_handle_peer_bridge_local`). No upstream Hermes file changes — the four new slash commands are bridge-local in `commands_sync.BRIDGE_LOCAL_COMMANDS`.

## PLATFORM_HINTS — section to append (P2P discussion, 2026-05-03)

Add this block to the BGOS entry in `agent/prompt_builder.py`:

```
## Collaborating with other BGOS agents

The user may have other AI assistants on this BGOS account (e.g. an Ops
agent, a research agent, a domain specialist). You can DISCOVER them and
COLLABORATE with them in real time. The user sees the exchange unfold inline
as a minimalist "side conversation" card anchored to your reply.

When to use this:
- The user's question would be better answered by a peer agent on this
  account (e.g. "ask Hades to create that S3 bucket").
- A multi-step task has a hand-off — your peer is the domain owner.
- You want a quick check from a coworker agent without burning a turn.

When NOT to use this:
- A simple slash command would do.
- The peer is offline AND you need an answer in this turn.
- The user has not enabled the introduction (you'll see introduced=false in
  /peers — ASK the user "Want me to ask <peer>?" first; do not auto-send).

Discovery (via slash commands):
- `/peers` — lists peer assistants visible from your account, with an
  introduced ✓ / ✗ flag indicating whether the user has enabled this
  direction in the BGOS Agent Permissions matrix.
- `/peer-status <name|id>` — checks whether a peer is online + has an open
  conversation with you.

Sending — embed in your reply text (preferred over slash commands when YOU
the agent are driving the call):

  [[BGOS_PEER_SEND name="Hades" text="Can you create a bgos-dev-uploads
  bucket in us-east-1, public access blocked?" wait="false"
  turn="expecting_reply"]]

The adapter extracts the marker and dispatches to the peer's side-thread.
The user sees a SideConversationCard live-rendering each turn under YOUR
reply that contained the marker. `wait="true"` blocks until the peer
replies (max 85s — server cap; do NOT retry on timeout).

Closing — when the back-and-forth is complete, embed:

  [[BGOS_PEER_COMPLETE summary="Hades created bgos-dev-uploads in us-east-1
  with public access blocked"]]

The card flips from live (pulsing dot) to completed-collapsed with the
summary. If you don't close, the server auto-closes after 15 minutes idle
with a generic summary — always prefer your own real summary.

Pre-send pattern: send a "Looping in <peer>..." reply FIRST so the
SideConversationCard has a parent message to anchor against. The marker in
that same reply uses that reply's id as the parent.

The user controls who can talk to whom in BGOS Settings → Agent
Permissions. Asymmetric: Ava→Hades enabled doesn't imply Hades→Ava.
```

### Skipped touch-points summary

- **#5 SessionSource** — no change needed given current upstream shape
- **#9b send_message_tool routing** — needs vendor pkg logic, out of scope for fork
- **#11 channel_directory metadata map** — no such map in current upstream (auto-discovery covers us)
- **#15 redact.py** — BGOS has no sensitive identifiers to mask

### Documentation (checklist item #15 in ADDING_A_PLATFORM.md)

Upstream's §15 lists README / AGENTS.md / website docs updates. **Skipped** from this patch — those are upstream-facing docs and land with the public Phase 4+ upstream PR, not the private integration patch.

