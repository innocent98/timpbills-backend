#!/bin/bash
# ============================================================
# Timpbills API Database Backup - Cron-Safe Version
#
# Designed to run unattended from cron on the VPS host.
# Runs pg_dump INSIDE the Docker container, writes to the
# mounted ./backups/ volume. No host-side dependencies.
#
# Setup on server (installed automatically by CD):
#   0 2 * * * /opt/timpbills/scripts/backup_cron.sh >> /opt/timpbills/backups/backup.log 2>&1
#
# Why this exists as a separate script from backup_db.sh:
#   - Uses absolute paths (no cd needed -- cron has no $PWD context)
#   - Reads .env via grep, NOT source -- immune to special chars in values
#   - Uses full path to docker binary
#   - Logs ISO-8601 timestamps so log correlation works
# ============================================================

set -uo pipefail

# ---- Configuration ----
DEPLOY_DIR="/opt/timpbills"
BACKUP_DIR="${DEPLOY_DIR}/backups"
ENV_FILE="${DEPLOY_DIR}/.env"
DOCKER="$(command -v docker || echo /usr/bin/docker)"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="timpbills_backup_${TIMESTAMP}.sql"
RETENTION_DAYS=30

# ---- Load .env safely (no sourcing -- avoids special char issues) ----
if [ ! -f "$ENV_FILE" ]; then
    echo "[$(date -u +%FT%TZ)] ERROR: $ENV_FILE not found"
    exit 1
fi

_env_val() { grep "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || echo ""; }

COMPOSE_PROJECT_NAME="$(_env_val COMPOSE_PROJECT_NAME)"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-timpbills}"
DB_CONTAINER="${COMPOSE_PROJECT_NAME}_db"
DB_USER="$(_env_val POSTGRES_USER)"
DB_NAME="$(_env_val POSTGRES_DB)"
ENABLE_CLOUD_BACKUP="$(_env_val ENABLE_CLOUD_BACKUP)"
CLOUD_PROVIDER="$(_env_val CLOUD_PROVIDER)"
GDRIVE_FOLDER="$(_env_val GDRIVE_FOLDER)"
DROPBOX_FOLDER="$(_env_val DROPBOX_FOLDER)"
AWS_S3_BUCKET="$(_env_val AWS_S3_BUCKET)"
B2_BUCKET="$(_env_val B2_BUCKET)"
B2_RCLONE_REMOTE="$(_env_val B2_RCLONE_REMOTE)"

# ---- Pre-flight checks ----
if [ -z "$DB_USER" ] || [ -z "$DB_NAME" ]; then
    echo "[$(date -u +%FT%TZ)] ERROR: POSTGRES_USER or POSTGRES_DB not set in .env"
    exit 1
fi

if ! $DOCKER ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    echo "[$(date -u +%FT%TZ)] ERROR: Container ${DB_CONTAINER} is not running"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# ---- Create backup ----
echo "[$(date -u +%FT%TZ)] Starting backup: ${BACKUP_FILE}"

if $DOCKER exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" \
    > "${BACKUP_DIR}/${BACKUP_FILE}"; then
    echo "[$(date -u +%FT%TZ)] pg_dump completed"
else
    echo "[$(date -u +%FT%TZ)] ERROR: pg_dump failed"
    rm -f "${BACKUP_DIR}/${BACKUP_FILE}"
    exit 1
fi

# ---- Compress ----
gzip "${BACKUP_DIR}/${BACKUP_FILE}"
BACKUP_SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_FILE}.gz" | cut -f1)
echo "[$(date -u +%FT%TZ)] Compressed: ${BACKUP_FILE}.gz (${BACKUP_SIZE})"

# ---- Cleanup old backups ----
DELETED=$(find "$BACKUP_DIR" -name "timpbills_backup_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete -print | wc -l | tr -d ' ')
[ "$DELETED" -gt 0 ] && echo "[$(date -u +%FT%TZ)] Cleaned up ${DELETED} backup(s) older than ${RETENTION_DAYS} days"

# ---- Cloud upload (optional) ----
if [ "${ENABLE_CLOUD_BACKUP:-false}" = "true" ]; then
    DATE_PATH=$(date +"%Y/%m")
    case "${CLOUD_PROVIDER:-}" in
        gdrive)
            if command -v rclone &>/dev/null; then
                rclone mkdir "gdrive:${GDRIVE_FOLDER:-timpbills-backups}/${DATE_PATH}" 2>/dev/null || true
                if rclone copy "${BACKUP_DIR}/${BACKUP_FILE}.gz" \
                    "gdrive:${GDRIVE_FOLDER:-timpbills-backups}/${DATE_PATH}/" --verbose 2>&1; then
                    echo "[$(date -u +%FT%TZ)] Uploaded to Google Drive"
                else
                    echo "[$(date -u +%FT%TZ)] WARNING: Google Drive upload failed"
                fi
            else
                echo "[$(date -u +%FT%TZ)] WARNING: rclone not installed, skipping cloud upload"
            fi
            ;;
        dropbox)
            if command -v rclone &>/dev/null; then
                rclone mkdir "dropbox:${DROPBOX_FOLDER:-timpbills-backups}/${DATE_PATH}" 2>/dev/null || true
                if rclone copy "${BACKUP_DIR}/${BACKUP_FILE}.gz" \
                    "dropbox:${DROPBOX_FOLDER:-timpbills-backups}/${DATE_PATH}/" --verbose 2>&1; then
                    echo "[$(date -u +%FT%TZ)] Uploaded to Dropbox"
                else
                    echo "[$(date -u +%FT%TZ)] WARNING: Dropbox upload failed"
                fi
            else
                echo "[$(date -u +%FT%TZ)] WARNING: rclone not installed, skipping Dropbox upload"
            fi
            ;;
        s3)
            if command -v aws &>/dev/null; then
                aws s3 cp "${BACKUP_DIR}/${BACKUP_FILE}.gz" \
                    "s3://${AWS_S3_BUCKET:-timpbills-backups}/backups/${DATE_PATH}/${BACKUP_FILE}.gz" 2>/dev/null \
                    && echo "[$(date -u +%FT%TZ)] Uploaded to S3" \
                    || echo "[$(date -u +%FT%TZ)] WARNING: S3 upload failed"
            else
                echo "[$(date -u +%FT%TZ)] WARNING: aws CLI not installed, skipping S3 upload"
            fi
            ;;
        b2)
            # Backblaze B2 via rclone. Configure once with: rclone config (choose "Backblaze B2")
            # Expected env: B2_BUCKET. Optional B2_RCLONE_REMOTE (defaults to "b2").
            if command -v rclone &>/dev/null; then
                B2_REMOTE="${B2_RCLONE_REMOTE:-b2}"
                rclone mkdir "${B2_REMOTE}:${B2_BUCKET:-timpbills-backups}/${DATE_PATH}" 2>/dev/null || true
                if rclone copy "${BACKUP_DIR}/${BACKUP_FILE}.gz" \
                    "${B2_REMOTE}:${B2_BUCKET:-timpbills-backups}/${DATE_PATH}/" --verbose 2>&1; then
                    echo "[$(date -u +%FT%TZ)] Uploaded to Backblaze B2"
                else
                    echo "[$(date -u +%FT%TZ)] WARNING: Backblaze B2 upload failed"
                fi
            else
                echo "[$(date -u +%FT%TZ)] WARNING: rclone not installed, skipping Backblaze B2 upload"
            fi
            ;;
    esac
fi

# ---- Summary ----
TOTAL_BACKUPS=$(ls -1 "${BACKUP_DIR}"/timpbills_backup_*.sql.gz 2>/dev/null | wc -l | tr -d ' ')
echo "[$(date -u +%FT%TZ)] Backup complete. ${TOTAL_BACKUPS} local backup(s) retained."
