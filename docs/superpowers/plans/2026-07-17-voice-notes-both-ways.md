# Voice Notes, Both Ways (P8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inbound HOAI voice notes route into Hermes's real STT pipeline (transcript reaches the agent as spoken words, file still available), a truthful "voice is getting ready" setup card covers the first-run Whisper model download, a per-agent "Speaks replies" setting drives Hermes's persisted /voice modes, and a flag-gated Voxor premium-transcription placeholder row ships inert.

**Architecture:** The adapter never transcribes and never synthesizes. For inbound, it mirrors the Telegram adapter contract: download audio bytes to the local Hermes audio cache, set `media_urls`/`media_types` and `message_type=VOICE` on the gateway MessageEvent, and the gateway's own `transcribe_audio` pipeline (local faster-whisper first, provider fallback) fires. For outbound, Hermes already persists per-chat `/voice off|on|tts` modes and calls the adapter's existing `send_voice()` (which posts a real BGOS voice bubble with a caption text slot); the app setting is a friendly face on that switch, sent as a `/voice` slash command. The setup card is an adapter-emitted `event` message PATCH-edited with progress computed only from real byte counts observed in the HuggingFace cache plus a real Content-Length total.

**Tech Stack:** Python 3.11+ adapter (pytest, aiohttp, python-socketio mocks), Expo/React Native frontend (ts-jest pure-logic tests, `npx tsc --noEmit`), NestJS backend (jest) for the served capability canon.

## Global Constraints

- Zero em dashes and zero en dashes in every added or changed line (code, comments, copy, docs). House rule from Kc.
- All new user-visible frontend surfaces are flag-gated and default OFF: `EXPO_PUBLIC_VOICE_REPLIES === "1"`, `EXPO_PUBLIC_VOXOR_TRANSCRIPTION_TEASER === "1"`.
- Adapter behavior defaults ON (Telegram parity, like quiet mode's tidy default) with env kill switches: `BGOS_VOICE_NOTES=off`, `BGOS_VOICE_SETUP_CARD=off`.
- Progress numbers must derive from actual bytes on disk and a real HTTP Content-Length; when the total is unknown, show downloaded MB only, never a synthetic percent.
- No new backend field for the speak-replies setting. The only backend change is the served capability canon text.
- Do not touch the quiet-mode send-path branches (`classify_robot_talk` gating, `_quiet_failsafe_fire`, `strip_voice_prefixes`) beyond reading them; no regression to tidy mode.
- Do not modify `SHOW_VOICE_NOTE_MIC` or the composer; the hidden mic is a recorded design decision.
- No version bumps, no publishes, no merges. Branch `feat/hermes-voice-notes` in both repos.
- No formatter churn: touch only the lines the change needs.
- The full Voxor flow is a separate approved-pending design (Dutify XQRffR2geZ-556). The placeholder row must have no link, no paywall logic, no Voxor code.

## Baselines (verified 2026-07-17)

- Adapter: `.venv/bin/python -m pytest -q` = 496 passed, 1 skipped (497 collected).
- Frontend: `npx tsc --noEmit` exit 0; `npx jest --silent` = 4104 passed, 356 suites.
- Backend: `npx jest capability-canon --silent` = 8 passed, 2 suites.

---

### Task 1: Adapter inbound voice routing (voice_notes.py)

**Files:**
- Create: `src/hermes_channel_bgos/voice_notes.py`
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (`__init__` ~967, `_handle_inbound` ~3695-3787)
- Modify: `tests/mocks/mock_hermes.py` (add `MessageType.VOICE`, `media_urls`/`media_types` fields)
- Test: `tests/test_voice_notes.py`, extend `tests/test_bgos_adapter_inbound.py`

**Interfaces:**
- Consumes: inbound `files[]` entries `{filename, mime, url? | dataUri?}` (bgos_adapter `_format_inbound_files` contract), `_GatewayMessageType`, `_GatewayMessageEvent`.
- Produces: `voice_note_ext(mime: str) -> str | None`; `is_voice_note_candidate(f: dict) -> bool`; `async collect_voice_notes(files: list[dict], fetch_url, cap: int) -> list[tuple[str, str]]` returning `[(local_path, mime)]`; `voice_notes_enabled() -> bool`; constant `MAX_VOICE_NOTE_BYTES = 25 * 1024 * 1024`.

Design points the implementation must hit:
- Mime map: `audio/mp4|audio/x-m4a|audio/m4a -> .m4a`, `audio/aac -> .aac`, `audio/mpeg|audio/mp3 -> .mp3`, `audio/ogg -> .ogg`, `audio/opus -> .ogg` (Hermes STT accepts `.ogg`, not `.opus`), `audio/webm -> .webm`, `audio/wav|audio/x-wav -> .wav`, `audio/flac -> .flac`. Anything else: not a candidate.
- Bytes come from `dataUri` (base64 decode, reject over cap) or `url` (streamed GET, abort past cap, 20s timeout, reuse the adapter's existing aiohttp session pattern).
- Cache via `gateway.platforms.base.cache_audio_from_bytes(data, ext=...)` when importable; fallback writes `$HERMES_HOME/cache/audio/bgos-<uuid><ext>` with 0o700 dir.
- In `_handle_inbound`, after `agent_visible_text` is built and only when the event is not a slash command: collect voice notes (max 5 files); if any collected and `getattr(_GatewayMessageType, "VOICE", None)` exists, set the gateway event's `message_type` to VOICE and set `media_urls`/`media_types` on it. The attachments markdown section stays in the text unchanged (file still available to the agent).
- Every failure path (bad mime, oversized, fetch error, cache error, old Hermes without VOICE, kill switch `BGOS_VOICE_NOTES=off`) degrades to today's link-only behavior. Never raise out of `_handle_inbound`.

- [ ] **Step 1:** Write failing tests: mime map units; candidate detection; inbound audio url message dispatches gateway event with `message_type == VOICE`, `media_urls` non-empty pointing at a real cached file whose bytes match the served bytes, `media_types == [mime]`, and text still containing the link line; dataUri variant; oversized fetch falls back to link-only TEXT; non-audio file stays TEXT; kill switch stays TEXT; slash command with audio stays COMMAND; VOICE missing from MessageType degrades to TEXT.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_voice_notes.py -q` and confirm failures reference missing module/attributes.
- [ ] **Step 3:** Implement `voice_notes.py` plus the `_handle_inbound` wiring and mock extensions.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` fully green (497+ tests).
- [ ] **Step 5:** Commit `feat(voice-notes): route inbound BGOS audio into hermes STT as VOICE events`.

### Task 2: Adapter STT setup card with real progress (stt_setup.py)

**Files:**
- Create: `src/hermes_channel_bgos/stt_setup.py`
- Modify: `src/hermes_channel_bgos/bgos_adapter.py` (instantiate in `__init__`, trigger in `_handle_inbound` after voice routing)
- Test: `tests/test_stt_setup.py`

**Interfaces:**
- Consumes: `bgos_api.post_send_message(..., message_type="event", event_meta=...)` and `post_edit_message(..., event_meta=...)` via adapter-injected async callables.
- Produces: `class SttSetupNotifier` with `async maybe_notify(chat_id: str) -> None`; helpers `hf_hub_root() -> Path` (respects `HF_HOME`, `HUGGINGFACE_HUB_CACHE`), `model_dir(root, model) -> Path` (`models--Systran--faster-whisper-<model>`), `is_model_cached(dir) -> bool` (any `snapshots/*/model.bin`), `incomplete_bytes(dir) -> int` (sum `blobs/*.incomplete`), `format_progress(downloaded: int, total: int | None) -> str`.

Design points:
- Fires only when: kill switch off, `faster_whisper` importable (`importlib.util.find_spec`), model (env `BGOS_STT_MODEL`, default `base`) not cached, and no notifier already active/completed this process.
- Total bytes from one HEAD to `https://huggingface.co/Systran/faster-whisper-<model>/resolve/main/model.bin` (follow redirects, read Content-Length). On failure total is None and copy shows downloaded MB only.
- Watcher loop with injected `sleep` and `clock`: poll disk every 1s; edit the card at most every 2.5s and only on byte/stage change; ready when `is_model_cached`; stalled after 90s without growth; hard stop at 15 minutes.
- Card contract: post `message_type="event"`, text = plain-words body, `event_meta = {"source": "voice_setup", "title": "Voice setup", "payload": {"progress": {"stage": "downloading"|"ready"|"stalled", "downloadedBytes": int, "totalBytes": int | null}}}`. Edits PATCH the same message with updated text and event_meta.
- Copy (exact, no dashes): initial "Getting ready to listen. Your agent is downloading a small speech model (about 150 MB) so it can understand voice notes. Typing works normally in the meantime, and the note you just sent will be heard once this finishes." progress "Downloading the speech model: 50% (74 of 148 MB)." or "Downloading the speech model: 74 MB so far." ready "Voice is ready. Your voice notes are understood from now on." stalled "The download paused. It will pick up again with your next voice note."
- Trigger from `_handle_inbound` via `asyncio.create_task`; never blocks or fails message dispatch.

- [ ] **Step 1:** Failing tests: no card when model cached; card posted once (dedupe on second memo); edits carry monotonically increasing `downloadedBytes` equal to fake blob sizes on disk and percent computed only when total known (fake HEAD injected); stall path; ready path flips stage and body; kill switch; `format_progress` exact strings.
- [ ] **Step 2:** Run and confirm failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite green.
- [ ] **Step 5:** Commit `feat(voice-notes): STT setup card with real download progress`.

### Task 3: Adapter passthrough tests, capability canon mirror, bundled hint

**Files:**
- Test: extend `tests/test_bgos_adapter_inbound.py` (or new `tests/test_voice_passthrough.py`), extend `tests/test_voice_adapter.py`
- Modify: `docs/bgos-agent-capabilities.md` (sections 5 and 6, Hermes cheat sheet)
- Modify: `src/hermes_channel_bgos/plugin.py` (bundled `BGOS_PLATFORM_HINT` media/receive lines ~340-348)
- Modify: `README.md` (env table: `BGOS_VOICE_NOTES`, `BGOS_VOICE_SETUP_CARD`, `BGOS_STT_MODEL`)

**Interfaces:**
- Consumes: `BRIDGE_LOCAL_COMMANDS` (must remain `{new, retry, status, quiet}`), existing `send_voice` (`isAudioMessage` + caption contract).
- Produces: doc text that Task 6's served canon mirrors keyword-for-keyword.

- [ ] **Step 1:** Failing/added tests: `/voice tts` slash event is NOT bridge-intercepted and dispatches as COMMAND with `command_name == "voice"` and args preserved (this is the adapter half of the spoken-reply setting on/off proof); `send_voice` with a caption posts `isAudioMessage: true` plus the caption as `text` (extend existing test if already close).
- [ ] **Step 2:** Run and confirm status.
- [ ] **Step 3:** Docs: section 6 gains "Voice notes: audio attachments are downloaded and transcribed through the host's speech to text pipeline; the transcript reaches the agent as quoted spoken words and the file link stays available. Formats: m4a, aac, mp3, ogg, opus, webm, wav, flac, up to 25 MB." Section 5 and the Hermes cheat sheet gain the speak-replies story: "/voice off | on | tts persists per chat; when on, replies to voice notes arrive as voice bubbles with the full text underneath; when tts, every reply does." Bundled hint in plugin.py updated in lockstep. README env rows added.
- [ ] **Step 4:** Full adapter suite green; `grep -n $'\u2014\|\u2013'|–' <changed files>` returns nothing.
- [ ] **Step 5:** Commit `docs(voice-notes): canon mirror, bundled hint, voice passthrough tests`.

### Task 4: Frontend EventCard real progress rendering

**Files:**
- Create: `frontend/expo-app/src/components/chat/eventCardProgress.ts`
- Modify: `frontend/expo-app/src/components/chat/EventCard.tsx`
- Test: `frontend/expo-app/src/components/chat/__tests__/eventCardProgress.test.ts`

**Interfaces:**
- Consumes: `eventMeta.payload.progress` shape from Task 2: `{stage, downloadedBytes, totalBytes}`.
- Produces: `extractEventCardProgress(payload: unknown): { stage: "downloading" | "ready" | "stalled"; percent: number | null; label: string } | null`. Percent is `Math.round(100 * downloadedBytes / totalBytes)` clamped 0..100, null when totalBytes missing/invalid. Label in plain words from the real numbers.

- [ ] **Step 1:** Failing tests mirroring `eventCardFormat.test.ts` style: valid downloading payload gives percent 50 for 74/148 MB byte inputs; missing total gives percent null and an "MB so far" label; ready/stalled stages; junk payloads give null; clamping.
- [ ] **Step 2:** Run `npx jest eventCardProgress` and confirm failure.
- [ ] **Step 3:** Implement helper; render in EventCard under the body text: a track View plus fill View at `width: percent%` when percent is non-null, label text always. Match existing theme color usage in the file.
- [ ] **Step 4:** `npx jest --silent` green; `npx tsc --noEmit` exit 0.
- [ ] **Step 5:** Commit `feat(chat): event card renders real progress for voice setup`.

### Task 5: Frontend Voice replies setting + Voxor placeholder

**Files:**
- Modify: `frontend/expo-app/src/env.ts` (two opt-in flags)
- Create: `frontend/expo-app/src/components/voice/speakReplies.ts` (mode type, per-assistant AsyncStorage pref store following the sendSparkPref idiom, and `toVoiceCommand(mode)` mapping)
- Modify: `frontend/expo-app/src/components/modals/EditAssistantModal.tsx` (voice step section)
- Test: `frontend/expo-app/src/components/voice/speakReplies.test.ts`

**Interfaces:**
- Consumes: `sendMessageMutation` slash-command contract (`messageType: "slash_command"`, `commandName`, `commandArgs`, text `/voice <arg>`), assistant integration discriminator, assistant chat id from the assistants-with-chats data already in the modal's reach.
- Produces: `type SpeakRepliesMode = "off" | "voice" | "always"`; `toVoiceCommand(mode)` returning `{ text, messageType, commandName, commandArgs }` with `off -> off`, `voice -> on`, `always -> tts`; `getSpeakRepliesPref(assistantId)`, `setSpeakRepliesPref(assistantId, mode)`, `useSpeakRepliesPref(assistantId)`, `__resetSpeakRepliesForTests()`.

Design points:
- Section renders only when `VOICE_REPLIES_ENABLED` and the assistant is a Hermes-integration assistant. Three radio-style rows: "Text only" / "When I send a voice note" / "Always", with one-line plain descriptions. Local pref is the display state; on Save with a changed mode, send the `/voice` command to the assistant's chat; if the assistant has no chat yet, show the rows disabled with "Start a chat with this agent first, then this switch can reach it."
- Voxor placeholder inside the same section, gated separately by `VOXOR_TRANSCRIPTION_TEASER_ENABLED`: an inert disabled row, label "Faster, more accurate transcription", sub-label "Coming soon". No onPress, no link, no paywall logic, no Voxor import. Code comment above it: `// Voxor premium transcription placeholder. The approved flow is a separate build tracked in Dutify XQRffR2geZ-556 (see voxor-hoai-premium-flow.md). Keep this row inert until that ships.`

- [ ] **Step 1:** Failing tests: `toVoiceCommand("always")` gives `/voice tts` + `commandArgs: "tts"`; `toVoiceCommand("off")` gives `/voice off` (the spoken-reply on and off proof, frontend half); `voice -> on`; pref store set/get/default/per-assistant isolation/reset.
- [ ] **Step 2:** Run `npx jest speakReplies` and confirm failure.
- [ ] **Step 3:** Implement store + mapping + modal section + flags.
- [ ] **Step 4:** `npx jest --silent` green; `npx tsc --noEmit` exit 0; changed-file dash grep clean.
- [ ] **Step 5:** Commit `feat(voice): Speaks replies setting over hermes voice modes + Voxor teaser placeholder (flags OFF)`.

### Task 6: Backend served capability canon update

**Files:**
- Modify: `backend/src/integrations/capability-canon.ts` (Hermes inbound files and media sections)
- Test: existing `backend/src/integrations/capability-canon*.spec.ts` must stay green against the Task 3 mirror.

**Interfaces:**
- Consumes: Task 3's mirror wording in `hermes-channel-bgos/docs/bgos-agent-capabilities.md`.
- Produces: served canon text describing inbound voice-note transcription and the speak-replies voice modes, keyword-consistent with the mirror so the drift guard passes.

- [ ] **Step 1:** Update the canon text (same capability facts as the mirror; keep section structure so the sync spec's counts hold).
- [ ] **Step 2:** `npx jest capability-canon --silent` green (run from `backend/`).
- [ ] **Step 3:** Commit `docs(canon): hermes voice notes inbound STT + speak replies modes`.

### Task 7: Verification, push, PRs

- [ ] Adapter: fresh full `.venv/bin/python -m pytest -q` count shown; targeted proof tests named in output.
- [ ] Frontend: `npx tsc --noEmit` exit 0; `npx jest --silent` totals shown.
- [ ] Backend: capability-canon suite shown.
- [ ] Dash scan over both repo diffs: `git diff origin/main | grep -c $'\u2014'` and en dash both 0.
- [ ] `git fetch origin && git rebase origin/main` in both worktrees if siblings landed; push `feat/hermes-voice-notes`; open one PR per repo; DO NOT merge.
- [ ] Write `/tmp/p8-voice-notes-report.md`; print WORKER DONE.
