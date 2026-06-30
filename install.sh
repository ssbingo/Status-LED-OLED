#!/usr/bin/env bash
#
# Installer fuer Status-LED + OLED auf dem Raspberry Pi.
#
# Schnellinstallation (eine Zeile):
#   curl -fsSL https://raw.githubusercontent.com/ssbingo/Status-LED-OLED/main/install.sh | sudo bash
#
# Der Installer ist idempotent: erneutes Ausfuehren aktualisiert die Installation.
# Update spaeter:  sudo status-led update   (bzw. /opt/status-led/update.sh)

set -euo pipefail

REPO_URL="https://github.com/ssbingo/Status-LED-OLED.git"
BRANCH="main"
INSTALL_DIR="/opt/status-led"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_DIR="/etc/status-led"
CONFIG_FILE="$CONFIG_DIR/config.toml"
SERVICE_FILE="/etc/systemd/system/status-led.service"
WRAPPER="/usr/local/bin/status-led"

REBOOT_NEEDED=0

c_info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
c_err()   { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; }

# --- Eingaben gehen ueber das Terminal, auch bei 'curl | sudo bash' -----------
TTY="/dev/tty"
have_tty() { [ -e "$TTY" ] && [ -r "$TTY" ]; }
ask_tty() {  # ask_tty "Frage (J/n)" -> echo Antwort
    local prompt="$1" ans=""
    if have_tty; then read -r -p "$prompt" ans <"$TTY" || true; fi
    echo "$ans"
}

# --- Vorpruefungen ------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    c_err "Bitte mit Root-Rechten ausfuehren (sudo)."
    exit 1
fi

MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo '')"
case "$MODEL" in
    *Raspberry*) c_ok "Erkannt: $MODEL" ;;
    *)
        c_warn "Kein Raspberry Pi erkannt (${MODEL:-unbekannt})."
        c_warn "Ohne echte GPIO-/I2C-/SPI-Pins funktioniert die Hardware nicht."
        a="$(ask_tty 'Trotzdem fortfahren? (j/N): ')"
        case "${a,,}" in j|ja|y|yes) ;; *) echo "Abgebrochen."; exit 1 ;; esac
        ;;
esac

# config.txt-Pfad (Bookworm vs. aelter)
BOOT_CONFIG="/boot/firmware/config.txt"
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG="/boot/config.txt"

# --- System-Pakete ------------------------------------------------------------
c_info "Installiere System-Pakete (apt)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    git python3-venv python3-dev i2c-tools \
    fonts-dejavu-core smartmontools build-essential
c_ok "System-Pakete installiert."

# --- Repository holen / aktualisieren -----------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
    c_info "Aktualisiere vorhandene Installation in $INSTALL_DIR..."
    git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"
    git -C "$INSTALL_DIR" reset --hard --quiet "origin/$BRANCH"
else
    c_info "Klone Repository nach $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
c_ok "Quellcode bereit ($(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || echo unbekannt))."

# --- Python-venv + Bibliotheken -----------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    c_info "Erstelle virtuelle Python-Umgebung..."
    python3 -m venv "$VENV_DIR"
fi
c_info "Installiere/aktualisiere Python-Bibliotheken (kann dauern)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet --upgrade -r "$INSTALL_DIR/requirements.txt"
c_ok "Python-Bibliotheken bereit."

# --- I2C aktivieren (fuer OLED) -----------------------------------------------
if command -v raspi-config >/dev/null 2>&1; then
    c_info "Aktiviere I2C..."
    raspi-config nonint do_i2c 0 || c_warn "I2C konnte nicht automatisch aktiviert werden."
    REBOOT_NEEDED=1
else
    c_warn "raspi-config nicht gefunden - I2C ggf. manuell aktivieren."
fi

# --- Konfigurations-Assistent -------------------------------------------------
mkdir -p "$CONFIG_DIR"
if have_tty; then
    c_info "Starte Konfigurations-Assistent..."
    "$VENV_DIR/bin/python" "$INSTALL_DIR/status_led.py" --setup --config "$CONFIG_FILE" <"$TTY" || \
        c_warn "Assistent abgebrochen - es gelten die Standardwerte."
else
    c_warn "Kein Terminal verfuegbar - ueberspringe den Assistenten."
    c_warn "Spaeter ausfuehren mit:  sudo status-led setup"
fi

# --- LED-Typ aus der Konfiguration lesen (steuert Service-User/Interfaces) ----
LED_TYPE="ws2812"
if [ -f "$CONFIG_FILE" ]; then
    LED_TYPE="$("$VENV_DIR/bin/python" - "$CONFIG_FILE" <<'PY'
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        print(tomllib.load(f).get("led_type", "ws2812"))
except Exception:
    print("ws2812")
PY
)"
fi
c_ok "LED-Typ: $LED_TYPE"

SERVICE_USER="root"
case "$LED_TYPE" in
    ws2812-spi)
        SERVICE_USER="${SUDO_USER:-root}"
        if command -v raspi-config >/dev/null 2>&1; then
            c_info "Aktiviere SPI (fuer ws2812-spi)..."
            raspi-config nonint do_spi 0 || c_warn "SPI konnte nicht automatisch aktiviert werden."
            REBOOT_NEEDED=1
        fi
        if [ "$SERVICE_USER" != "root" ]; then
            usermod -aG spi,i2c "$SERVICE_USER" 2>/dev/null || true
        fi
        ;;
    ws2812|analog)
        # PWM teilt sich die Hardware mit dem Onboard-Audio -> Audio deaktivieren
        if grep -q '^dtparam=audio=on' "$BOOT_CONFIG" 2>/dev/null; then
            sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$BOOT_CONFIG"
            REBOOT_NEEDED=1
        elif ! grep -q '^dtparam=audio=off' "$BOOT_CONFIG" 2>/dev/null; then
            echo "dtparam=audio=off" >> "$BOOT_CONFIG"
            REBOOT_NEEDED=1
        fi
        c_ok "Onboard-Audio deaktiviert (PWM-Voraussetzung)."
        ;;
esac

# --- systemd-Service ----------------------------------------------------------
c_info "Schreibe systemd-Service (User=$SERVICE_USER)..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=RGB Status LED + OLED
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python $INSTALL_DIR/status_led.py --config $CONFIG_FILE
Restart=always
RestartSec=5
User=$SERVICE_USER
RuntimeDirectory=status-led

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now status-led.service >/dev/null 2>&1 || systemctl restart status-led.service
c_ok "Dienst aktiviert und gestartet."

# --- CLI-Wrapper installieren -------------------------------------------------
install -m 0755 "$INSTALL_DIR/status-led" "$WRAPPER" 2>/dev/null || {
    cp "$INSTALL_DIR/status-led" "$WRAPPER"; chmod 0755 "$WRAPPER";
}
c_ok "Befehl 'status-led' installiert."

# --- Optionales restic-Backup -------------------------------------------------
echo
if have_tty; then
    a="$(ask_tty 'Automatisches restic-Backup jetzt einrichten? (j/N): ')"
    case "${a,,}" in
        j|ja|y|yes) "$INSTALL_DIR/setup-backup.sh" || c_warn "Backup-Assistent abgebrochen." ;;
        *) c_info "Backup uebersprungen (spaeter: sudo status-led backup-setup)" ;;
    esac
else
    c_info "Kein Terminal - Backup spaeter mit: sudo status-led backup-setup"
fi

# --- Optionales Web-Dashboard -------------------------------------------------
echo
if have_tty; then
    a="$(ask_tty 'Web-Dashboard jetzt einrichten? (j/N): ')"
    case "${a,,}" in
        j|ja|y|yes) "$INSTALL_DIR/setup-web.sh" || c_warn "Web-Assistent abgebrochen." ;;
        *) c_info "Web-Dashboard uebersprungen (spaeter: sudo status-led web-setup)" ;;
    esac
else
    c_info "Kein Terminal - Web-Dashboard spaeter mit: sudo status-led web-setup"
fi

# --- Abschluss ----------------------------------------------------------------
echo
c_info "Fertig. Naechste Schritte:"
echo "    status-led status     # Dienststatus"
echo "    status-led logs       # Live-Log"
echo "    status-led setup        # Konfiguration erneut anpassen"
echo "    status-led backup-setup # Backup einrichten/aendern"
echo "    status-led web-setup    # Web-Dashboard einrichten/aendern"
echo "    status-led update       # spaeter auf neue Version aktualisieren"
echo
if [ "$REBOOT_NEEDED" -eq 1 ]; then
    c_warn "Interface-/Audio-Aenderungen brauchen einen Neustart."
    a="$(ask_tty 'Jetzt neu starten? (j/N): ')"
    case "${a,,}" in j|ja|y|yes) c_info "Starte neu..."; reboot ;; *) c_warn "Bitte spaeter manuell neu starten: sudo reboot" ;; esac
fi
