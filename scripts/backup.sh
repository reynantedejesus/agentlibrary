#!/usr/bin/env bash
#
# Agent Library backup — database + uploaded files.
#
#   sudo /opt/agentlibrary/scripts/backup.sh
#   sudo /opt/agentlibrary/scripts/backup.sh /mnt/backups
#
# Cron (03:15 daily):
#   sudo crontab -e
#   15 3 * * * /opt/agentlibrary/scripts/backup.sh >> /var/log/agentlibrary/backup.log 2>&1
#
# The database dump is consistent on its own (--single-transaction on InnoDB
# takes a snapshot without locking writers). Files are copied afterwards, so a
# file uploaded mid-run may be in the archive without a matching database row —
# harmless, because the restore only ever adds orphans, never loses rows.
set -euo pipefail

BACKUP_ROOT="${1:-/var/backups/agentlibrary}"
ENV_FILE="${ENV_FILE:-/etc/agentlibrary/agentlibrary.env}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"

log() { printf '%s  %s\n' "$(date --iso-8601=seconds)" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

[[ -r "$ENV_FILE" ]] || fail "Cannot read $ENV_FILE"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${MYSQL_DATABASE:?MYSQL_DATABASE not set}"
: "${MYSQL_USER:?MYSQL_USER not set}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD not set}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
UPLOAD_DIR="${UPLOAD_DIR:-/var/lib/agentlibrary/uploads}"

DEST="${BACKUP_ROOT}/${STAMP}"
mkdir -p "$DEST"
chmod 700 "$BACKUP_ROOT" "$DEST"

# Keep the password out of the process list and off the command line.
CNF="$(mktemp)"
chmod 600 "$CNF"
trap 'rm -f "$CNF"' EXIT
cat > "$CNF" <<CNFEOF
[client]
host=${MYSQL_HOST}
port=${MYSQL_PORT}
user=${MYSQL_USER}
password=${MYSQL_PASSWORD}
CNFEOF

log "Dumping database ${MYSQL_DATABASE}"
mysqldump --defaults-extra-file="$CNF" \
    --single-transaction \
    --quick \
    --routines \
    --triggers \
    --events \
    --set-gtid-purged=OFF \
    --default-character-set=utf8mb4 \
    "$MYSQL_DATABASE" | gzip -9 > "${DEST}/database.sql.gz"

[[ -s "${DEST}/database.sql.gz" ]] || fail "Database dump is empty"
log "Database dump: $(du -h "${DEST}/database.sql.gz" | cut -f1)"

if [[ -d "$UPLOAD_DIR" ]]; then
    log "Archiving uploads from ${UPLOAD_DIR}"
    tar -czf "${DEST}/uploads.tar.gz" -C "$(dirname "$UPLOAD_DIR")" "$(basename "$UPLOAD_DIR")"
    log "Uploads archive: $(du -h "${DEST}/uploads.tar.gz" | cut -f1)"
else
    log "WARNING: upload directory ${UPLOAD_DIR} does not exist — skipping"
fi

# Record what produced this backup, so a restore knows which migration to
# expect. Never record credentials.
cat > "${DEST}/manifest.txt" <<MANIFESTEOF
timestamp=${STAMP}
host=$(hostname -f)
database=${MYSQL_DATABASE}
upload_dir=${UPLOAD_DIR}
alembic_head=$(cd /opt/agentlibrary 2>/dev/null && \
    FLASK_APP=wsgi.py /opt/agentlibrary/.venv/bin/flask db current 2>/dev/null | tail -1 || echo unknown)
MANIFESTEOF

sha256sum "${DEST}"/* > "${DEST}/SHA256SUMS" 2>/dev/null || true
chmod 600 "${DEST}"/*

log "Pruning backups older than ${RETENTION_DAYS} days"
find "$BACKUP_ROOT" -maxdepth 1 -type d -name '20*' -mtime "+${RETENTION_DAYS}" \
    -exec rm -rf {} + 2>/dev/null || true

log "Backup complete: ${DEST}"
