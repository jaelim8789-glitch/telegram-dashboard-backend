#!/bin/sh
set -e

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/db_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting PostgreSQL backup..."

pg_dump | gzip > "$BACKUP_FILE"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup: ${BACKUP_FILE} ($(du -h "$BACKUP_FILE" | cut -f1))"

# Clean old backups
find "$BACKUP_DIR" -name "db_*.sql.gz" -type f -mtime "+${RETENTION_DAYS}" -delete

ln -sf "$BACKUP_FILE" "${BACKUP_DIR}/latest.sql.gz"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup complete."
