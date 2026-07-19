# Hermes-BGOS PR queue reconciliation report

Date: 2026-07-19. Main at 0.19.0 (b8cf771, PR #29). Baseline suite on main: 516 passed, 1 skipped.
Work done in a fresh clone; every reconciled branch was force-pushed back to its original PR branch name, so the PRs on GitHub now show the reconciled state. No merges, no version bumps, no deploys were performed. Lint note: the repo has no ruff/flake8; CI checks are the fork-patch format validation (git apply --numstat) plus pytest. Both were run after every rebase, plus a compileall syntax pass and a conflict-marker grep.

## PR #14 (Jun 25): agent self-status, STATUS marker

Reconciled branch pushed to feat/agent-self-status (head cbdaf17). Suite after rebase: 516 passed, 1 skipped (the PR ships no tests, see gap below). Patch check: OK.

Classification:
- Still-valid (kept): BgosApi.patch_status (PATCH /api/v1/integrations/assistants/:id/status), the STATUS: line marker parse (_STATUS_LINE_RE + _parse_status_line) and its fail-open wiring in send(), and the fork-patch PLATFORM_HINTS "Setting your status" section (embedded hunk header count updated, validates with git apply --numstat).
- Already-on-main (dropped): the PR's whole capability-doc section "### 11. Agent self-status" (main already documents the capability as "Set my status", including a newer detail field), the PR's Gobot cheat-sheet section (main has a newer, richer one), the propagation-checklist rewrite, and the doc versioning note. The docs change was reduced to one line: the Hermes cheat-sheet "Set status" bullet now also documents the agent-side STATUS: marker syntax.
- Conflicts resolved: two content conflicts in bgos_adapter.py (main's [[BGOS_EVENT]] block and quiet-mode chat_style lines vs the PR's insertions), both resolved by keeping both sides in order; docs conflicts resolved by taking main and re-applying only the one-bullet augmentation.
- STOP items: none. Main's doc describes the capability as a direct API call; the marker is the idiomatic Hermes agent-facing surface (same family as [[BGOS_REPLY_TO]] and MEDIA:), complementary rather than contradictory.
- Gap to flag: the PR adds zero tests, so patch_status and _parse_status_line are uncovered. Recommend adding a small marker-parse + wire test before merge.

User test (BGOS app vs a Hermes agent):
1. Ask the agent: "Set your status to Reviewing invoices and reply done." The visible reply must contain no STATUS: line.
2. Check the agent's roster / command-center entry: the status line under its name reads "Reviewing invoices".
3. Say "clear your status"; the status line disappears.

## PR #17 (Jul 10): voice mint with the caller's OpenAI key

Reconciled branch pushed to fix/voice-per-user-openai-key (head 3abd462). Suite after rebase: 528 passed, 1 skipped. Patch check: OK.

Classification:
- Still-valid (kept, all of it): per-user key preference in _mint (payload.openaiApiKey wins, host env key is a standalone fallback), the allowlist log redaction (redact_voice_rpc_for_log, payload key NAMES only, never values), the gpt-realtime-2.1 default, and 12 new tests including never-log-the-key and never-return-the-key guards. Main had none of this.
- Already-on-main / dropped: the PR's version bump 0.16.0 to 0.16.1 (main is 0.19.0; the orchestrator bumps 0.20.0 once). Nothing else was superseded.
- Conflicts resolved: pyproject + __init__ version (kept main's 0.19.0), README env-var table (kept main's newer table incl. BGOS_CHAT_STYLE row, swapped in the PR's new BGOS_OPENAI_API_KEY description), capability-doc Gobot voice bullet (kept main's richer bullet, spliced in the per-user-key phrasing by exact-hunk replace).
- Two small follow-up commits added: (a) docs alignment, README + capability doc now state the gpt-realtime-2.1 default that the code commit set; (b) test fix, main's test_load_voice_env_precedence asserted the old default and failed after the model change, updated to 2.1. Without (b) the branch is red, this was the only suite failure seen in the whole reconciliation.
- STOP items: none.

User test:
1. Put your own OpenAI key in Home of Agents settings; voice-call the Hermes agent, the call connects and speaks.
2. Verify the usage lands on YOUR OpenAI dashboard, not the gateway owner's.
3. Grep the gateway log for your key (or send a malformed voice frame); the key must never appear, dropped-frame logs show payload key names only.

## PR #26 (Jul 17): version at connect + doctor_rpc

Reconciled branch pushed to feat/hermes-health-doctor (head 6acdb97). Suite after rebase: 531 passed, 1 skipped. Patch check: OK.

Classification (per the reconcile order: do not double-report versions):
- Reduced, not kept as-is: the version-at-connect commit. Main's #29 heartbeat (boot + every 6h) already covers version reporting cadence, so the PR's connect-time and reconnect-time heartbeat posts were DROPPED entirely. What the heartbeat did not cover was kept: the env facts. _daemon_env() (platform, python, hermes version, each capped at 64 chars) now rides main's existing 6h heartbeat as the HeartbeatDto env object. Exactly one reporting path remains; no double reporting.
- Kept in full (net-new): the doctor_rpc lane. WS handler for doctor_rpc frames on the pairing room ({rpcId, op:"run", payload:{}}), ack to POST /integrations/doctor-rpc/:rpcId/ack, in-process run of the existing doctor checks with a 45 s cap, result to /result with the {ok, payload:{result, checks[]}} / {ok:false, error:{code,message}} contract, in-flight dedupe, task cleanup on disconnect, plus 357 lines of tests. Verified against the real backend: BGOS origin/main ships doctor-rpc.controller.ts and the capability canon documents this exact frame and reply contract.
- Main bug found and fixed in passing: main's post_heartbeat had unused daemon_env / last_error STRING params producing {daemonEnv, lastError} which do not match the backend HeartbeatDto (top-level daemonEnv would be whitelist-stripped; a bare-string lastError would fail validation). Signature aligned to the DTO: env is an object, lastError is a {code, message, at} object. Latent-only on main (never called with those params), but it would have bitten the first caller.
- Tests: the PR's connect-time heartbeat tests were replaced by a reduced test_heartbeat_report.py (env-fact unit tests) and main's test_heartbeat.py was updated (boot beat now also carries env; optional-fields wire test uses the DTO shapes).
- STOP items: none, the ordered reduction resolved the overlap.

User test:
1. Open the Hermes agent's health card in the app and tap "Run checkup"; within about 45 s the check rows (OK / WARN / FAIL with fix hints) appear.
2. Tap it again immediately; the server's one-run-per-minute limit is surfaced instead of a second run.
3. After the next heartbeat (daemon restart is quickest), the pairing row shows daemon version plus env facts (platform / python / hermes).

## PR #27 (Jul 17): voice notes both ways

Reconciled branch pushed to feat/hermes-voice-notes (head 9c76622). Suite after rebase: 552 passed, 1 skipped. Patch check: OK.

Classification:
- Still-valid (kept, everything): inbound BGOS audio routed into hermes STT as VOICE events, the STT setup card with real download progress (eventMeta PATCH), spoken replies, voice passthrough tests, canon mirror + bundled hint, and the plan doc. 36 new tests.
- Already-on-main: nothing; the app half (BGOS backend PR #748, flags OFF) is the counterpart, not an overlap.
- Conflicts: none, the branch was based on #25 and rebased over #29 cleanly (all 5 commits applied without conflict).
- STOP items: none. Note the PR's own last two commits are a house-style dash cleanup, already aligned with the no-dash rule.

User test:
1. Send a voice memo to the Hermes agent in a BGOS chat; the agent answers the CONTENT of the memo (STT worked).
2. First time without an STT model: a Voice setup card appears and shows real download progress; when it completes, resend the memo.
3. Turn on the "Speaks replies" setting; the agent's next reply arrives with spoken audio.

## PR #28 (Jul 17): memory panel bridge

Reconciled branch pushed to feat/hermes-memory-panel (head f3af219). Suite after rebase: 570 passed, 1 skipped. Patch check: OK.

Classification:
- Still-valid (kept, everything): the memory_rpc WS lane in bgos_ws.py (alongside voice_rpc / doctor-style handlers), the new memory_bridge.py (list/add/replace/remove over the in-process MemoryStore plus search over SessionDB with owner scoping), the capability-doc mirror note, and 1,298 lines of tests (54 tests).
- Already-on-main: nothing. Backend counterpart is BGOS #750 (Memory panel, flag OFF).
- Conflicts: none, clean rebase over #29 (3 commits).
- STOP items: none.

User test:
1. Open the Hermes agent's Memory panel in the app (flag ON); it lists what the agent remembers.
2. Add an entry, edit one, remove one; reopen the panel, the changes persisted.
3. Search the panel for a phrase from an old chat; results come back owner-scoped (nothing from other users).

## Recommended merge order and cross-PR notes

Merge in queue order: #14, then #17, then #26, then #27, then #28. Each branch was reconciled against main INDEPENDENTLY, so after each merge the NEXT branch needs a quick rebase; expected friction is small and mechanical: bgos_adapter.py import block and parse chain (#14 vs #27), bgos_ws.py handler registration lists (#26 vs #28), and the docs Hermes cheat-sheet bullets (all of them). No semantic conflicts between the five.

- No PR is fully superseded; none recommended for closing. All five carry net-new, working, tested behavior (with #14's missing-tests caveat).
- Version: all branches sit on 0.19.0; the orchestrator bumps 0.20.0 once after the last merge, then regenerates nothing (the fork patch is already consistent on every branch).
- Pre-existing doc drift noticed (not fixed, out of scope): main's capability doc no longer documents the reply-quote marker ([[BGOS_REPLY_TO]]) anywhere although the adapter still supports it; it was lost in an earlier doc resync.
- The live gateway at /Users/fitecho/hermes-channel-bgos was not touched.
