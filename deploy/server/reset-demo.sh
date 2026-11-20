#!/bin/bash
# Fresh 4 Aug world for a demo: back up the current DB locally, re-create it from the snapshot COPY with your
# handsets mapped, restart the app, and set the demo clock.
#   reset-demo.sh                 map DEMO_PHONES (several handsets), clock at SNAPSHOT_ANCHOR
#   reset-demo.sh C06             one phone: map DEMO_PHONE to C06's driver and set the clock to 09:31
# Scenarios: C03 DRV013 09:19 | C06 DRV004 09:31 | C07 DRV006 09:34 | C12 DRV011 10:05 | C14 DRV015 16:24
. "$(dirname "$0")/lib.sh"
need_root
SC="$(echo "${1:-}" | tr '[:lower:]' '[:upper:]')"
case "$SC" in
  "")  DRV=""; AT="" ;;
  C03) DRV=DRV013; AT=09:19 ;; C06) DRV=DRV004; AT=09:31 ;; C07) DRV=DRV006; AT=09:34 ;;
  C12) DRV=DRV011; AT=10:05 ;; C14) DRV=DRV015; AT=16:24 ;;
  *) echo "usage: reset-demo.sh [C03|C06|C07|C12|C14]"; exit 2 ;;
esac
if [ -n "$DRV" ]; then
  P="$(envval DEMO_PHONE)"; [ -n "$P" ] || { echo "DEMO_PHONE not set (deploy/aws/put_secrets.sh)"; exit 1; }
  export SEED_PHONES="$DRV=$P"
else
  SEED_PHONES="$(envval DEMO_PHONES)"; export SEED_PHONES
fi
bash "$SRC/deploy/server/backup.sh" --local-only || echo "(no backup taken: app not running?)"
compose stop app
rm -f "$SETUHAUL_HOME/data/demo_world.db" "$SETUHAUL_HOME/data/demo_world.db-wal" "$SETUHAUL_HOME/data/demo_world.db-shm"
if [ -n "$SEED_PHONES" ]; then
  compose run --rm --no-deps -e SEED_PHONES --entrypoint python app -I scripts/seed_demo_phones.py --out /data/demo_world.db > /dev/null
fi
compose up -d app
bash "$SRC/deploy/server/healthcheck.sh" --wait 90 >/dev/null
[ -n "$AT" ] && curl -fsS -m 5 http://127.0.0.1:8000/dev/clock -H 'content-type: application/json' -d "{\"as_of\":\"2026-08-04T$AT:00+05:30\"}" >/dev/null
echo "READY ${SC:-demo}: fresh world${AT:+, clock $AT}. Handsets mapped: $([ -n "$SEED_PHONES" ] && echo yes || echo none)."
