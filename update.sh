#!/usr/bin/env bash
#
# Update fuer Status-LED + OLED: holt die neueste Version aus dem Repo,
# aktualisiert die Bibliotheken und startet den Dienst neu.
# Die Konfiguration unter /etc/status-led/config.toml bleibt unangetastet.
#
# Aufruf:  sudo status-led update   (oder direkt: sudo /opt/status-led/update.sh)

set -euo pipefail

INSTALL_DIR="/opt/status-led"
VENV_DIR="$INSTALL_DIR/venv"
BRANCH="main"

c_info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    c_err "Bitte mit Root-Rechten ausfuehren (sudo)."
    exit 1
fi
if [ ! -d "$INSTALL_DIR/.git" ]; then
    c_err "Keine Git-Installation in $INSTALL_DIR gefunden. Bitte zuerst install.sh ausfuehren."
    exit 1
fi

OLD_VER="$(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || echo unbekannt)"

c_info "Hole neueste Aenderungen..."
git -C "$INSTALL_DIR" fetch --quiet --tags origin "$BRANCH"
git -C "$INSTALL_DIR" reset --hard --quiet "origin/$BRANCH"
NEW_VER="$(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || echo unbekannt)"

c_info "Aktualisiere Python-Bibliotheken..."
"$VENV_DIR/bin/pip" install --quiet --upgrade -r "$INSTALL_DIR/requirements.txt"

# CLI-Wrapper auf den aktuellen Stand bringen (neue Unterbefehle etc.)
if [ -f "$INSTALL_DIR/status-led" ]; then
    install -m 0755 "$INSTALL_DIR/status-led" /usr/local/bin/status-led 2>/dev/null || {
        cp "$INSTALL_DIR/status-led" /usr/local/bin/status-led; chmod 0755 /usr/local/bin/status-led;
    }
fi

if [ -f /etc/systemd/system/status-led.service ]; then
    systemctl daemon-reload
fi

c_info "Starte Dienst neu..."
systemctl restart status-led.service
# Web-Dashboard (falls eingerichtet) ebenfalls neu starten, damit Code-Aenderungen
# am Webserver wirksam werden.
systemctl try-restart status-led-web.service 2>/dev/null || true

if [ "$OLD_VER" = "$NEW_VER" ]; then
    c_ok "Bereits aktuell ($NEW_VER)."
else
    c_ok "Aktualisiert: $OLD_VER  ->  $NEW_VER"
fi
