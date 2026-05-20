#!/bin/bash
# ============================================================================
# Timpbills API Database Restore
# ============================================================================
# Restores a gzipped pg_dump into the database. Reads DB config from .env
# (POSTGRES_USER, POSTGRES_DB, COMPOSE_PROJECT_NAME). Creates a safety
# backup of the current state before overwriting.
#
# Usage:
#   ./scripts/restore_db.sh ./backups/timpbills_backup_20260422_020000.sql.gz
# ============================================================================

set -e

# ---- Load .env ------------------------------------------------------------
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# ---- Configuration --------------------------------------------------------
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-timpbills}"
DB_CONTAINER="${DB_CONTAINER:-${COMPOSE_PROJECT_NAME}_db}"
API_CONTAINER="${API_CONTAINER:-${COMPOSE_PROJECT_NAME}_api}"
WORKER_CONTAINER="${WORKER_CONTAINER:-${COMPOSE_PROJECT_NAME}_worker}"
DB_USER="${POSTGRES_USER:-}"
DB_NAME="${POSTGRES_DB:-}"

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

# ---- Validation -----------------------------------------------------------
if [ -z "${DB_USER}" ] || [ -z "${DB_NAME}" ]; then
    log_error "POSTGRES_USER and POSTGRES_DB must be set in .env"
    exit 1
fi

if [ -z "${1:-}" ]; then
    echo ""
    log_error "Usage: ./scripts/restore_db.sh <backup_file.sql.gz>"
    echo ""
    log_info "Available backups:"
    if ls ./backups/timpbills_backup_*.sql.gz 1>/dev/null 2>&1; then
        ls -lh ./backups/timpbills_backup_*.sql.gz | awk '{print "  " $9 " (" $5 ")"}'
    else
        echo "  No backups found in ./backups/"
    fi
    echo ""
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
    log_error "Backup file not found: $BACKUP_FILE"
    exit 1
fi

# ---- Confirmation ---------------------------------------------------------
echo ""
echo "============================================"
echo "  Timpbills API Database Restore"
echo "============================================"
echo ""
log_warning "This will OVERWRITE the current database."
log_warning "All existing data will be PERMANENTLY DELETED."
echo ""
log_info "Database:    ${DB_NAME}"
log_info "Container:   ${DB_CONTAINER}"
log_info "Backup file: ${BACKUP_FILE}"
echo ""
read -rp "Type 'yes' to proceed: " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    log_error "Restore cancelled"
    exit 0
fi

# ---- Pre-restore safety backup -------------------------------------------
echo ""
log_info "Creating safety backup of current database..."
mkdir -p ./backups
SAFETY_BACKUP="./backups/pre_restore_backup_$(date +%Y%m%d_%H%M%S).sql.gz"
docker exec -t "${DB_CONTAINER}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" | gzip > "${SAFETY_BACKUP}"
log_success "Safety backup created: ${SAFETY_BACKUP}"

# ---- Restore --------------------------------------------------------------
echo ""
log_info "Starting restore..."

if ! docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    log_error "Container ${DB_CONTAINER} is not running"
    exit 1
fi

TEMP_FILE="/tmp/timpbills_restore_temp_$$.sql"
if [[ "$BACKUP_FILE" == *.gz ]]; then
    log_info "Decompressing backup..."
    gunzip -c "$BACKUP_FILE" > "$TEMP_FILE"
else
    TEMP_FILE="$BACKUP_FILE"
fi

log_info "Terminating existing database connections..."
docker exec -t "${DB_CONTAINER}" psql -U "${DB_USER}" -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB_NAME}' AND pid <> pg_backend_pid();" \
    >/dev/null 2>&1 || true

log_info "Recreating database..."
docker exec -t "${DB_CONTAINER}" psql -U "${DB_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec -t "${DB_CONTAINER}" psql -U "${DB_USER}" -d postgres -c "CREATE DATABASE ${DB_NAME};"

log_info "Restoring data..."
docker exec -i "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" < "$TEMP_FILE" >/dev/null 2>&1

if [[ "$BACKUP_FILE" == *.gz ]]; then
    rm -f "$TEMP_FILE"
fi

log_success "Database restored"

# ---- Post-restore service restarts ---------------------------------------
echo ""
log_info "Restarting application containers..."
if docker ps -a --format '{{.Names}}' | grep -q "^${API_CONTAINER}$"; then
    docker restart "${API_CONTAINER}" >/dev/null 2>&1 \
        && log_success "Restarted ${API_CONTAINER}" \
        || log_warning "Failed to restart ${API_CONTAINER}"
fi
if docker ps -a --format '{{.Names}}' | grep -q "^${WORKER_CONTAINER}$"; then
    docker restart "${WORKER_CONTAINER}" >/dev/null 2>&1 \
        && log_success "Restarted ${WORKER_CONTAINER}" \
        || log_warning "Failed to restart ${WORKER_CONTAINER}"
fi

# ---- Summary --------------------------------------------------------------
echo ""
echo "============================================"
log_success "Restore complete"
echo "  Database:        ${DB_NAME}"
echo "  Restored from:   ${BACKUP_FILE}"
echo "  Safety backup:   ${SAFETY_BACKUP}"
echo "============================================"
echo ""
log_warning "Verify the application is working correctly before resuming traffic."
echo ""
