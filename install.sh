#!/usr/bin/env bash
# One-command installer for hermes-channel-bgos.
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/BrandGrowthOS/hermes-channel-bgos/main/install.sh)
# Optional env:
#   HERMES_INSTALL=/path/to/hermes-agent
#   HERMES_PYTHON=/path/to/hermes/python
#   BGOS_AGENTS="default:Hermes"
#   BGOS_PAIR_CODE="BGOS-XXXX-XX"
#     BGOS_CODE is accepted as a synonym for BGOS_PAIR_CODE.
#   DEVICE_LABEL="my-server"
#   HERMES_SERVICE="hermes-gateway.service"
#   REPO_DIR="$HOME/hermes-channel-bgos"
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
  if hermes_supports_plugins "$py"; then
    log "Modern Hermes plugin registry detected. Registering BGOS via ~/.hermes/plugins/bgos symlink."
    mkdir -p "$HOME/.hermes/plugins"
    ln -sfn "$REPO_DIR/plugins/platforms/bgos" "$HOME/.hermes/plugins/bgos"
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

write_env() {
  local install="$1"
  local envfile
  if command -v hermes >/dev/null 2>&1; then
    envfile="$(hermes config env-path 2>/dev/null || true)"
  fi
  envfile="${envfile:-$install/.env}"
  mkdir -p "$(dirname "$envfile")"
  touch "$envfile"
  chmod 600 "$envfile" || true

  local tmp
  tmp="$(mktemp)"
  grep -vE '^(BGOS_AGENTS|BGOS_ALLOW_ALL_USERS)=' "$envfile" > "$tmp" 2>/dev/null || true
  {
    cat "$tmp"
    printf 'BGOS_AGENTS=%s\n' "$BGOS_AGENTS"
    printf 'BGOS_ALLOW_ALL_USERS=true\n'
  } > "$envfile"
  rm -f "$tmp"
  log "Wrote BGOS env to $envfile"

  if command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user/$HERMES_SERVICE.d"
    cat > "$HOME/.config/systemd/user/$HERMES_SERVICE.d/bgos-env.conf" <<EOF
[Service]
EnvironmentFile=$envfile
EOF
    systemctl --user daemon-reload || warn "systemctl daemon-reload failed; restart Hermes manually."
  fi
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
    "$py" -m hermes_channel_bgos.pair_cli "$code" --device-label "$DEVICE_LABEL" --agents "$BGOS_AGENTS"
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
