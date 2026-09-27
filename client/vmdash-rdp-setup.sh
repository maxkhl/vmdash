#!/usr/bin/env bash
#
# vmdash-rdp-setup.sh — richtet einen Linux-Rechner einmalig so ein, dass
# .rdp-Dateien von rdpgw (über vmdash "Verbinden") per Klick mit FreeRDP
# über das RD Gateway (Websocket) geöffnet werden.
#
#   Einrichten:   bash vmdash-rdp-setup.sh
#   Entfernen:    bash vmdash-rdp-setup.sh --uninstall
#
# Voraussetzung: Flatpak. Alles wird nur für den aktuellen Benutzer installiert
# (kein sudo nötig).
#
set -euo pipefail

APP_ID="com.freerdp.FreeRDP"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/vmdash-rdp-open"
DESKTOP_DIR="$DATA_HOME/applications"
DESKTOP_FILE="$DESKTOP_DIR/vmdash-rdp.desktop"
MIME_DIR="$DATA_HOME/mime"
MIME_FILE="$MIME_DIR/packages/vmdash-rdp.xml"
MIME_TYPE="application/x-rdp"

refresh_databases() {
    update-mime-database "$MIME_DIR" >/dev/null 2>&1 || true
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$LAUNCHER" "$DESKTOP_FILE" "$MIME_FILE"
    refresh_databases
    echo "Entfernt. FreeRDP (Flatpak $APP_ID) bleibt installiert:"
    echo "  flatpak uninstall --user $APP_ID"
    exit 0
fi

if ! command -v flatpak >/dev/null 2>&1; then
    echo "FEHLER: Flatpak ist nicht installiert." >&2
    exit 1
fi

# 1. FreeRDP installieren, falls es noch fehlt
if flatpak info "$APP_ID" >/dev/null 2>&1; then
    echo "FreeRDP ist bereits installiert."
else
    echo "Installiere FreeRDP ($APP_ID) für den aktuellen Benutzer ..."
    flatpak remote-add --user --if-not-exists flathub \
        https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --user -y flathub "$APP_ID"
fi

mkdir -p "$BIN_DIR" "$DESKTOP_DIR" "$MIME_DIR/packages"

# 2. Starter: kopiert die .rdp-Datei in ein privates Laufzeitverzeichnis,
#    liest den Gateway-Host aus und ruft FreeRDP mit festen Parametern auf.
#    Die Kopie (enthält das Gateway-Token) wird danach gelöscht.
cat > "$LAUNCHER" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail

src="${1:?Aufruf: vmdash-rdp-open <datei.rdp>}"
log_dir="${XDG_CACHE_HOME:-$HOME/.cache}"
log="$log_dir/vmdash-rdp.log"
mkdir -p "$log_dir"
touch "$log" && chmod 600 "$log"

fail() {
    echo "$(date '+%F %T') FEHLER: $1" >> "$log"
    command -v notify-send >/dev/null 2>&1 && notify-send "vmdash RDP" "$1" || true
    exit 1
}

[[ -r "$src" ]] || fail "Datei nicht lesbar: $src"

run_dir="${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR ist nicht gesetzt}/vmdash-rdp"
mkdir -p "$run_dir"
chmod 700 "$run_dir"
tmp="$(mktemp --suffix=.rdp "$run_dir/XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cp "$src" "$tmp"

gw="$(grep -i '^gatewayhostname:s:' "$tmp" | head -n 1 | cut -d: -f3- | tr -d '\r')"
[[ -n "$gw" ]] || fail "Keine Gateway-Angabe in $src"

echo "$(date '+%F %T') Starte Verbindung über Gateway $gw" >> "$log"
# FreeRDP endet auch beim normalen Schließen des Fensters mit Rückgabewert != 0
# (ERRCONNECT_CONNECT_CANCELLED). Als Fehler gilt nur ein frühes Ende.
SECONDS=0
rc=0
flatpak run --filesystem=xdg-run/vmdash-rdp:ro com.freerdp.FreeRDP \
    "$tmp" \
    "/gateway:g:${gw},usage-method:direct,type:http" \
    /dynamic-resolution \
    /cert:tofu \
    >> "$log" 2>&1 || rc=$?
runtime=$SECONDS
echo "$(date '+%F %T') FreeRDP beendet: Rückgabewert $rc nach ${runtime} s" >> "$log"

if (( rc != 0 && runtime <= 30 )); then
    fail "Verbindung fehlgeschlagen (Rückgabewert $rc), Details in $log"
fi
exit 0
EOF
chmod 755 "$LAUNCHER"

# 3. Desktop-Eintrag (Name taucht im "Öffnen mit"-Dialog auf)
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=vmdash RDP
Comment=Öffnet .rdp-Dateien von vmdash über das RD Gateway mit FreeRDP
Exec=$LAUNCHER %f
Icon=preferences-desktop-remote-desktop
MimeType=$MIME_TYPE;
Terminal=false
Categories=Network;RemoteAccess;
EOF

# 4. MIME-Typ für *.rdp (falls das System ihn nicht schon kennt)
cat > "$MIME_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="$MIME_TYPE">
    <comment>Remote Desktop Connection</comment>
    <glob pattern="*.rdp"/>
  </mime-type>
</mime-info>
EOF

refresh_databases
xdg-mime default vmdash-rdp.desktop "$MIME_TYPE"

echo
echo "Fertig. Standardprogramm für $MIME_TYPE: $(xdg-mime query default "$MIME_TYPE")"
echo "Im Browser einmalig einstellen, dass .rdp-Dateien immer geöffnet werden."
echo "Log bei Problemen: ${XDG_CACHE_HOME:-$HOME/.cache}/vmdash-rdp.log"
