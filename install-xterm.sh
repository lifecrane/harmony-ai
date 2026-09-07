#!/usr/bin/env bash
# =============================================================================
# install-xterm.sh — harmony-xterm-aichat one-shot installer (Debian headless)
#
# Run INSIDE the repo folder on a bare Debian 12 box (guest, laptop, server):
#     bash install-xterm.sh            # full install (needs sudo for apt)
#     bash install-xterm.sh --check    # verify only, changes nothing
#
# WHAT IT DOES (idempotent — safe to re-run):
#   1. apt deps: python3.11, venv, git, curl
#   2. Ollama (official script if missing) + enable + start
#   3. aichat CLI 0.30.0 (musl static binary -> ~/.local/bin)
#   4. venv_ui + pip panel deps (nicegui, fastapi/uvicorn, openviking)
#   5. ollama pull qwen2.5-3b + qwen3-embedding:0.6b (skips what exists)
#   6. deploy aichat-config-template/ -> ~/.config/aichat/ (new only;
#      use --force to overwrite a live config)
#   7. start panel (run_panel.sh) + verify http://localhost:8080
#
# NEVER ships/touches: credentials.enc, messages.md, Models/blobs, venv_ui/.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${APP_ROOT:-$SCRIPT_DIR}"
AICHAT_VER="0.30.0"
AICHAT_URL="https://github.com/sigoden/aichat/releases/download/v${AICHAT_VER}/aichat-v${AICHAT_VER}-x86_64-unknown-linux-musl.tar.gz"
FORCE=0
CHECK=0
for a in "$@"; do
    case "$a" in
        --force) FORCE=1 ;;
        --check) CHECK=1 ;;
    esac
done

log()  { printf '[install-xterm] %s\n' "$*"; }
warn() { printf '[install-xterm][WARN] %s\n' "$*" >&2; }
die()  { printf '[install-xterm][ERROR] %s\n' "$*" >&2; exit 1; }

[ -f "$APP_ROOT/aichat_xterm.py" ] || die "run from the repo folder ($APP_ROOT/aichat_xterm.py missing)"

if [ "$CHECK" = "1" ]; then
    echo "=== CHECK (no changes) ==="
    command -v python3.11 >/dev/null && echo "OK python3.11: $(python3.11 --version)" || echo "MISS python3.11"
    [ -x "$APP_ROOT/venv_ui/bin/python" ] && echo "OK venv_ui" || echo "MISS venv_ui"
    command -v ollama >/dev/null && echo "OK ollama: $(ollama --version 2>&1 | head -1)" || echo "MISS ollama"
    curl -s -m 2 http://localhost:11434/api/tags >/dev/null && echo "OK ollama API" || echo "MISS ollama API"
    command -v aichat >/dev/null && echo "OK aichat: $(aichat --version 2>&1 | head -1)" || echo "MISS aichat"
    [ -f "$HOME/.config/aichat/config.yaml" ] && echo "OK aichat config" || echo "MISS aichat config"
    ollama list 2>/dev/null | grep -q 'qwen2.5-3b' && echo "OK qwen2.5-3b" || echo "MISS qwen2.5-3b"
    ollama list 2>/dev/null | grep -q 'qwen3-embedding' && echo "OK qwen3-embedding:0.6b" || echo "MISS qwen3-embedding:0.6b"
    curl -s -m 3 http://localhost:8080 >/dev/null && echo "OK panel :8080" || echo "MISS panel :8080"
    exit 0
fi

# ---- 1. apt deps ------------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    log "Installing apt deps (sudo) ..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip git curl ca-certificates
else
    warn "no apt-get — install python3.11 + git + curl by hand"
fi
command -v python3.11 >/dev/null || die "python3.11 still missing after apt"

# ---- 2. Ollama --------------------------------------------------------------
if ! command -v ollama >/dev/null 2>&1; then
    log "Installing Ollama ..."
    curl -fsSL https://ollama.com/install.sh | sh
fi
if systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
    sudo systemctl enable --now ollama 2>/dev/null || systemctl --user start ollama 2>/dev/null || true
else
    (ollama serve >/tmp/ollama.log 2>&1 &) || true
fi
for _ in $(seq 1 20); do
    curl -s -m 2 http://localhost:11434/api/tags >/dev/null && break
    sleep 1
done
curl -s -m 2 http://localhost:11434/api/tags >/dev/null || die "Ollama API not responding on :11434"

# ---- 3. aichat CLI ----------------------------------------------------------
if command -v aichat >/dev/null 2>&1 && aichat --version 2>&1 | grep -q "$AICHAT_VER"; then
    log "aichat $AICHAT_VER already installed"
else
    log "Installing aichat $AICHAT_VER ..."
    mkdir -p "$HOME/.local/bin"
    tmpd="$(mktemp -d)"; trap 'rm -rf "$tmpd"' EXIT
    curl -fsSL "$AICHAT_URL" -o "$tmpd/aichat.tar.gz" \
        || die "aichat download failed ($AICHAT_URL)"
    tar -xzf "$tmpd/aichat.tar.gz" -C "$tmpd"
    install -m 0755 "$tmpd/aichat" "$HOME/.local/bin/aichat"
    rm -rf "$tmpd"; trap - EXIT
    export PATH="$HOME/.local/bin:$PATH"
    aichat --version
fi

# ---- 4. venv + pip ----------------------------------------------------------
if [ ! -x "$APP_ROOT/venv_ui/bin/python" ]; then
    log "Creating venv_ui ..."
    python3.11 -m venv "$APP_ROOT/venv_ui"
fi
log "Installing pip deps ..."
"$APP_ROOT/venv_ui/bin/pip" install --upgrade -q pip
"$APP_ROOT/venv_ui/bin/pip" install -q \
    'nicegui==3.14.0' 'fastapi==0.141.1' 'uvicorn==0.52.1' \
    'requests==2.34.2' 'openviking==0.4.16' 'openviking-sdk==0.1.8'

# ---- 5. models (idempotent) -------------------------------------------------
for m in qwen2.5-3b qwen3-embedding:0.6b; do
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m\|$m:latest"; then
        log "already pulled: $m"
    else
        log "pulling $m ..."
        ollama pull "$m" || warn "pull failed for $m (retry: ollama pull $m)"
    fi
done

# ---- 6. aichat config (new only, unless --force) ----------------------------
if [ -f "$HOME/.config/aichat/config.yaml" ] && [ "$FORCE" = "0" ]; then
    log "aichat config exists — keeping live config (use --force to overwrite)"
else
    log "Deploying aichat-config-template -> ~/.config/aichat/"
    mkdir -p "$HOME/.config/aichat/roles"
    cp "$APP_ROOT/aichat-config-template/config.yaml" "$HOME/.config/aichat/config.yaml"
    cp "$APP_ROOT/aichat-config-template/roles/"*.md "$HOME/.config/aichat/roles/"
fi

# ---- 7. runtime dirs + start + verify ---------------------------------------
mkdir -p "$APP_ROOT/History" "$APP_ROOT/backup" "$APP_ROOT/WORKSPACE"
chmod +x "$APP_ROOT/run_panel.sh"
log "Starting panel ..."
PY="$APP_ROOT/venv_ui/bin/python" bash "$APP_ROOT/run_panel.sh" start
sleep 2
if curl -s -m 5 http://localhost:8080 >/dev/null; then
    log "✅ Panel UP: http://localhost:8080"
else
    warn "panel not answering yet — see $APP_ROOT/History/panel.log"
fi
log "Done. Re-run anytime: bash install-xterm.sh (idempotent)."
