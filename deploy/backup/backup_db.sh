#!/usr/bin/env bash
# Nightly SQLite backup for the VPS side.
#
# Flow: consistent snapshot (sqlite3 .backup, safe even while the app is
# writing -- NOT a raw `cp`, which could grab the file mid-write) -> gzip
# -> encrypt with age using a PUBLIC key only. This machine never holds the
# private key, so even a fully compromised VPS can't decrypt any backup
# sitting in BACKUP_DIR -- only the home PC (which holds the private key,
# see pull_backup.ps1) can. Old encrypted backups are rotated out locally;
# the home PC pulls them down daily and keeps its own longer retention.
#
# Setup (once, after the VPS is active):
#   sudo apt install age sqlite3
#   age-keygen -o /tmp/key.txt        # generates a keypair
#   grep 'public key' /tmp/key.txt    # copy the "age1..." line into
#                                     # AGE_RECIPIENT below (or into
#                                     # age_recipient.txt, see below)
#   scp /tmp/key.txt <home-pc>:...    # move the PRIVATE key off this VPS
#   rm /tmp/key.txt                   # then delete it here -- this VPS
#                                     # should never retain the private key
#   crontab -e
#     30 23 * * * /opt/nse-momentum-dashboard/deploy/backup/backup_db.sh >> /opt/nse-momentum-dashboard/backups/cron.log 2>&1

set -euo pipefail

APP_DIR="/opt/nse-momentum-dashboard"
DB_PATH="$APP_DIR/cache/state.db"
BACKUP_DIR="$APP_DIR/backups"
# Either paste the "age1..." public key string directly here, or point at a
# file containing it (one recipient per line) via AGE_RECIPIENT_FILE --
# only ONE of the two is needed.
AGE_RECIPIENT="age1cgffllrwxhep70zrsld2guf5f4jpmkw6sehz8756d5nlwfzl0qrsu57qnn"
AGE_RECIPIENT_FILE="$BACKUP_DIR/age_recipient.txt"
RETENTION_DAYS=14
LOG_FILE="$BACKUP_DIR/backup.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"; }

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    log "FAILED -- $DB_PATH not found"
    exit 1
fi

if [ -z "$AGE_RECIPIENT" ] && [ ! -f "$AGE_RECIPIENT_FILE" ]; then
    log "FAILED -- no age recipient configured (set AGE_RECIPIENT or create $AGE_RECIPIENT_FILE)"
    exit 1
fi
RECIPIENT_ARGS=()
if [ -n "$AGE_RECIPIENT" ]; then
    RECIPIENT_ARGS=(-r "$AGE_RECIPIENT")
else
    RECIPIENT_ARGS=(-R "$AGE_RECIPIENT_FILE")
fi

TS=$(date +%Y%m%d_%H%M%S)
SNAPSHOT="$BACKUP_DIR/state_$TS.db"
ENCRYPTED="$BACKUP_DIR/state_$TS.db.gz.age"

if ! sqlite3 "$DB_PATH" ".backup '$SNAPSHOT'"; then
    log "FAILED -- sqlite3 .backup command failed"
    rm -f "$SNAPSHOT"
    exit 1
fi

if ! gzip -c "$SNAPSHOT" | age "${RECIPIENT_ARGS[@]}" -o "$ENCRYPTED"; then
    log "FAILED -- compression/encryption failed"
    rm -f "$SNAPSHOT" "$ENCRYPTED"
    exit 1
fi
rm -f "$SNAPSHOT"  # only the encrypted copy sticks around

log "OK -- backup created: $(basename "$ENCRYPTED") ($(du -h "$ENCRYPTED" | cut -f1))"

find "$BACKUP_DIR" -name "state_*.db.gz.age" -mtime "+$RETENTION_DAYS" -delete
log "Rotation complete -- keeping last $RETENTION_DAYS days on the VPS."
