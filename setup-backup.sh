#!/usr/bin/env bash
#
# Konfigurations-Assistent fuer das restic-Backup der Status-LED.
# Fragt Haeufigkeit, Uhrzeit, SMB-Ziel, Zugangsdaten, Quellpfade und
# Aufbewahrung ab, schreibt die Konfiguration, erzeugt systemd-Service + -Timer
# und kann anschliessend sofort ein erstes Backup starten.
#
# Aufruf:  sudo status-led backup-setup   (oder sudo /opt/status-led/setup-backup.sh)

set -euo pipefail

INSTALL_DIR="/opt/status-led"
CONFIG_DIR="/etc/status-led"
ENV_FILE="$CONFIG_DIR/backup.env"
CRED_FILE="$CONFIG_DIR/smb-credentials"
PASS_FILE="$CONFIG_DIR/restic-password"
SERVICE_FILE="/etc/systemd/system/status-led-backup.service"
TIMER_FILE="/etc/systemd/system/status-led-backup.timer"

c_info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; }

# --- Eingaben ueber das Terminal (auch bei 'curl | sudo bash') ---------------
TTY="/dev/tty"
have_tty() { [ -e "$TTY" ] && [ -r "$TTY" ]; }
ask() {  # ask "Frage" "default" -> echo Antwort
    local prompt="$1" def="${2:-}" ans=""
    local suffix=""; [ -n "$def" ] && suffix=" [$def]"
    if have_tty; then read -r -p "$prompt$suffix: " ans <"$TTY" || true; fi
    echo "${ans:-$def}"
}
ask_secret() {  # ask_secret "Frage" -> echo Eingabe (verdeckt)
    local prompt="$1" ans=""
    if have_tty; then read -r -s -p "$prompt: " ans <"$TTY" || true; echo >"$TTY"; fi
    echo "$ans"
}
ask_yesno() {  # ask_yesno "Frage" defaultBool
    local prompt="$1" def="$2" d ans
    [ "$def" = "1" ] && d="J/n" || d="j/N"
    ans="$(ask "$prompt ($d)" "")"
    [ -z "$ans" ] && { [ "$def" = "1" ]; return; }
    case "${ans,,}" in j|ja|y|yes) return 0;; *) return 1;; esac
}

if [ "$(id -u)" -ne 0 ]; then
    c_err "Bitte mit Root-Rechten ausfuehren (sudo)."
    exit 1
fi
if ! have_tty; then
    c_err "Kein Terminal verfuegbar - der Backup-Assistent braucht interaktive Eingaben."
    exit 1
fi

echo "============================================================"
echo " Status-LED  --  restic-Backup einrichten"
echo "============================================================"
echo "Sichert per restic auf eine SMB-Netzwerkfreigabe. LED/OLED zeigen"
echo "den Verlauf automatisch (cyan = laeuft, magenta = Fehler)."
echo

if ! ask_yesno "Automatisches Backup einrichten/aktivieren?" 1; then
    if [ -f "$ENV_FILE" ]; then
        sed -i 's/^BACKUP_ENABLED=.*/BACKUP_ENABLED=0/' "$ENV_FILE"
        systemctl disable --now status-led-backup.timer 2>/dev/null || true
        c_ok "Backup deaktiviert (Konfiguration bleibt erhalten)."
    else
        c_info "Kein Backup eingerichtet."
    fi
    exit 0
fi

# --- Benoetigte Pakete -------------------------------------------------------
need_pkgs=()
command -v restic  >/dev/null 2>&1 || need_pkgs+=(restic)
command -v mount.cifs >/dev/null 2>&1 || need_pkgs+=(cifs-utils)
if [ "${#need_pkgs[@]}" -gt 0 ]; then
    c_info "Installiere benoetigte Pakete: ${need_pkgs[*]} ..."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need_pkgs[@]}"
fi

# --- SMB-Ziel ----------------------------------------------------------------
echo; c_info "Ziel (SMB-Netzwerkfreigabe)"
SMB_SHARE="$(ask 'Freigabe (//Server/Freigabe)' "${SMB_SHARE:-//nas/backup}")"
SMB_USER="$(ask 'SMB-Benutzername' "${SMB_USER:-}")"
SMB_PASS="$(ask_secret 'SMB-Passwort')"
SMB_DOMAIN="$(ask 'SMB-Domain/Workgroup (optional, leer lassen)' "")"
MOUNTPOINT="$(ask 'Lokaler Mountpoint' '/mnt/status-led-backup')"
SMB_SUBDIR="$(ask 'Unterordner auf der Freigabe fuer das Repo' 'restic')"
SMB_OPTIONS="$(ask 'Zusaetzliche Mount-Optionen (optional, z. B. vers=3.0)' '')"

# --- restic-Repo-Passwort ----------------------------------------------------
echo; c_info "restic-Repository-Passwort (verschluesselt das Backup - gut merken!)"
while :; do
    RPW1="$(ask_secret 'restic-Passwort')"
    RPW2="$(ask_secret 'restic-Passwort wiederholen')"
    [ -n "$RPW1" ] && [ "$RPW1" = "$RPW2" ] && break
    c_warn "Passwoerter leer oder ungleich - bitte erneut."
done

# --- Quellpfade & Ausschluesse ----------------------------------------------
echo; c_info "Was soll gesichert werden?"
BACKUP_SOURCES="$(ask 'Quellpfade (durch Leerzeichen getrennt)' '/home /etc')"
BACKUP_EXCLUDES="$(ask 'Ausschluesse (optional, Leerzeichen-getrennt)' '')"

# --- Aufbewahrung ------------------------------------------------------------
echo; c_info "Aufbewahrung (restic forget --prune nach jedem Lauf)"
KEEP_DAILY="$(ask 'Taegliche Snapshots behalten' '7')"
KEEP_WEEKLY="$(ask 'Woechentliche Snapshots behalten' '4')"
KEEP_MONTHLY="$(ask 'Monatliche Snapshots behalten' '6')"

# --- Zeitplan ----------------------------------------------------------------
echo; c_info "Zeitplan"
echo "  1) taeglich"
echo "  2) woechentlich (montags)"
echo "  3) monatlich (am 1.)"
FREQ_CHOICE="$(ask 'Haeufigkeit (1/2/3)' '1')"
while :; do
    BACKUP_TIME="$(ask 'Uhrzeit (hh:mm)' '03:30')"
    if [[ "$BACKUP_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then break; fi
    c_warn "Bitte im Format hh:mm (00:00 - 23:59)."
done
HH="${BACKUP_TIME%%:*}"; MM="${BACKUP_TIME##*:}"
case "$FREQ_CHOICE" in
    2) ONCALENDAR="Mon *-*-* $HH:$MM:00"; FREQ="weekly"  ;;
    3) ONCALENDAR="*-*-01 $HH:$MM:00";   FREQ="monthly" ;;
    *) ONCALENDAR="*-*-* $HH:$MM:00";    FREQ="daily"   ;;
esac

RESTIC_REPOSITORY="$MOUNTPOINT/$SMB_SUBDIR"

# --- Dateien schreiben -------------------------------------------------------
mkdir -p "$CONFIG_DIR"

umask 077
printf '%s\n' "$RPW1" > "$PASS_FILE"
{
    echo "username=$SMB_USER"
    echo "password=$SMB_PASS"
    [ -n "$SMB_DOMAIN" ] && echo "domain=$SMB_DOMAIN"
} > "$CRED_FILE"
chmod 600 "$PASS_FILE" "$CRED_FILE"

cat > "$ENV_FILE" <<EOF
# Status-LED Backup-Konfiguration (erzeugt von setup-backup.sh)
BACKUP_ENABLED=1
TARGET_TYPE=smb
SMB_SHARE="$SMB_SHARE"
SMB_CREDENTIALS="$CRED_FILE"
SMB_OPTIONS="$SMB_OPTIONS"
MOUNTPOINT="$MOUNTPOINT"
RESTIC_REPOSITORY="$RESTIC_REPOSITORY"
RESTIC_PASSWORD_FILE="$PASS_FILE"
BACKUP_SOURCES="$BACKUP_SOURCES"
BACKUP_EXCLUDES="$BACKUP_EXCLUDES"
KEEP_DAILY="$KEEP_DAILY"
KEEP_WEEKLY="$KEEP_WEEKLY"
KEEP_MONTHLY="$KEEP_MONTHLY"
STATUS_FILE="/run/status-led/backup"
EOF
chmod 640 "$ENV_FILE"
umask 022
c_ok "Konfiguration geschrieben: $ENV_FILE (Secrets: $PASS_FILE, $CRED_FILE)"

# --- systemd-Service + -Timer ------------------------------------------------
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Status-LED restic backup
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/backup-run.sh
EOF

cat > "$TIMER_FILE" <<EOF
[Unit]
Description=Status-LED restic backup timer ($FREQ um $BACKUP_TIME)

[Timer]
OnCalendar=$ONCALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now status-led-backup.timer >/dev/null 2>&1 || true
c_ok "Zeitplan aktiv: $FREQ um $BACKUP_TIME  (OnCalendar=$ONCALENDAR)"
echo
systemctl list-timers status-led-backup.timer --no-pager 2>/dev/null || true

# --- Erstes Backup -----------------------------------------------------------
echo
if ask_yesno "Jetzt ein erstes Backup starten?" 1; then
    c_info "Starte erstes Backup (initialisiert das Repository) ..."
    if systemctl start status-led-backup.service; then
        c_ok "Backup-Lauf beendet."
    else
        c_warn "Backup-Lauf meldete einen Fehler."
    fi
    echo "--- letzte Log-Zeilen ---"
    journalctl -u status-led-backup -n 25 --no-pager 2>/dev/null || true
else
    c_info "Spaeter manuell: sudo status-led backup"
fi
