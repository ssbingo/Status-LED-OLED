#!/usr/bin/env bash
#
# Wiederherstellungs-Assistent fuer die restic-Backups der Status-LED.
# Mountet die Backup-Freigabe NUR LESEND, zeigt die vorhandenen Snapshots und
# bietet einfache Optionen, um Daten zurueckzuholen - ohne Vorwissen.
#
# Aufruf:  sudo status-led restore
#
# Hinweis: Fuer die Wiederherstellung auf einem NEUEN/leeren System (Pi defekt)
# siehe den Abschnitt "Notfall-Wiederherstellung" in der README - dort steht der
# manuelle Weg, der nur das restic-Passwort und den Freigabe-Pfad braucht.

set -uo pipefail

ENV_FILE="/etc/status-led/backup.env"
RESTORE_MNT="/mnt/status-led-restore"

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

if [ "$(id -u)" -ne 0 ]; then
    c_err "Bitte mit Root-Rechten ausfuehren (sudo)."
    exit 1
fi

if [ ! -r "$ENV_FILE" ]; then
    c_err "Keine Backup-Konfiguration gefunden ($ENV_FILE)."
    echo "Auf einem frischen/neuen System bitte den Abschnitt 'Notfall-Wiederherstellung'"
    echo "in der README nutzen (manueller Weg mit restic-Passwort und Freigabe-Pfad)."
    exit 1
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

MOUNTED_BY_US=0
RESTIC_MOUNTED=0
cleanup() {
    if [ "$RESTIC_MOUNTED" = "1" ]; then
        fusermount -u "$RESTORE_MNT" 2>/dev/null || umount "$RESTORE_MNT" 2>/dev/null || true
    fi
    if [ "$MOUNTED_BY_US" = "1" ]; then
        umount "$MOUNTPOINT" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# --- Backup-Freigabe nur lesend mounten --------------------------------------
if [ "${TARGET_TYPE:-smb}" = "smb" ]; then
    mkdir -p "$MOUNTPOINT"
    if ! mountpoint -q "$MOUNTPOINT"; then
        opts="ro,credentials=$SMB_CREDENTIALS,uid=0,gid=0"
        [ -n "${SMB_OPTIONS:-}" ] && opts="$opts,$SMB_OPTIONS"
        mount -t cifs "$SMB_SHARE" "$MOUNTPOINT" -o "$opts" \
            || { c_err "Mount der Freigabe $SMB_SHARE fehlgeschlagen (Zugangsdaten/Freigabe pruefen)."; exit 1; }
        MOUNTED_BY_US=1
    fi
fi

export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE
export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-/var/cache/status-led-restic}"
mkdir -p "$RESTIC_CACHE_DIR" 2>/dev/null || true

echo
c_info "Vorhandene Sicherungen (Snapshots):"
if ! restic snapshots; then
    c_err "Konnte die Sicherungen nicht lesen (restic-Passwort oder Repository-Pfad falsch?)."
    exit 1
fi

echo
echo "Was moechtest du tun?"
echo "  1) Dateien durchsuchen und einzeln herauskopieren (empfohlen)"
echo "  2) Alles in einen Ordner wiederherstellen"
echo "  3) Einen bestimmten Pfad wiederherstellen (z. B. /etc oder eine Datei)"
echo "  4) Abbrechen"
choice="$(ask 'Auswahl (1-4)' '1')"

case "$choice" in
    1)
        if ! command -v fusermount >/dev/null 2>&1 && ! command -v fusermount3 >/dev/null 2>&1; then
            c_info "Installiere fuse (zum Durchsuchen) ..."
            DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fuse3 \
                || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fuse || true
        fi
        mkdir -p "$RESTORE_MNT"
        echo
        c_info "Das Backup wird jetzt als Ordner eingebunden unter: $RESTORE_MNT"
        echo "  Oeffne ein ZWEITES Terminal (oder einen Dateimanager) und kopiere heraus,"
        echo "  was du brauchst, z. B.:"
        echo "      ls $RESTORE_MNT/snapshots/latest/"
        echo "      cp -a $RESTORE_MNT/snapshots/latest/home/BENUTZER/datei.txt ~/"
        echo "  Wenn du fertig bist: hier Strg+C druecken."
        echo
        RESTIC_MOUNTED=1
        restic mount "$RESTORE_MNT" || true
        RESTIC_MOUNTED=0
        ;;
    2)
        tgt="$(ask 'Zielordner (wird angelegt)' '/tmp/status-led-restore')"
        mkdir -p "$tgt"
        c_info "Stelle die neueste Sicherung nach $tgt wieder her ..."
        if restic restore latest --target "$tgt"; then
            c_ok "Fertig. Die Daten liegen unter $tgt (Originalpfade als Unterordner)."
        else
            c_err "Wiederherstellung fehlgeschlagen."
        fi
        ;;
    3)
        p="$(ask 'Welcher Pfad aus dem Backup? (z. B. /etc oder /home/ich/datei.txt)' '/etc')"
        tgt="$(ask 'Zielordner (wird angelegt)' '/tmp/status-led-restore')"
        mkdir -p "$tgt"
        c_info "Stelle $p nach $tgt wieder her ..."
        if restic restore latest --target "$tgt" --include "$p"; then
            c_ok "Fertig. Siehe ${tgt}${p}"
        else
            c_err "Wiederherstellung fehlgeschlagen."
        fi
        ;;
    *)
        c_info "Abgebrochen."
        ;;
esac
