# Hermes fork patch — apply & maintain

> **Status (2026-04-24):** partial. `FORK-NOTES.md` enumerates the 16 touch-points from Hermes's `gateway/platforms/ADDING_A_PLATFORM.md`; `gateway-platforms-bgos.py` holds the 5-line shim that lives at `gateway/platforms/bgos.py` in the fork. The actual `git format-patch` artifact is produced at the start of Phase 4 (E2E) once we have a real Hermes clone to edit against. This is intentional — the patch lines depend on Hermes's current `main`, and we want to produce+apply+verify in one sitting.

## What this directory is for

Option D of the distribution decision (see `../docs/distribution-decision.md`) says:

- A **private fork** of `NousResearch/hermes-agent` holds ~80 lines of BGOS registration boilerplate across 16 files.
- All real BGOS logic lives in this `hermes-channel-bgos/` pip package.
- Nightly `git rebase upstream/main` keeps the fork current.

This directory is the home for the fork's BGOS-specific content so it's reviewable alongside the adapter code even though it'll ultimately be applied in a separate repo (Kc's private `brandgrowthos/hermes-agent-bgos` or whatever he names it).

## How the patch gets produced (Phase 4 Task)

1. **Clone Hermes to a sibling directory** (not inside this monorepo):
   ```
   cd ~/src/                  # or wherever
   git clone https://github.com/NousResearch/hermes-agent
   cd hermes-agent
   git checkout -b bgos-integration
   ```

2. **Apply each of the 16 touch-points** following `FORK-NOTES.md`. Drop `gateway-platforms-bgos.py` from this directory into the fork at `gateway/platforms/bgos.py`.

3. **Smoke-test the fork** with `hermes-channel-bgos` installed:
   ```
   pip install -e .                                  # Hermes in editable mode
   pip install -e <bgos-monorepo>/hermes-channel-bgos
   python -c "from gateway.config import Platform; assert Platform.BGOS.value == 'bgos'"
   python -c "from gateway.platforms.bgos import BGOSAdapter; print(BGOSAdapter.__name__)"
   ```

4. **Commit on the fork**:
   ```
   git add -A
   git commit -m "feat: add BGOS as a channel platform (via hermes-channel-bgos vendor pkg)"
   ```

5. **Export the patch back into this monorepo**:
   ```
   git format-patch upstream/main --stdout > <bgos-monorepo>/hermes-channel-bgos/hermes-fork-patch/0001-bgos-integration.patch
   ```

6. **Commit the patch artifact in the BGOS monorepo** (on this same `feature/hermes-bgos-integration` branch) so it's reviewable in the PR.

## How the patch gets applied (end-user install)

See the top-level `hermes-channel-bgos/README.md` for the operator-facing walkthrough. The short version:

```
git clone <kc-private-fork> ~/hermes-agent
cd ~/hermes-agent
pip install -e .
pip install -e <bgos-monorepo>/hermes-channel-bgos
hermes-pair-bgos BGOS-XXXX-XX --device-label <label>
```

## How the fork stays current with upstream

Nightly GitHub Action in the fork:

```yaml
name: rebase-on-upstream
on:
  schedule:
    - cron: "0 3 * * *"       # 03:00 UTC daily
  workflow_dispatch: {}
jobs:
  rebase:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0, token: ${{ secrets.GH_PAT }} }
      - name: Rebase onto upstream
        run: |
          git remote add upstream https://github.com/NousResearch/hermes-agent
          git fetch upstream main
          git config user.name  "bgos-bot"
          git config user.email "bgos-bot@users.noreply.github.com"
          git rebase upstream/main || {
              echo "::error::rebase conflict — manual resolution needed"
              exit 1
          }
          git push --force-with-lease
```

Conflicts only happen when upstream touches one of our 16 patched lines — rare. When they do, Kc gets an email from GH Actions and resolves in a local checkout of the fork.

## Why not an install-time patcher?

Considered, rejected. See `../docs/distribution-decision.md` for the full comparison, but the short version: 16 touch-points is too many to reliably regex/AST patch across every upstream change; patches silently misapply; users don't find out until something like cron delivery breaks. A git rebase is an honest, visible conflict you resolve once.
