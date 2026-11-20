#!/bin/bash
# Every 5 minutes (cron): if /readyz has failed 3 checks in a row, restart the app container and log it.
# Docker already restarts a crashed container; this catches a hung one. Log: /var/log/setuhaul-watchdog.log
. "$(dirname "$0")/lib.sh"
need_root
STATE="$SETUHAUL_HOME/watchdog.failures"
if curl -fsS -m 10 http://127.0.0.1:8000/readyz 2>/dev/null | grep -q '"ok":true'; then
  echo 0 > "$STATE"; exit 0
fi
n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE"
echo "$(date -u +%FT%TZ) readyz failed ($n in a row)"
if [ "$n" -ge 3 ]; then
  echo "$(date -u +%FT%TZ) restarting app"
  compose restart app && echo 0 > "$STATE"
fi
