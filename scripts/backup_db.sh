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
# How long to wait for a concurrent writer before giving up (#276).
BUSY_TIMEOUT_MS="${BUSY_TIMEOUT_MS:-60000}"
# A real backup of this database is ~40 MB. Anything under a few pages is a
# failed one, whatever PRAGMA integrity_check says about it.
MIN_BACKUP_BYTES="${MIN_BACKUP_BYTES:-65536}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/ke_wp_mapping_${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

# Remove a partial file on any failure below, so a run that dies never leaves
# something behind that sorts as the newest backup (#276).
trap 'rm -f "${BACKUP_FILE}"' ERR

# .backup uses the Online Backup API: checkpoints WAL, then copies atomically.
# DO NOT back up just the .db file without the -wal and -shm files;
# always use this script to get a consistent snapshot.
#
# The API is safe during active writes, but the CLI opens with no busy timeout,
# so a concurrent writer made it fail outright ("database is locked") instead of
# waiting. That is why every 02:00 run succeeded and the first ad-hoc daytime run
# did not. Wait instead of failing.
if ! sqlite3 -cmd ".timeout ${BUSY_TIMEOUT_MS}" "${DB_PATH}" ".backup '${BACKUP_FILE}'"; then
    echo "[BACKUP ERROR] sqlite3 .backup failed for ${DB_PATH}" >&2
    rm -f "${BACKUP_FILE}"
    exit 1
fi

# Completion check. SQLite treats a zero-length file as a valid empty database,
# so PRAGMA integrity_check returns "ok" on exactly the failure it most needs to
# catch — it is a corruption guard and was never a completion guard. Size first.
BACKUP_BYTES=$(wc -c < "${BACKUP_FILE}")
if [ "${BACKUP_BYTES}" -lt "${MIN_BACKUP_BYTES}" ]; then
    echo "[BACKUP ERROR] ${BACKUP_FILE} is ${BACKUP_BYTES} bytes, under the ${MIN_BACKUP_BYTES}-byte floor" >&2
    rm -f "${BACKUP_FILE}"
    exit 1
fi

# Integrity check — remove backup if corrupted
RESULT=$(sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;")
if [ "${RESULT}" != "ok" ]; then
    echo "[BACKUP ERROR] Integrity check failed for ${BACKUP_FILE}: ${RESULT}" >&2
    rm -f "${BACKUP_FILE}"
    exit 1
fi

# Content check — a structurally valid database with no mappings in it is not a
# backup of this database.
MAPPING_COUNT=$(sqlite3 "${BACKUP_FILE}" "SELECT COUNT(*) FROM mappings;")
if [ "${MAPPING_COUNT}" -lt 1 ]; then
    echo "[BACKUP ERROR] ${BACKUP_FILE} holds no mappings" >&2
    rm -f "${BACKUP_FILE}"
    exit 1
fi

trap - ERR
echo "[BACKUP OK] ${BACKUP_FILE} — ${BACKUP_BYTES} bytes, integrity ok, ${MAPPING_COUNT} mappings"

# Prune backups older than retention period. The glob covers "-wal"/"-shm"
# sidecars too: a clean sqlite3 close removes them, but one left behind by an
# interrupted read matched no prune pattern and stayed forever.
find "${BACKUP_DIR}" -name "ke_wp_mapping_*.db*" -mtime "+${RETENTION_DAYS}" -delete
# Prune empty backups regardless of age — any left by a run that predates the
# checks above would otherwise sit there for a full retention period, sorting
# newest.
find "${BACKUP_DIR}" -name "ke_wp_mapping_*.db" -size 0 -delete
