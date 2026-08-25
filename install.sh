#!/usr/bin/env bash
# One-command installer for hermes-channel-bgos.
# Usage (what the HOAI app hands you, two plain lines):
#   curl -fsSL https://raw.githubusercontent.com/BrandGrowthOS/hermes-channel-bgos/main/install.sh -o hoai-hermes-install.sh
#   bash hoai-hermes-install.sh --pair-code BGOS-XXXX-XX
#
# The app used to hand out `BGOS_PAIR_CODE=<code> bash <(curl -fsSL <url>)`.
# That is valid POSIX and nothing else: a leading VAR=value is not a command in
# Windows PowerShell (it answers "is not recognized as the name of a cmdlet"),
# and <(...) process substitution does not exist there at all, so a Windows
# owner got a shell error that named nothing. Downloading first and then
# running the file removes both, and the pair code rides a normal flag.
#
# Options (all optional; the env vars below still work unchanged):
#   --pair-code BGOS-XXXX-XX   pair this server as part of the install
#   --assistant-id 1012        pin the pairing to one assistant
#   --agents 'route:Name'      the agent catalog to register (comma-delimited)
#   --device-label my-server   how this machine is labelled in the app
#   BGOS-XXXX-XX               a bare pair code also works as the first argument
# Optional env:
#   HERMES_INSTALL=/path/to/hermes-agent
#   HERMES_PYTHON=/path/to/hermes/python
#   BGOS_AGENTS="default:Hermes"
#   BGOS_PAIR_CODE="BGOS-XXXX-XX"
#     BGOS_CODE is accepted as a synonym for BGOS_PAIR_CODE.
#   BGOS_ASSISTANT_ID=1012   # pin the pairing to one assistant (one-click flow)
#   DEVICE_LABEL="my-server"
#   HERMES_SERVICE="hermes-gateway.service"
#   REPO_DIR="$HOME/hermes-channel-bgos"
#   BGOS_BACKEND_URL="https://api.brandgrowthos.ai"   # staging/local only; a
#     trailing /api/v1 (the app-facing form) is stripped automatically.
#   BGOS_ENV_FILE=/path/to/.env   # override where BGOS env vars are written
set -euo pipefail

REPO_URL="https://github.com/BrandGrowthOS/hermes-channel-bgos.git"
REPO_DIR="${REPO_DIR:-$HOME/hermes-channel-bgos}"
DEVICE_LABEL="${DEVICE_LABEL:-$(hostname 2>/dev/null || echo hermes-server)}"
BGOS_AGENTS="${BGOS_AGENTS:-default:Hermes}"
HERMES_SERVICE="${HERMES_SERVICE:-hermes-gateway.service}"

log() { printf '\033[1;34m[bgos-install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bgos-install][warn]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[bgos-install][fail]\033[0m %s\n' "$*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"; }

# Command-line arguments, which exist so the pair code can travel as a normal
# flag instead of a POSIX env prefix (see the header). Flags WIN over the
# matching env var when both are given: the flag is what the person just typed.
# An unknown flag warns and is ignored rather than aborting: this file is
# fetched fresh from main every run, so the only way to meet one is a typo or a
# newer app, and neither should cost someone a working install (a mistyped
# --pair-code simply falls through to the interactive prompt below).
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "${1:-}" in
      --pair-code)   BGOS_PAIR_CODE="${2:-}"; shift 2 ;;
      --assistant-id) BGOS_ASSISTANT_ID="${2:-}"; shift 2 ;;
      --agents)      BGOS_AGENTS="${2:-}"; shift 2 ;;
      --device-label) DEVICE_LABEL="${2:-}"; shift 2 ;;
      -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
      BGOS-*|OC-*)   BGOS_PAIR_CODE="$1"; shift ;;
      "")            shift ;;
      *)             warn "Ignoring unrecognized argument: $1"; shift ;;
    esac
  done
}

find_hermes_install() {
  if [[ -n "${HERMES_INSTALL:-}" ]]; then
    printf '%s\n' "$HERMES_INSTALL"
    return
  fi
  local candidates=(
    "$HOME/.hermes/hermes-agent"
    "$HOME/hermes-agent"
    "/opt/hermes-agent"
    "/opt/hermes/hermes-agent"
  )
  local c
  for c in "${candidates[@]}"; do
    if [[ -d "$c" ]]; then
      printf '%s\n' "$c"
      return
    fi
  done
  fail "Could not find Hermes. Set HERMES_INSTALL=/path/to/hermes-agent and rerun."
}

find_hermes_python() {
  local install="$1"
  if [[ -n "${HERMES_PYTHON:-}" ]]; then
    printf '%s\n' "$HERMES_PYTHON"
    return
  fi
  local candidates=(
    "$install/venv/bin/python"
    "$install/.venv/bin/python"
    "$HOME/.local/pipx/venvs/hermes-agent/bin/python"
  )
  local c
  for c in "${candidates[@]}"; do
    if [[ -x "$c" ]]; then
      printf '%s\n' "$c"
      return
    fi
  done
  if command -v hermes >/dev/null 2>&1; then
    local hermes_bin
    hermes_bin="$(command -v hermes)"
    if [[ "$hermes_bin" == *"/bin/hermes" && -x "${hermes_bin%/bin/hermes}/bin/python" ]]; then
      printf '%s\n' "${hermes_bin%/bin/hermes}/bin/python"
      return
    fi
  fi
  fail "Could not find Hermes Python. Set HERMES_PYTHON=/path/to/python and rerun."
}

python_ok() {
  "$1" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

install_package() {
  local py="$1"
  if command -v uv >/dev/null 2>&1; then
    log "Installing package into Hermes Python with uv: $py"
    uv pip install --python "$py" -e "$REPO_DIR"
    return
  fi
  if [[ -x "${py%/python}/pip" ]]; then
    log "Installing package into Hermes venv with pip: ${py%/python}/pip"
    "${py%/python}/pip" install -e "$REPO_DIR"
    return
  fi
  if ! "$py" -m pip --version >/dev/null 2>&1; then
    # A uv-built Hermes venv ships without pip, so the last-resort path
    # below dies with "No module named pip" (hit on a fresh Mac mini,
    # 2026-08-09). ensurepip bootstraps it from the stdlib.
    log "pip missing from the Hermes venv; bootstrapping with ensurepip"
    "$py" -m ensurepip --upgrade \
      || fail "Could not bootstrap pip into $py (ensurepip failed). Install uv (https://docs.astral.sh/uv/) and re-run."
  fi
  log "Installing package with python -m pip"
  "$py" -m pip install -e "$REPO_DIR"
}

hermes_supports_plugins() {
  "$1" - <<'PY' >/dev/null 2>&1
import gateway.platform_registry
PY
}

register_plugin_or_patch() {
  local install="$1"
  local py="$2"
  local hermes_home="${HERMES_HOME:-$HOME/.hermes}"
  local plugin_src="$REPO_DIR/plugins/platforms/bgos"
  if hermes_supports_plugins "$py"; then
    # Fresh-clone layout sanity: the symlink is only useful if it points at a
    # real plugin dir (plugin.yaml manifest + __init__.py register shim).
    # A silent dangling/incomplete symlink is exactly the state the doctor
    # later reports as "Platform.BGOS not registered".
    [[ -f "$plugin_src/plugin.yaml" && -f "$plugin_src/__init__.py" ]] \
      || fail "Plugin source incomplete at $plugin_src (expected plugin.yaml + __init__.py). Re-clone $REPO_DIR."
    log "Modern Hermes plugin registry detected. Registering BGOS via $hermes_home/plugins/bgos symlink."
    mkdir -p "$hermes_home/plugins"
    ln -sfn "$plugin_src" "$hermes_home/plugins/bgos"
    return
  fi

  log "Legacy Hermes detected. Applying fork patch if BGOS is not already registered."
  if "$py" - <<'PY' >/dev/null 2>&1
from gateway.config import Platform
assert Platform.BGOS.value == 'bgos'
PY
  then
    log "Legacy Hermes already has Platform.BGOS. Skipping patch."
    return
  fi

  [[ -d "$install/.git" ]] || fail "Legacy Hermes needs fork patch, but $install is not a git checkout. Upgrade Hermes or apply the patch manually."
  (
    cd "$install"
    git checkout -B bgos-integration
    git am "$REPO_DIR/hermes-fork-patch/0001-bgos-integration.patch" \
      || git am --3way "$REPO_DIR/hermes-fork-patch/0001-bgos-integration.patch" \
      || fail "Fork patch conflicted. Resolve manually in $install."
  )
}

# Strip a trailing slash and any trailing /api/v1 from a base URL. The BGOS
# client appends /api/v1/... paths itself, so a base that already ends in
# /api/v1 doubles the prefix and 404s every call (bit a fresh macOS install:
# whoami HTTP 404 with base_url=https://api.brandgrowthos.ai/api/v1).
normalize_backend_url() {
  local url="$1"
  while :; do
    while [[ "$url" == */ ]]; do url="${url%/}"; done
    if [[ "$url" == */api/v1 ]]; then url="${url%/api/v1}"; else break; fi
  done
  printf '%s\n' "$url"
}

resolve_env_file() {
  local install="$1"
  if [[ -n "${BGOS_ENV_FILE:-}" ]]; then
    printf '%s\n' "$BGOS_ENV_FILE"
    return
  fi
  local envfile=""
  if command -v hermes >/dev/null 2>&1; then
    envfile="$(hermes config env-path 2>/dev/null || true)"
  fi
  # Default to $HERMES_HOME/.env, NOT $install/.env: the gateway itself loads
  # $HERMES_HOME/.env into its environment at startup on EVERY platform
  # (gateway/run.py -> load_hermes_dotenv, override=true). That is what makes
  # the vars reach a launchd-managed gateway on macOS, where there is no
  # systemd EnvironmentFile mechanism and the LaunchAgent plist carries no
  # BGOS vars. The old fallback ($install/.env) is only a fill-in file the
  # gateway consults when $HERMES_HOME/.env is missing.
  printf '%s\n' "${envfile:-${HERMES_HOME:-$HOME/.hermes}/.env}"
}

write_env() {
  local install="$1"
  local envfile
  envfile="$(resolve_env_file "$install")"
  mkdir -p "$(dirname "$envfile")"
  touch "$envfile"
  chmod 600 "$envfile" || true

  local backend_url=""
  if [[ -n "${BGOS_BACKEND_URL:-}" ]]; then
    backend_url="$(normalize_backend_url "$BGOS_BACKEND_URL")"
  fi

  local tmp
  tmp="$(mktemp)"
  grep -vE '^(BGOS_AGENTS|BGOS_ALLOW_ALL_USERS|BGOS_BACKEND_URL)=' "$envfile" > "$tmp" 2>/dev/null || true
  {
    cat "$tmp"
    printf 'BGOS_AGENTS=%s\n' "$BGOS_AGENTS"
    printf 'BGOS_ALLOW_ALL_USERS=true\n'
    if [[ -n "$backend_url" ]]; then
      printf 'BGOS_BACKEND_URL=%s\n' "$backend_url"
    fi
  } > "$envfile"
  rm -f "$tmp"
  log "Wrote BGOS env to $envfile"

  # Linux/systemd: also wire the env file into the unit (belt and suspenders;
  # the gateway's own dotenv load covers the default $HERMES_HOME/.env path).
  if command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user/$HERMES_SERVICE.d"
    cat > "$HOME/.config/systemd/user/$HERMES_SERVICE.d/bgos-env.conf" <<EOF
[Service]
EnvironmentFile=$envfile
EOF
    systemctl --user daemon-reload || warn "systemctl daemon-reload failed; restart Hermes manually."
  fi
  # macOS/launchd: nothing extra to do. The gateway reads $HERMES_HOME/.env
  # itself at startup, and restart_hermes (launchctl kickstart -k) below makes
  # the freshly written vars take effect. Do NOT edit the LaunchAgent plist's
  # EnvironmentVariables - it is unnecessary and risks clobbering user keys.
}

pair_if_requested() {
  local py="$1"
  local code="${BGOS_PAIR_CODE:-${BGOS_CODE:-}}"
  if [[ -z "$code" && ! -f "$HOME/.hermes/secrets/bgos.json" && -t 0 ]]; then
    echo
    log "Open BGOS → Integrations → Hermes → Connect a new Hermes server."
    read -r -p "Paste pairing code now, or press Enter to skip pairing: " code
  fi
  if [[ -n "$code" ]]; then
    log "Pairing this server with BGOS."
    # Since 0.23.0 the pair CLI refuses to pair a multi-agent catalog into a
    # broken topology (missing profile, multiplex off, stray per-profile
    # pairing file) and prints the exact fixes. Do not let set -e kill the
    # installer here: the plugin install itself is fine, and the doctor at
    # the end re-reports the same findings. Surface it and continue.
    # BGOS_ASSISTANT_ID pins this pairing to one assistant (the one-click
    # new-agent flow mints the code pinned to a freshly created assistant
    # and passes its id here, so the exchange can never collide with the
    # account's existing agents).
    local pin_args=()
    if [[ -n "${BGOS_ASSISTANT_ID:-}" ]]; then
      pin_args=(--assistant-id "$BGOS_ASSISTANT_ID")
    fi
    # A staging/local BGOS_BACKEND_URL must reach the pair CLI too, not only
    # the gateway env file: pair_cli otherwise falls back to its production
    # default and pairs against the wrong backend (found live 2026-08-09).
    if [[ -n "${BGOS_BACKEND_URL:-}" ]]; then
      pin_args+=(--base-url "$(normalize_backend_url "$BGOS_BACKEND_URL")")
    fi
    if ! "$py" -m hermes_channel_bgos.pair_cli "$code" --device-label "$DEVICE_LABEL" --agents "$BGOS_AGENTS" ${pin_args[@]+"${pin_args[@]}"}; then
      warn "Pairing did not complete - read the topology findings above, apply the printed fixes, then re-run: $py -m hermes_channel_bgos.pair_cli <NEW-CODE> --device-label '$DEVICE_LABEL' --agents '$BGOS_AGENTS' (pair codes expire in 10 minutes, mint a fresh one)"
    fi
  else
    warn "Pairing skipped. Later run: $py -m hermes_channel_bgos.pair_cli BGOS-XXXX-XX --device-label '$DEVICE_LABEL' --agents '$BGOS_AGENTS'"
  fi
}

restart_hermes() {
  if command -v systemctl >/dev/null 2>&1 && systemctl --user status "$HERMES_SERVICE" >/dev/null 2>&1; then
    log "Restarting systemd user service: $HERMES_SERVICE"
    systemctl --user restart "$HERMES_SERVICE"
    return
  fi
  if command -v launchctl >/dev/null 2>&1 && launchctl print "gui/$(id -u)/ai.hermes.gateway" >/dev/null 2>&1; then
    log "Restarting macOS launchd service: ai.hermes.gateway"
    launchctl kickstart -k "gui/$(id -u)/ai.hermes.gateway"
    return
  fi
  warn "Could not detect a managed Hermes gateway service. Restart Hermes manually."
}

main() {
  parse_args "$@"
  need_cmd git
  local install py
  install="$(find_hermes_install)"
  py="$(find_hermes_python "$install")"
  python_ok "$py" || fail "Hermes Python must be >= 3.11: $py"

  log "Hermes install: $install"
  log "Hermes Python:  $py"
  log "BGOS agents:    $BGOS_AGENTS"

  if [[ -d "$REPO_DIR/.git" ]]; then
    log "Updating existing checkout: $REPO_DIR"
    git -C "$REPO_DIR" fetch origin main
    git -C "$REPO_DIR" checkout main
    git -C "$REPO_DIR" pull --ff-only origin main
  else
    log "Cloning $REPO_URL to $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR"
  fi

  install_package "$py"
  register_plugin_or_patch "$install" "$py"
  write_env "$install"
  pair_if_requested "$py"
  restart_hermes

  log "Running doctor."
  if "$py" -m hermes_channel_bgos.doctor; then
    log "Done. BGOS plugin is installed. If pairing_live is WARN, tick agents in BGOS Integrations → Hermes → Save."
  else
    fail "Doctor found a blocking issue. Follow the fix lines above."
  fi
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then main "$@"; fi
