#!/bin/sh
set -eu

APP_NAME="syncatuna"
INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

printf '[Syncatuna] Desinstalando...\n'
rm -f "$BIN_DIR/$APP_NAME"
rm -rf "$INSTALL_ROOT"
printf '[Syncatuna] Desinstalado.\n'
