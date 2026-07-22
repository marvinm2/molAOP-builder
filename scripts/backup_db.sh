#!/bin/bash
# /app/scripts/backup_db.sh
# SQLite backup using Online Backup API (safe during active writes).
# Source: https://sqlite.org/backup.html
#
# Scheduled daily at 02:00 UTC as a Swarm cron job, NOT by a cron daemon inside
# this container — see "Database backups" in CLAUDE.md. The job runs this same
# script in a one-shot container off the same image, with the data directory
# bind-mounted, so it works on whichever node the scheduler picks.
#
# Run it by hand after significant curation:
#   ssh tgx1 "docker exec \$(docker ps -qf name=molaop-builder) /app/scripts/backup_db.sh"
set -euo pipefail

# Overridable so the Swarm job (and local testing) can point at other paths
# without editing the script.
DB_PATH="${DB_PATH:-/app/data/ke_wp_mapping.db}"
BACKUP_DIR="${BACKUP_DIR:-/app/data/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/ke_wp_mapping_${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

# .backup uses the Online Backup API: checkpoints WAL, then copies atomically.
# DO NOT back up just the .db file without the -wal and -shm files;
# always use this script to get a consistent snapshot.
sqlite3 "${DB_PATH}" ".backup '${BACKUP_FILE}'"

# Integrity check — remove backup if corrupted
RESULT=$(sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;")
if [ "${RESULT}" != "ok" ]; then
    echo "[BACKUP ERROR] Integrity check failed for ${BACKUP_FILE}: ${RESULT}" >&2
    rm -f "${BACKUP_FILE}"
    exit 1
fi

echo "[BACKUP OK] ${BACKUP_FILE}"

# Prune backups older than retention period
find "${BACKUP_DIR}" -name "ke_wp_mapping_*.db" -mtime "+${RETENTION_DAYS}" -delete
