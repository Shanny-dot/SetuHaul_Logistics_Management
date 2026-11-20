#!/bin/bash
# Nightly: consistent SQLite copy -> integrity check -> gzip -> s3://<bucket>/backups/ (30-day expiry).
# Keeps the last 3 copies locally too. Backups protect DATA, not uptime (see README: restore).
# Usage (root): backup.sh [--local-only]
. "$(dirname "$0")/lib.sh"
need_root
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="demo_world-$STAMP.db"
compose exec -T app python -I - "/data/backups/$NAME" < "$SRC/deploy/server/sqlite_backup.py"
mv "$SETUHAUL_HOME/data/backups/$NAME" "$SETUHAUL_HOME/backups/$NAME"
gzip -9 "$SETUHAUL_HOME/backups/$NAME"
if [ "${1:-}" != "--local-only" ]; then
  BUCKET="$(envval BACKUP_BUCKET)"
  [ -n "$BUCKET" ] || { echo "BACKUP_BUCKET not set (run deploy/aws/create_bucket.sh, then load-env.sh)"; exit 1; }
  aws s3 cp --region "$REGION" --only-show-errors --sse AES256 "$SETUHAUL_HOME/backups/$NAME.gz" "s3://$BUCKET/backups/$NAME.gz"
  echo "uploaded s3://$BUCKET/backups/$NAME.gz"
fi
ls -1t "$SETUHAUL_HOME"/backups/demo_world-*.db.gz | tail -n +4 | xargs -r rm -f
