#!/bin/bash
# Live WhatsApp demo: (re)start the app for one scenario, on your phone.
#
#   scripts/live_start.sh C03            # or C06, C07, C12, C14
#   scripts/live_start.sh C06 +91XXXXXXXXXX   # phone given here instead of DEMO_PHONE in .env
#
# What it does, in order:
#   1. stops whatever is listening on port 8000 (the previous run)
#   2. maps your phone to the scenario's driver on a FRESH copy of the database (seed_demo_phones.py)
#   3. loads ../.env (never printed), checks ngrok's current address matches TWILIO_WEBHOOK_BASE_URL
#   4. starts the app, waits until /readyz is ok, moves the demo clock to the scenario's time
#   5. shows the app log; Ctrl+C stops the app
#
# Needs in .env: CHANNEL=twilio, TWILIO_*, TWILIO_WEBHOOK_BASE_URL=<ngrok https URL>, CLOCK_MODE=snapshot,
# SNAPSHOT_ANCHOR=2026-08-04T09:15:00+05:30, SETUHAUL_DEV=1, and DEMO_PHONE=+91XXXXXXXXXX (your WhatsApp number).
# Keep ngrok running in its own terminal (./ngrok http 8000).

set -u
cd "$(dirname "$0")/.." || exit 1                      # the prototype folder
ENV_FILE="${ENV_FILE:-../.env}"
PORT="${PORT:-8000}"
DB="${SETUHAUL_DB_OVERRIDE:-out/demo_world.db}"

SC="$(echo "${1:-}" | tr '[:lower:]' '[:upper:]')"
case "$SC" in
  C03) DRV=DRV013; AT=09:19; SAY="I am late by one hour.   then reply 1" ;;
  C06) DRV=DRV004; AT=09:31; SAY="I will be late by 45 minutes.   then शाम वाला   then 1" ;;
  C07) DRV=DRV006; AT=09:34; SAY="Traffic after Shahpura. Reaching around 11:20. Any slot after 12?   (then the same text again)" ;;
  C12) DRV=DRV011; AT=10:05; SAY="bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya   then slot kab milega?" ;;
  C14) DRV=DRV015; AT=16:24; SAY="Evening traffic. ETA 6:30. Can the reefer unload tonight?" ;;
  *) echo "usage: scripts/live_start.sh C03|C06|C07|C12|C14 [phone]"; exit 2 ;;
esac

[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE found (run from anywhere; the script looks next to the prototype folder)."; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a                           # values are never echoed
PHONE="${2:-${DEMO_PHONE:-}}"
[ -n "$PHONE" ] || { echo "Add DEMO_PHONE=+91XXXXXXXXXX (your WhatsApp number) to .env, or pass it as the 2nd argument."; exit 1; }
MASKED="${PHONE:0:5}******${PHONE: -2}"

echo "== 1/5 stopping anything on port $PORT"
PIDS="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null)"
if [ -n "$PIDS" ]; then kill $PIDS 2>/dev/null; sleep 1; fi
PIDS="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null)"; [ -n "$PIDS" ] && kill -9 $PIDS 2>/dev/null

echo "== 2/5 mapping $MASKED to $DRV for $SC (fresh copy of the 4 Aug world)"
SEED_PHONES="$DRV=$PHONE" .venv/bin/python -I scripts/seed_demo_phones.py --out "$DB" >/dev/null || { echo "phone mapping failed"; exit 1; }

echo "== 3/5 checking ngrok"
NGROK="$(curl -s -m 3 http://localhost:4040/api/tunnels | /usr/bin/python3 -c 'import sys,json
try:
    t=[x["public_url"] for x in json.load(sys.stdin)["tunnels"] if x["public_url"].startswith("https")]
    print(t[0] if t else "")
except Exception:
    print("")' 2>/dev/null)"
if [ -z "$NGROK" ]; then
  echo "   WARNING: ngrok is not running. Start it in another terminal:  cd ~/Downloads && ./ngrok http $PORT"
elif [ "${NGROK%/}" != "${TWILIO_WEBHOOK_BASE_URL%/}" ]; then
  echo "   WARNING: ngrok's address changed to $NGROK"
  echo "   Put it in .env as TWILIO_WEBHOOK_BASE_URL and in Twilio's Sandbox settings (+ /webhook/twilio), then run this again."
else
  echo "   ngrok OK: $NGROK"
fi
[ "${TWILIO_WHATSAPP_FROM:-}" = "whatsapp:+14155238886" ] || echo "   WARNING: TWILIO_WHATSAPP_FROM should be whatsapp:+14155238886 (the sandbox number)"
[ "${CHANNEL:-}" = "twilio" ] || echo "   WARNING: CHANNEL is not twilio: replies will not go to WhatsApp"

echo "== 4/5 starting the app"
SETUHAUL_DB="$DB" .venv/bin/uvicorn app:app --port "$PORT" --log-level warning &
APP=$!
trap 'echo; echo "stopping the app"; kill $APP 2>/dev/null; exit 0' INT TERM
for _ in $(seq 1 40); do
  curl -s -m 2 "http://localhost:$PORT/readyz" 2>/dev/null | grep -q '"ok":true' && break
  kill -0 $APP 2>/dev/null || { echo "the app stopped while starting (see the error above)"; exit 1; }
  sleep 0.5
done
curl -s -m 2 "http://localhost:$PORT/readyz" | grep -q '"ok":true' || { echo "the app is not ready: curl localhost:$PORT/readyz"; kill $APP; exit 1; }

echo "== 5/5 demo clock -> $AT"
curl -s -m 5 "http://localhost:$PORT/dev/clock" -H 'content-type: application/json' -d "{\"as_of\":\"2026-08-04T$AT:00+05:30\"}" >/dev/null

cat <<EOF

  READY: $SC  (your phone $MASKED is $DRV, clock $AT)
  On WhatsApp, send to +1 415 523 8886:
     $SAY

  App log follows. Ctrl+C stops the app. Next scenario: scripts/live_start.sh <C06|C07|C12|C14>
EOF
wait $APP
