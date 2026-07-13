#!/usr/bin/env bash
set -euo pipefail

# Wire this into the same cron/rclone/Gitea backup routine as your other backup scripts.
BACKUP_DIR="/backup/postgres-projects"
mkdir -p "$BACKUP_DIR"

docker exec postgres-projects pg_dump -U n8n_projects home_automation \
  | gzip > "$BACKUP_DIR/home_automation_$(date +%F).sql.gz"
