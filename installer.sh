#!/bin/sh
set -eu

APP_NAME="syncatuna"
VERSION="0.2.0"

INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"
CONFIG_FILE="$CONFIG_DIR/config.toml"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

REPO_RAW="https://raw.githubusercontent.com/GalletaOreo98/Syncatuna/refs/heads/main"

fail() {
    printf '\n[ERROR] %s\n' "$1" >&2
    exit 1
}

info() {
    printf '[Syncatuna] %s\n' "$1"
}

warn() {
    printf '[Syncatuna] WARNING: %s\n' "$1" >&2
}


# ---------------------------------------------------------------------------
# Required dependency checks
# ---------------------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || \
    fail "Python 3 was not found in PATH. Install Python 3.12 or newer using your distribution's package manager and run the installer again."

command -v curl >/dev/null 2>&1 || \
    fail "curl was not found. Install curl to download Syncatuna."


# yt-dlp and mpv are only required by the Syncatuna client.
# The server itself does not require either one.
HAS_YT_DLP=1
HAS_MPV=1

if ! command -v yt-dlp >/dev/null 2>&1; then
    HAS_YT_DLP=0
fi

if ! command -v mpv >/dev/null 2>&1; then
    HAS_MPV=0
fi


# ---------------------------------------------------------------------------
# Python version check
# ---------------------------------------------------------------------------

PYTHON="python3"

PYTHON_VERSION="$(
    "$PYTHON" -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)"

"$PYTHON" - <<'PY' || fail "Syncatuna requires Python 3.12 or newer."
import sys

raise SystemExit(
    0 if sys.version_info >= (3, 12) else 1
)
PY

# ---------------------------------------------------------------------------
# Temporary installation directory
# ---------------------------------------------------------------------------

TMP_DIR="$(mktemp -d)"
STAGE="$TMP_DIR/syncatuna"

cleanup() {
    rm -rf "$TMP_DIR"
}

trap cleanup EXIT INT TERM HUP

info "System checks passed. Python $PYTHON_VERSION detected."
info "Downloading Syncatuna $VERSION..."

# ---------------------------------------------------------------------------
# Download application files
# ---------------------------------------------------------------------------

mkdir -p "$STAGE"

curl -fsSL "$REPO_RAW/client.py" \
    -o "$STAGE/client.py" \
    || fail "Could not download client.py."

curl -fsSL "$REPO_RAW/server.py" \
    -o "$STAGE/server.py" \
    || fail "Could not download server.py."

curl -fsSL "$REPO_RAW/syncatuna.py" \
    -o "$STAGE/syncatuna.py" \
    || fail "Could not download syncatuna.py."

curl -fsSL "$REPO_RAW/config.py" \
    -o "$STAGE/config.py" \
    || fail "Could not download config.py."

curl -fsSL "$REPO_RAW/requirements.txt" \
    -o "$STAGE/requirements.txt" \
    || fail "Could not download requirements.txt."

printf '%s\n' "$VERSION" > "$STAGE/VERSION"

# ---------------------------------------------------------------------------
# Create private Python environment
# ---------------------------------------------------------------------------

info "Creating private Python virtual environment..."

"$PYTHON" -m venv "$STAGE/venv" \
    || fail "Could not create the Python virtual environment. Your distribution may require a package such as python3-venv."


info "Installing Python dependencies..."

"$STAGE/venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --upgrade pip >/dev/null \
    || fail "Could not upgrade pip."

"$STAGE/venv/bin/python" -m pip install \
    --disable-pip-version-check \
    -r "$STAGE/requirements.txt" \
    || fail "Could not install Python dependencies."


# ---------------------------------------------------------------------------
# Replace application code atomically
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "$INSTALL_ROOT")" "$BIN_DIR"

rm -rf "$INSTALL_ROOT"
mv "$STAGE" "$INSTALL_ROOT"

# ---------------------------------------------------------------------------
# Create executable launcher
# ---------------------------------------------------------------------------

cat > "$BIN_DIR/$APP_NAME" <<EOF
#!/bin/sh
exec "$INSTALL_ROOT/venv/bin/python" "$INSTALL_ROOT/syncatuna.py" "\$@"
EOF

chmod +x "$BIN_DIR/$APP_NAME"

# ---------------------------------------------------------------------------
# Create persistent configuration
#
# IMPORTANT:
# The configuration is created only on the first installation.
# Existing configuration files are NEVER overwritten by updates.
# ---------------------------------------------------------------------------

mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<'EOF'
# Syncatuna configuration
#
# This file is persistent and is preserved across Syncatuna updates.
# Values not specified here use Syncatuna's built-in defaults.

[server]

host = "127.0.0.1"
port = 8765

# WebSocket limits
max_msg_size = 8192

# Watchdog interval in seconds
watchdog_interval = 2.0

# Delay before starting a new track after an automatic/manual advance
auto_advance_grace = 3.0
manual_advance_grace = 3.0

# Queue and input limits
max_queue_size = 100
max_url_length = 2048
max_title_length = 200
max_name_length = 24
max_duration_seconds = 21600

# Minimum delay between "add" requests from the same client
add_cooldown_seconds = 2.0


[client]

# How often the client synchronizes its clock with the server
clock_sync_interval = 20.0

# Terminal UI refresh interval
ticker_interval = 1.0
EOF

    chmod 600 "$CONFIG_FILE"

    info "Created configuration file: $CONFIG_FILE"
else
    info "Existing configuration preserved: $CONFIG_FILE"
fi

# ---------------------------------------------------------------------------
# Installation summary
# ---------------------------------------------------------------------------

info "Syncatuna $VERSION installed successfully."
info "Installation directory: $INSTALL_ROOT"
info "Configuration file: $CONFIG_FILE"
info "Executable: $BIN_DIR/$APP_NAME"

# ---------------------------------------------------------------------------
# Optional client dependency warnings
# ---------------------------------------------------------------------------

if [ "$HAS_YT_DLP" -eq 0 ]; then
    warn "yt-dlp was not found in PATH."
    warn "yt-dlp is required to use the Syncatuna client."
fi

if [ "$HAS_MPV" -eq 0 ]; then
    warn "mpv was not found in PATH."
    warn "mpv is required to use the Syncatuna client."
fi

if [ "$HAS_YT_DLP" -eq 0 ] || [ "$HAS_MPV" -eq 0 ]; then
    printf '\n'
    warn "The Syncatuna server does not require yt-dlp or mpv."
    warn "Install both dependencies before using the client."
fi

# ---------------------------------------------------------------------------
# PATH warning
# ---------------------------------------------------------------------------

case ":${PATH:-}:" in
    *":$BIN_DIR:"*)
        ;;
    *)
        printf '\n'
        warn "$BIN_DIR is not currently in PATH."
        printf 'Add this to your shell configuration:\n'
        printf '  export PATH="%s:$PATH"\n' "$BIN_DIR"
        ;;
esac


printf '\n'
info "Installation complete."
printf 'Try:\n'
printf '  syncatuna -h\n'