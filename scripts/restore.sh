#!/usr/bin/env bash
#
# Agent Library restore — database + uploaded files.
#
#   sudo /var/www/agentlibrary/scripts/restore.sh /var/backups/agentlibrary/20260820-031500
#
# THIS OVERWRITES THE CURRENT DATABASE AND UPLOAD DIRECTORY. It stops the
# service first, takes a safety dump of what is there now, restores, runs any
# outstanding migrations, and starts the service again.
set -euo pipefail

BACKUP_DIR="${1:?Usage: restore.sh /path/to/backup-directory}"
ENV_FILE="${ENV_FILE:-/etc/agentlibrary/agentlibrary.env}"
APP_DIR="${APP_DIR:-/var/www/agentlibrary}"
# Matches GUNICORN_BIND. For a Unix socket, override with e.g.
#   HEALTH_URL="--unix-socket /run/agentlibrary/agentlibrary.sock http://localhost/health"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8090/health}"

log() { printf '%s  %s\n' "$(date --iso-8601=seconds)" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

[[ -d "$BACKUP_DIR" ]] || fail "No such backup directory: $BACKUP_DIR"
[[ -r "${BACKUP_DIR}/database.sql.gz" ]] || fail "Missing database.sql.gz"
[[ -r "$ENV_FILE" ]] || fail "Cannot read $ENV_FILE"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
: "${MYSQL_DATABASE:?}"; : "${MYSQL_USER:?}"; : "${MYSQL_PASSWORD:?}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
UPLOAD_DIR="${UPLOAD_DIR:-/var/lib/agentlibrary/uploads}"

if [[ -r "${BACKUP_DIR}/SHA256SUMS" ]]; then
    log "Verifying checksums"
    (cd "$BACKUP_DIR" && sha256sum --quiet --check SHA256SUMS) \
        || fail "Checksum mismatch — refusing to restore a damaged backup"
fi

cat "${BACKUP_DIR}/manifest.txt" 2>/dev/null || true
read -r -p "Restore over the CURRENT database '${MYSQL_DATABASE}'? [type RESTORE] " CONFIRM
[[ "$CONFIRM" == "RESTORE" ]] || fail "Aborted"

CNF="$(mktemp)"; chmod 600 "$CNF"; trap 'rm -f "$CNF"' EXIT
cat > "$CNF" <<CNFEOF
[client]
host=${MYSQL_HOST}
port=${MYSQL_PORT}
user=${MYSQL_USER}
password=${MYSQL_PASSWORD}
CNFEOF

log "Stopping agentlibrary"
systemctl stop agentlibrary || true

SAFETY="/var/backups/agentlibrary/pre-restore-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SAFETY"; chmod 700 "$SAFETY"
log "Taking a safety dump of the current database into ${SAFETY}"
mysqldump --defaults-extra-file="$CNF" --single-transaction --quick \
    --default-character-set=utf8mb4 "$MYSQL_DATABASE" \
    | gzip -9 > "${SAFETY}/database.sql.gz" || log "WARNING: safety dump failed"

log "Restoring database"
gunzip -c "${BACKUP_DIR}/database.sql.gz" \
    | mysql --defaults-extra-file="$CNF" --default-character-set=utf8mb4 "$MYSQL_DATABASE"

if [[ -r "${BACKUP_DIR}/uploads.tar.gz" ]]; then
    log "Restoring uploads into ${UPLOAD_DIR}"
    PARENT="$(dirname "$UPLOAD_DIR")"
    if [[ -d "$UPLOAD_DIR" ]]; then
        mv "$UPLOAD_DIR" "${UPLOAD_DIR}.pre-restore-$(date +%s)"
    fi
    mkdir -p "$PARENT"
    tar -xzf "${BACKUP_DIR}/uploads.tar.gz" -C "$PARENT"
    chown -R agentlibrary:agentlibrary "$UPLOAD_DIR"
    chmod 750 "$UPLOAD_DIR"
    command -v restorecon >/dev/null && restorecon -R "$UPLOAD_DIR" || true
else
    log "No uploads archive in the backup — leaving ${UPLOAD_DIR} as-is"
fi

log "Applying any outstanding migrations"
cd "$APP_DIR"
sudo -u agentlibrary env FLASK_APP=wsgi.py "${APP_DIR}/.venv/bin/flask" db upgrade

log "Starting agentlibrary"
systemctl start agentlibrary
sleep 3
systemctl is-active --quiet agentlibrary || fail "Service did not come back up — check journalctl -u agentlibrary"

log "Health check"
curl -fsS "${HEALTH_URL}" || fail "Health check failed"
echo
log "Restore complete. Safety dump kept at ${SAFETY}"
