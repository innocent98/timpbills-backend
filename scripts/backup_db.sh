#!/bin/bash
# ============================================================================
# Timpbills API Database Backup - Developer / ad-hoc use
# ============================================================================
# Creates a gzipped pg_dump of the database and optionally uploads to cloud
# storage (S3, Google Drive, Dropbox). Designed for local/dev machines.
# For production cron use, see backup_cron.sh (hardened for unattended runs).
# ============================================================================

set -euo pipefail  # Exit on error; fail on unset vars

# ---- Load .env ------------------------------------------------------------
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# ---- Configuration --------------------------------------------------------
BACKUP_DIR="./backups"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-timpbills}"
DB_CONTAINER="${DB_CONTAINER:-${COMPOSE_PROJECT_NAME}_db}"
DB_USER="${POSTGRES_USER:-}"
DB_NAME="${POSTGRES_DB:-}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
DATE_PATH=$(date +"%Y/%m")
BACKUP_FILE="timpbills_backup_${TIMESTAMP}.sql"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILE}"

# Retention (days)
RETENTION_DAYS="${RETENTION_DAYS:-30}"

# Cloud (optional)
ENABLE_CLOUD_BACKUP=${ENABLE_CLOUD_BACKUP:-false}
CLOUD_PROVIDER=${CLOUD_PROVIDER:-""}

# ---- Colors ---------------------------------------------------------------
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[info] $1${NC}"; }
log_success() { echo -e "${GREEN}[ok]   $1${NC}"; }
log_warning() { echo -e "${YELLOW}[warn] $1${NC}"; }
log_error()   { echo -e "${RED}[err]  $1${NC}"; }

# ---- Main -----------------------------------------------------------------
echo ""
echo "============================================"
echo "  Timpbills API Database Backup"
echo "============================================"
log_info "Timestamp: ${TIMESTAMP}"
log_info "Database:  ${DB_NAME:-<unset>}"
log_info "Container: ${DB_CONTAINER}"
echo ""

mkdir -p "${BACKUP_DIR}"

if [ -z "${DB_USER}" ] || [ -z "${DB_NAME}" ]; then
    log_error "POSTGRES_USER and POSTGRES_DB must be set in .env"
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    log_error "Database container '${DB_CONTAINER}' is not running."
    exit 1
fi

log_info "Creating backup: ${BACKUP_FILE}"
if docker exec "${DB_CONTAINER}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" > "${BACKUP_PATH}"; then
    log_success "pg_dump completed"
else
    log_error "pg_dump failed"
    rm -f "${BACKUP_PATH}"
    exit 1
fi

log_info "Compressing..."
gzip "${BACKUP_PATH}"
BACKUP_PATH="${BACKUP_PATH}.gz"
BACKUP_SIZE=$(du -h "${BACKUP_PATH}" | cut -f1)
log_success "Compressed: ${BACKUP_FILE}.gz (${BACKUP_SIZE})"

# ---- Cloud upload (optional) ---------------------------------------------
if [ "$ENABLE_CLOUD_BACKUP" = "true" ]; then
    echo ""
    log_info "Cloud backup enabled: ${CLOUD_PROVIDER}"

    case "$CLOUD_PROVIDER" in
        s3)
            if [ -z "${AWS_S3_BUCKET:-}" ]; then
                log_error "AWS_S3_BUCKET not set"
            elif command -v aws &>/dev/null; then
                aws s3 cp "${BACKUP_PATH}" "s3://${AWS_S3_BUCKET}/backups/${DATE_PATH}/${BACKUP_FILE}.gz" \
                    && log_success "Uploaded to S3" \
                    || log_warning "S3 upload failed"
            else
                log_warning "aws CLI not installed; skipping S3 upload"
            fi
            ;;
        gdrive)
            if [ -z "${GDRIVE_FOLDER:-}" ]; then
                log_error "GDRIVE_FOLDER not set"
            elif command -v rclone &>/dev/null; then
                rclone mkdir "gdrive:${GDRIVE_FOLDER}/${DATE_PATH}" 2>/dev/null || true
                rclone copy "${BACKUP_PATH}" "gdrive:${GDRIVE_FOLDER}/${DATE_PATH}/" --progress \
                    && log_success "Uploaded to Google Drive" \
                    || log_warning "Google Drive upload failed"
            else
                log_warning "rclone not installed; skipping Google Drive upload"
            fi
            ;;
        dropbox)
            if [ -z "${DROPBOX_FOLDER:-}" ]; then
                log_error "DROPBOX_FOLDER not set"
            elif command -v rclone &>/dev/null; then
                rclone mkdir "dropbox:${DROPBOX_FOLDER}/${DATE_PATH}" 2>/dev/null || true
                rclone copy "${BACKUP_PATH}" "dropbox:${DROPBOX_FOLDER}/${DATE_PATH}/" --progress \
                    && log_success "Uploaded to Dropbox" \
                    || log_warning "Dropbox upload failed"
            else
                log_warning "rclone not installed; skipping Dropbox upload"
            fi
            ;;
        b2)
            # Backblaze B2 via rclone. Configure once with:  rclone config  (choose "Backblaze B2")
            # Expected env: B2_BUCKET. Uses remote name "b2" by default; override with B2_RCLONE_REMOTE.
            if [ -z "${B2_BUCKET:-}" ]; then
                log_error "B2_BUCKET not set"
            elif command -v rclone &>/dev/null; then
                B2_REMOTE="${B2_RCLONE_REMOTE:-b2}"
                rclone mkdir "${B2_REMOTE}:${B2_BUCKET}/${DATE_PATH}" 2>/dev/null || true
                rclone copy "${BACKUP_PATH}" "${B2_REMOTE}:${B2_BUCKET}/${DATE_PATH}/" --progress \
                    && log_success "Uploaded to Backblaze B2" \
                    || log_warning "Backblaze B2 upload failed"
            else
                log_warning "rclone not installed; skipping Backblaze B2 upload"
            fi
            ;;
        *)
            log_warning "Unknown cloud provider: ${CLOUD_PROVIDER} (supported: s3, gdrive, dropbox, b2)"
            ;;
    esac
fi

# ---- Cleanup old backups -------------------------------------------------
echo ""
log_info "Cleaning backups older than ${RETENTION_DAYS} days..."
DELETED_COUNT=$(find "${BACKUP_DIR}" -name "timpbills_backup_*.sql.gz" -mtime +${RETENTION_DAYS} -delete -print | wc -l | tr -d ' ')

if [ "$DELETED_COUNT" -gt 0 ]; then
    log_success "Deleted ${DELETED_COUNT} old backup(s)"
else
    log_info "No old backups to delete"
fi

BACKUP_COUNT=$(ls -1 "${BACKUP_DIR}"/timpbills_backup_*.sql.gz 2>/dev/null | wc -l | tr -d ' ')
log_info "Total local backups: ${BACKUP_COUNT}"

# ---- Summary --------------------------------------------------------------
echo ""
echo "============================================"
log_success "Backup complete"
echo "  Local:     ${BACKUP_PATH}"
echo "  Size:      ${BACKUP_SIZE}"
echo "  Retention: ${RETENTION_DAYS} days"
[ "$ENABLE_CLOUD_BACKUP" = "true" ] && echo "  Cloud:     ${CLOUD_PROVIDER}"
echo "============================================"
echo ""
