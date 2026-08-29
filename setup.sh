#!/usr/bin/env bash
#
# setup.sh - interactive first-run setup for Brightspace-MCP.
#
# Walks the README's "Setup" section end to end:
#   * checks for uv (offers to install it)
#   * uv sync            (+ optional heavy "transcription" extra)
#   * collects your Brightspace browser-session cookies
#   * mints the inbound bearer token via mint_token.py
#   * writes .env with everything brightspacemcp.auth / server.py read
#   * prints the  Bearer <token>  string to paste into your MCP client
#
# Safe to re-run: existing .env values are offered back as defaults.

set -euo pipefail

cd "$(cd "$(dirname "$0")" && pwd)"

ENV_FILE=".env"

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '\033[1;33m   %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

ask_yes_no() {
    # ask_yes_no "prompt" [default y|n]
    local prompt="$1" default="${2:-y}" hint reply
    [ "$default" = y ] && hint="Y/n" || hint="y/N"
    read -rp "   $prompt [$hint]: " reply
    reply="${reply:-$default}"
    case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

ask_value() {
    # ask_value "label" "existing"  -> echoes chosen value on stdout
    local label="$1" existing="${2:-}" reply
    if [ -n "$existing" ]; then
        read -rp "   $label (enter to keep current): " reply
        printf '%s' "${reply:-$existing}"
    else
        read -rp "   $label: " reply
        printf '%s' "$reply"
    fi
}

ask_required() {
    # like ask_value, but re-prompts until non-empty
    local label="$1" existing="${2:-}" value
    while :; do
        value="$(ask_value "$label" "$existing")"
        [ -n "$value" ] && { printf '%s' "$value"; return; }
        warn "That can't be empty."
    done
}

env_get() {
    # current value of KEY in .env ("" if the file or key is absent)
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | head -n1
}

say "Brightspace-MCP setup"
info "Working in: $(pwd)"

# --- uv -----------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    warn "uv is not installed - it manages the venv and runs the server."
    if ask_yes_no "Install uv now via the official astral.sh installer?"; then
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- https://astral.sh/uv/install.sh | sh
        else
            die "Need curl or wget to fetch the uv installer. Install uv manually: https://docs.astral.sh/uv/"
        fi
        export PATH="$HOME/.local/bin:$PATH"
        command -v uv >/dev/null 2>&1 || die "uv still not on PATH - open a new shell and re-run setup.sh"
    else
        die "Can't continue without uv. See https://docs.astral.sh/uv/ then re-run."
    fi
fi
info "uv: $(uv --version)"

# --- dependencies -----------------------------------------------------------
say "Installing dependencies"
info "uv sync  (mcp, httpx2, python-dotenv; Python >= 3.14 fetched automatically)"
uv sync

if ask_yes_no "Also install the heavy 'transcription' extra (playwright + whisper/PyTorch) for getLink?" n; then
    uv sync --extra transcription
    uv run playwright install chromium
fi

# --- existing .env values (offered back as defaults on re-run) --------------
old_session="$(env_get d2lSessionVal)"
old_secure="$(env_get d2lSecureSessionVal)"
old_host="$(env_get MCP_PUBLIC_HOST)"
old_token="$(env_get MCP_INBOUND_TOKEN)"

# --- outbound auth: Brightspace session cookies ----------------------------
say "Brightspace session cookies (outbound auth)"
cat <<'EOF'
   Log in to https://purdue.brightspace.com in your browser, open DevTools ->
   Application / Storage -> Cookies, and copy these two values:
EOF
d2l_session="$(ask_required "d2lSessionVal" "$old_session")"
d2l_secure="$(ask_required "d2lSecureSessionVal" "$old_secure")"

# --- public host (optional) -----------------------------------------------
say "Public host (optional)"
info "Hostname your TLS proxy serves; pins the SDK's allowed Host/Origin."
info "Only matters if you front this with nginx/Cloudflare. Default: mcp.xennick.com"
public_host="$(ask_value "MCP_PUBLIC_HOST" "${old_host:-mcp.xennick.com}")"

# --- write .env -----------------------------------------------------------
say "Writing $ENV_FILE"
umask 077
{
    printf '# --- Brightspace-MCP environment ---------------------------------------\n'
    printf '# Loaded by brightspacemcp.auth via python-dotenv (load_dotenv()) at\n'
    printf '# import, so these are in os.environ before server.main() runs.\n'
    printf '# Written by setup.sh - re-run it to change values.\n'
    printf '\n'
    printf '# Outbound auth (this server -> Brightspace): a logged-in browser\n'
    printf "# session's cookies. Refresh when the server starts returning 401/403.\n"
    printf 'd2lSessionVal=%s\n' "$d2l_session"
    printf 'd2lSecureSessionVal=%s\n' "$d2l_secure"
    printf '\n'
    printf '# Optional: public hostname nginx/Cloudflare serves (default mcp.xennick.com).\n'
    printf 'MCP_PUBLIC_HOST=%s\n' "$public_host"
} > "$ENV_FILE"

# --- inbound auth: bearer token via mint_token.py ----------------------------
say "Inbound bearer token (MCP client -> this server)"
if [ -n "$old_token" ] && ask_yes_no "Reuse the existing inbound token (keeps your MCP client config valid)?" y; then
    token="$old_token"
    printf '\n# Inbound auth: MCP clients must send  Authorization: Bearer <this>\n' >> "$ENV_FILE"
    printf 'MCP_INBOUND_TOKEN=%s\n' "$token" >> "$ENV_FILE"
    info "kept existing MCP_INBOUND_TOKEN"
else
    printf '\n# Inbound auth: MCP clients must send  Authorization: Bearer <this>\n' >> "$ENV_FILE"
    token="$(uv run python mint_token.py --raw --force)"
    info "minted a fresh MCP_INBOUND_TOKEN (written to $ENV_FILE)"
fi

# --- done ---------------------------------------------------------------
say "Done"
cat <<EOF

   Start the server:

       uv run brightspacemcp          # or: python -m brightspacemcp

   Listens on http://127.0.0.1:8008 - point your MCP client there directly,
   or through a TLS proxy for ${public_host}.

EOF
printf '\033[1;32mCopy this whole token for input: "Bearer %s"\033[0m\n\n' "$token"
