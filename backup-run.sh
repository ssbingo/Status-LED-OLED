#!/usr/bin/env bash
#
# Backup-Runner fuer Status-LED: sichert per restic auf eine SMB-Freigabe und
# meldet den Verlauf ueber /run/status-led/backup (running/ok/failed), sodass
# LED/OLED den Backup-Status automatisch anzeigen.
#
# Wird vom systemd-Dienst status-led-backup.service aufgerufen (von
# setup-backup.sh erzeugt). Manuell: sudo status-led backup
#
# Konfiguration: /etc/status-led/backup.env (+ restic-password, smb-credentials)

set -uo pipefail   # bewusst kein -e: Fehler werden abgefangen und als Status gemeldet

ENV_FILE="/etc/status-led/backup.env"
if [ ! -r "$ENV_FILE" ]; then
    echo "FEHLER: $ENV_FILE fehlt - bitte 'status-led backup-setup' ausfuehren." >&2
    exit 1
fi
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

STATUS_FILE="${STATUS_FILE:-/run/status-led/backup}"
write_status() {
    mkdir -p "$(dirname "$STATUS_FILE")" 2>/dev/null || true
    echo "$1" > "$STATUS_FILE" 2>/dev/null || true
}

if [ "${BACKUP_ENABLED:-0}" != "1" ]; then
    echo "Backup ist deaktiviert (BACKUP_ENABLED!=1)."
    exit 0
fi

MOUNTED_BY_US=0
cleanup() {
    if [ "$MOUNTED_BY_US" = "1" ]; then
        umount "$MOUNTPOINT" 2>/dev/null || true
    fi
}
trap cleanup EXIT

fail() {
    echo "FEHLER: $*" >&2
    write_status failed
    exit 1
}

write_status running

# --- Ziel bereitstellen (SMB-Freigabe on-demand mounten) ---------------------
if [ "${TARGET_TYPE:-smb}" = "smb" ]; then
    mkdir -p "$MOUNTPOINT"
    if ! mountpoint -q "$MOUNTPOINT"; then
        opts="credentials=$SMB_CREDENTIALS,uid=0,gid=0,file_mode=0600,dir_mode=0700"
        [ -n "${SMB_OPTIONS:-}" ] && opts="$opts,$SMB_OPTIONS"
        mount -t cifs "$SMB_SHARE" "$MOUNTPOINT" -o "$opts" \
            || fail "Mount der Freigabe $SMB_SHARE fehlgeschlagen"
        MOUNTED_BY_US=1
    fi
    mkdir -p "$RESTIC_REPOSITORY"
fi

export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE

# --- Repository initialisieren, falls noch nicht vorhanden -------------------
if ! restic cat config >/dev/null 2>&1; then
    echo "Initialisiere restic-Repository unter $RESTIC_REPOSITORY ..."
    restic init || fail "restic init fehlgeschlagen"
fi

# --- Backup ------------------------------------------------------------------
exargs=()
for e in ${BACKUP_EXCLUDES:-}; do
    exargs+=(--exclude "$e")
done

echo "Starte Backup der Pfade: ${BACKUP_SOURCES}"
# BACKUP_SOURCES bewusst ohne Quotes (mehrere Pfade per Wort-Splitting)
# shellcheck disable=SC2086
if restic backup --tag status-led "${exargs[@]}" ${BACKUP_SOURCES}; then
    # --- Aufbewahrung / Prune ---
    fargs=()
    [ -n "${KEEP_DAILY:-}" ]   && fargs+=(--keep-daily "$KEEP_DAILY")
    [ -n "${KEEP_WEEKLY:-}" ]  && fargs+=(--keep-weekly "$KEEP_WEEKLY")
    [ -n "${KEEP_MONTHLY:-}" ] && fargs+=(--keep-monthly "$KEEP_MONTHLY")
    if [ "${#fargs[@]}" -gt 0 ]; then
        echo "Raeume alte Snapshots auf (forget --prune) ..."
        restic forget --prune "${fargs[@]}" || echo "Warnung: forget/prune fehlgeschlagen"
    fi
    write_status ok
    echo "Backup erfolgreich abgeschlossen."
else
    fail "restic backup fehlgeschlagen"
fi
