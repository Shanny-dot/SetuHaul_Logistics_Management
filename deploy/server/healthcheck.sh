#!/bin/bash
# Is the demo healthy? Exit 0 only if the app's /readyz is ok (db, redis, twilio credentials).
# Also reports (without failing): public https /healthz through Caddy, containers, memory, swap, OOM kills, disk.
# Usage (root): healthcheck.sh [--wait SECONDS]
. "$(dirname "$0")/lib.sh"
WAIT=0; [ "${1:-}" = "--wait" ] && WAIT="${2:-90}"
deadline=$(( $(date +%s) + WAIT ))
until curl -fsS -m 5 http://127.0.0.1:8000/readyz 2>/dev/null | grep -q '"ok":true'; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "FAIL  /readyz is not ok:"; curl -sS -m 5 http://127.0.0.1:8000/readyz || true; echo; exit 1
  fi
  sleep 3
done
echo "OK    /readyz (db, redis, twilio)"
DOMAIN="$(envval DOMAIN)"
if [ -n "$DOMAIN" ] && curl -fsS -m 10 "https://$DOMAIN/healthz" >/dev/null 2>&1; then echo "OK    https://$DOMAIN/healthz (certificate + Caddy)"
else echo "WARN  https://$DOMAIN/healthz not reachable yet (first certificate can take a minute; see README 'HTTPS')"; fi
compose ps --format '      {{.Service}}: {{.State}} {{.Health}}' 2>/dev/null || true
free -m | awk 'NR==2{printf "      memory used %s/%s MiB\n",$3,$2} NR==3{printf "      swap used   %s/%s MiB\n",$3,$2}'
if dmesg 2>/dev/null | grep -qi 'out of memory\|oom-kill'; then echo "WARN  the kernel has killed a process for memory (dmesg | grep -i oom)"; fi
df -h / | awk 'NR==2{print "      disk used " $3 " of " $2}'
LAST="$(ls -1t "$SETUHAUL_HOME"/backups/demo_world-*.db.gz 2>/dev/null | head -1 || true)"
if [ -z "$LAST" ]; then echo "WARN  no backup yet (sudo $SRC/deploy/server/backup.sh)"
elif [ $(( $(date +%s) - $(stat -c %Y "$LAST" 2>/dev/null || stat -f %m "$LAST") )) -gt 93600 ]; then echo "WARN  last backup is older than 26 h: $(basename "$LAST") (see /var/log/setuhaul-backup.log)"
else echo "OK    last backup $(basename "$LAST")"; fi
[ -s "$SETUHAUL_HOME/watchdog.failures" ] && [ "$(cat "$SETUHAUL_HOME/watchdog.failures")" != 0 ] && echo "WARN  watchdog: $(cat "$SETUHAUL_HOME/watchdog.failures") failed check(s) in a row"
true
