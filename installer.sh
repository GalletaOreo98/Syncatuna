#!/bin/sh
set -eu

APP_NAME="syncatuna"
VERSION="0.1.1"
INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
# Cambia esta URL si tu repositorio de Syncatuna tiene otro nombre/owner.
REPO_RAW="https://raw.githubusercontent.com/GalletaOreo98/Syncatuna/refs/heads/main"

fail() {
    printf '\n[ERROR] %s\n' "$1" >&2
    exit 1
}

info() {
    printf '[Syncatuna] %s\n' "$1"
}

# Las 2 dependencias que pide el proyecto se comprueban primero
command -v yt-dlp >/dev/null 2>&1 || fail "No se encontró yt-dlp en PATH. Instálalo con el gestor de paquetes de tu distribución y vuelve a ejecutar el instalador."
command -v mpv >/dev/null 2>&1 || fail "No se encontró mpv en PATH. Instálalo con el gestor de paquetes de tu distribución y vuelve a ejecutar el instalador."

command -v python3 >/dev/null 2>&1 || fail "No se encontró Python 3 en PATH. Instálalo con el gestor de paquetes de tu distribución y vuelve a ejecutar el instalador."
command -v curl >/dev/null 2>&1 || fail "No se encontró curl. Instálalo para poder descargar Syncatuna."

PYTHON="python3"
PYTHON_VERSION="$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
$PYTHON - <<'PY' || fail "Syncatuna requiere Python 3.10 o superior."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY

TMP_DIR="$(mktemp -d)"
STAGE="$TMP_DIR/syncatuna"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM HUP

info "Comprobaciones: yt-dlp ✓  mpv ✓  Python $PYTHON_VERSION ✓"
info "Descargando Syncatuna..."

mkdir -p "$STAGE"
curl -fsSL "$REPO_RAW/client.py" -o "$STAGE/client.py" || fail "No se pudo descargar client.py."
curl -fsSL "$REPO_RAW/server.py" -o "$STAGE/server.py" || fail "No se pudo descargar server.py."
curl -fsSL "$REPO_RAW/syncatuna.py" -o "$STAGE/syncatuna.py" || fail "No se pudo descargar syncatuna.py."
curl -fsSL "$REPO_RAW/requirements.txt" -o "$STAGE/requirements.txt" || fail "No se pudo descargar requirements.txt."
printf '%s\n' "$VERSION" > "$STAGE/VERSION"

info "Creando entorno Python privado..."
"$PYTHON" -m venv "$STAGE/venv" || fail "No se pudo crear el entorno virtual. En algunas distribuciones necesitas instalar python3-venv/python-venv."

info "Instalando dependencias Python en el entorno privado..."
"$STAGE/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null || fail "No se pudo actualizar pip."
"$STAGE/venv/bin/python" -m pip install --disable-pip-version-check -r "$STAGE/requirements.txt" || fail "No se pudieron instalar las dependencias Python."

# Si todo lo anterior salió bien, reemplazamos la instalación anterior de una vez.
mkdir -p "$(dirname "$INSTALL_ROOT")" "$BIN_DIR"
rm -rf "$INSTALL_ROOT"
mv "$STAGE" "$INSTALL_ROOT"

cat > "$BIN_DIR/$APP_NAME" <<EOF
#!/bin/sh
exec "$INSTALL_ROOT/venv/bin/python" "$INSTALL_ROOT/syncatuna.py" "\$@"
EOF
chmod +x "$BIN_DIR/$APP_NAME"

info "Syncatuna $VERSION quedó instalado en $INSTALL_ROOT"
info "Ejecutable: $BIN_DIR/$APP_NAME"

case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *)
        printf '\nAviso: %s no está en PATH.\n' "$BIN_DIR"
        printf 'Añade esto a tu shell:\n  export PATH="%s:\$PATH"\n' "$BIN_DIR"
        ;;
esac

printf '\nListo. Prueba:\n  syncatuna -h\n'
