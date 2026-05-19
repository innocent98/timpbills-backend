#!/bin/bash
# ============================================================================
# Timpbills API Backup Status
# ============================================================================
# Quick health check: cron installed? recent local backups? recent cloud
# backups? last log entry? DB container running?
# ============================================================================

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-timpbills}"
DB_CONTAINER="${DB_CONTAINER:-${COMPOSE_PROJECT_NAME}_db}"
GDRIVE_FOLDER="${GDRIVE_FOLDER:-timpbills-backups}"
B2_BUCKET="${B2_BUCKET:-timpbills-backups}"
B2_RCLONE_REMOTE="${B2_RCLONE_REMOTE:-b2}"

echo ""
echo "============================================"
echo "  Timpbills API Backup Status"
echo "============================================"
echo ""

# Cron
echo -e "${BLUE}Automated backup schedule:${NC}"
if crontab -l 2>/dev/null | grep -qE "backup_(db|cron)\.sh"; then
    echo -e "${GREEN}  Active${NC}"
    crontab -l | grep -E "backup_(db|cron)\.sh"
else
    echo -e "${YELLOW}  No cron job found${NC}"
fi
echo ""

# Local backups
echo -e "${BLUE}Local backups:${NC}"
if ls backups/timpbills_backup_*.sql.gz 1>/dev/null 2>&1; then
    BACKUP_COUNT=$(ls -1 backups/timpbills_backup_*.sql.gz | wc -l | tr -d ' ')
    TOTAL_SIZE=$(du -sh backups/ | cut -f1)
    echo -e "${GREEN}  ${BACKUP_COUNT} backups (${TOTAL_SIZE})${NC}"
    echo ""
    echo "  Recent:"
    ls -lht backups/timpbills_backup_*.sql.gz | head -5 | awk '{print "    " $9 " (" $5 ", " $6 " " $7 " " $8 ")"}'
else
    echo -e "${YELLOW}  No local backups found${NC}"
fi
echo ""

# Google Drive
echo -e "${BLUE}Google Drive backups:${NC}"
if command -v rclone &>/dev/null; then
    if rclone listremotes | grep -q "gdrive:"; then
        GDRIVE_COUNT=$(rclone ls "gdrive:${GDRIVE_FOLDER}/" 2>/dev/null | wc -l | tr -d ' ')
        if [ "$GDRIVE_COUNT" -gt 0 ]; then
            echo -e "${GREEN}  ${GDRIVE_COUNT} backups in Google Drive${NC}"
            echo ""
            echo "  Recent:"
            CURRENT_DATE_PATH=$(date +"%Y/%m")
            rclone ls "gdrive:${GDRIVE_FOLDER}/${CURRENT_DATE_PATH}/" 2>/dev/null | tail -3 | awk '{printf "    %s (%.0f KB)\n", $2, $1/1024}'
        else
            echo -e "${YELLOW}  No backups in Google Drive yet${NC}"
        fi
    else
        echo -e "${YELLOW}  Google Drive not configured (run: rclone config)${NC}"
    fi
else
    echo -e "${YELLOW}  rclone not installed${NC}"
fi
echo ""

# Backblaze B2
echo -e "${BLUE}Backblaze B2 backups:${NC}"
if command -v rclone &>/dev/null; then
    if rclone listremotes | grep -q "^${B2_RCLONE_REMOTE}:"; then
        B2_COUNT=$(rclone ls "${B2_RCLONE_REMOTE}:${B2_BUCKET}/" 2>/dev/null | wc -l | tr -d ' ')
        if [ "$B2_COUNT" -gt 0 ]; then
            echo -e "${GREEN}  ${B2_COUNT} backups in Backblaze B2${NC}"
            echo ""
            echo "  Recent:"
            CURRENT_DATE_PATH=$(date +"%Y/%m")
            rclone ls "${B2_RCLONE_REMOTE}:${B2_BUCKET}/${CURRENT_DATE_PATH}/" 2>/dev/null | tail -3 | awk '{printf "    %s (%.0f KB)\n", $2, $1/1024}'
        else
            echo -e "${YELLOW}  No backups in Backblaze B2 yet${NC}"
        fi
    else
        echo -e "${YELLOW}  Backblaze B2 not configured (run: rclone config; choose 'Backblaze B2')${NC}"
    fi
else
    echo -e "${YELLOW}  rclone not installed${NC}"
fi
echo ""

# Last log
echo -e "${BLUE}Last backup log:${NC}"
if [ -f backups/backup.log ]; then
    echo "  Last 10 lines:"
    tail -10 backups/backup.log | sed 's/^/    /' || echo "    (no recent activity)"
else
    echo -e "${YELLOW}  No log file found (backups/backup.log)${NC}"
fi
echo ""

# DB container
echo -e "${BLUE}Database container:${NC}"
if docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    echo -e "${GREEN}  ${DB_CONTAINER} is running${NC}"
else
    echo -e "${YELLOW}  Expected container ${DB_CONTAINER} is not running${NC}"
fi
echo ""

echo "============================================"
echo ""
