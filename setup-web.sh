#!/usr/bin/env bash
#
# Einrichtungs-Assistent fuer das Status-LED Web-Dashboard.
# Fragt Port, Bind-Adresse und Zugangsdaten (HTTP-Basic-Auth) ab, legt einen
# unprivilegierten Dienst-Benutzer an und erzeugt/aktiviert den systemd-Dienst.
#
# Aufruf:  sudo status-led web-setup   (oder sudo /opt/status-led/setup-web.sh)

set -euo pipefail

INSTALL_DIR="/opt/status-led"
CONFIG_DIR="/etc/status-led"
ENV_FILE="$CONFIG_DIR/web.env"
SECRET_FILE="$CONFIG_DIR/web.secret"
SERVICE_FILE="/etc/systemd/system/status-led-web.service"
WEB_USER="statusled-web"
STATE_PATH="/run/status-led/state.json"

c_info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; }

TTY="/dev/tty"
have_tty() { [ -e "$TTY" ] && [ -r "$TTY" ]; }
ask() {
    local prompt="$1" def="${2:-}" ans="" suffix=""
    [ -n "$def" ] && suffix=" [$def]"
    if have_tty; then read -r -p "$prompt$suffix: " ans <"$TTY" || true; fi
    echo "${ans:-$def}"
}
ask_secret() {
    local prompt="$1" ans=""
    if have_tty; then read -r -s -p "$prompt: " ans <"$TTY" || true; echo >"$TTY"; fi
    echo "$ans"
}
ask_yesno() {
    local prompt="$1" def="$2" d ans
    [ "$def" = "1" ] && d="J/n" || d="j/N"
    ans="$(ask "$prompt ($d)" "")"
    [ -z "$ans" ] && { [ "$def" = "1" ]; return; }
    case "${ans,,}" in j|ja|y|yes) return 0;; *) return 1;; esac
}

[ "$(id -u)" -eq 0 ] || { c_err "Bitte mit Root-Rechten ausfuehren (sudo)."; exit 1; }
have_tty || { c_err "Kein Terminal - der Assistent braucht interaktive Eingaben."; exit 1; }

echo "============================================================"
echo " Status-LED  --  Web-Dashboard einrichten"
echo "============================================================"
echo "Zeigt die gesammelten Werte modern im Browser. Zugriff per Passwort."
echo

if ! ask_yesno "Web-Dashboard einrichten/aktivieren?" 1; then
    systemctl disable --now status-led-web.service 2>/dev/null || true
    c_ok "Web-Dashboard deaktiviert (Konfiguration bleibt erhalten)."
    exit 0
fi

WEB_BIND="$(ask 'Bind-Adresse (0.0.0.0 = im ganzen LAN erreichbar)' '0.0.0.0')"
WEB_PORT="$(ask 'Port' '8080')"
echo; c_info "Zugang (Benutzername + Passwort fuer die Seite)"
WEB_USERNAME="$(ask 'Benutzername' 'admin')"
while :; do
    WPW1="$(ask_secret 'Passwort')"
    WPW2="$(ask_secret 'Passwort wiederholen')"
    [ -n "$WPW1" ] && [ "$WPW1" = "$WPW2" ] && break
    c_warn "Passwoerter leer oder ungleich - bitte erneut."
done

# --- Steuer-Aktionen (Update/Neustart per Button) ----------------------------
echo; c_info "Steuerung (optional)"
echo "  Erlaubt Update und Dienst-Neustart per Button in der Weboberflaeche."
echo "  ACHTUNG: dazu laeuft der Webserver als root (groessere Angriffsflaeche)."
if ask_yesno "Steuer-Aktionen (Update/Neustart) erlauben?" 0; then
    WEB_CONTROL=1; SERVICE_USER="root"
else
    WEB_CONTROL=0; SERVICE_USER="$WEB_USER"
fi

# --- Dienst-Benutzer (unprivilegiert) ----------------------------------------
if ! id -u "$WEB_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$WEB_USER"
    c_ok "Dienst-Benutzer '$WEB_USER' angelegt."
fi
# Lese-Zugriff auf das Journal (fuer das Backup-Protokoll im System-/Backup-Tab)
usermod -aG systemd-journal "$WEB_USER" 2>/dev/null || true

# --- Dateien schreiben -------------------------------------------------------
mkdir -p "$CONFIG_DIR"
cat > "$ENV_FILE" <<EOF
# Status-LED Web-Dashboard (erzeugt von setup-web.sh)
WEB_BIND=$WEB_BIND
WEB_PORT=$WEB_PORT
WEB_STATE_PATH=$STATE_PATH
WEB_SECRET=$SECRET_FILE
WEB_CONTROL=$WEB_CONTROL
EOF
chmod 644 "$ENV_FILE"

umask 077
{
    echo "username=$WEB_USERNAME"
    echo "password=$WPW1"
} > "$SECRET_FILE"
umask 022
chown "$WEB_USER:$WEB_USER" "$SECRET_FILE"
chmod 600 "$SECRET_FILE"
c_ok "Zugangsdaten gespeichert ($SECRET_FILE, nur fuer '$WEB_USER' lesbar)."

# --- systemd-Dienst ----------------------------------------------------------
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Status-LED web dashboard
After=network-online.target status-led.service
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/web_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Update per Button: entkoppeltes oneshot-Unit (laeuft unabhaengig vom Web-Request,
# darf daher auch den Web-Dienst selbst neu starten).
if [ "$WEB_CONTROL" = "1" ]; then
    cat > /etc/systemd/system/status-led-update.service <<EOF
[Unit]
Description=Status-LED self-update (ausgeloest aus der Weboberflaeche)

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/update.sh
EOF
fi

systemctl daemon-reload
systemctl enable status-led-web.service >/dev/null 2>&1 || true
# Immer neu starten (nicht 'enable --now'): so wird die geaenderte Unit
# (User, WEB_CONTROL) auch bei einem erneuten Lauf wirklich uebernommen.
systemctl restart status-led-web.service
# Hauptdienst neu starten, damit die Statusdatei sicher erzeugt wird
systemctl try-restart status-led.service 2>/dev/null || true
c_ok "Web-Dienst aktiviert."

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
c_ok "Fertig. Aufruf im Browser:  http://${IP:-<pi-ip>}:$WEB_PORT/"
echo "    Benutzer: $WEB_USERNAME"
echo "    Status:   status-led web    |    Log: journalctl -u status-led-web -f"
if [ "$WEB_CONTROL" = "1" ]; then
    c_warn "Steuerung aktiv: Update + Dienst-Neustart per Button moeglich (Web laeuft als root)."
fi
