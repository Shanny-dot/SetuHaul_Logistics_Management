#!/bin/bash
# Smoke test WITHOUT Docker: the real Caddyfile (Caddy binary) in front of the app run exactly like the
# container's entrypoint (one uvicorn worker, --proxy-headers), on localhost with a local TLS certificate.
# Runs the checks twice: with TWILIO_WEBHOOK_BASE_URL set (production setting) and without it (the app must then
# rebuild the public URL from Caddy's X-Forwarded-Proto/Host). Then backs up the DB and runs the restore check.
# Needs: caddy (CADDY=/path/to/caddy) and the prototype venv (PYTHON=prototype/.venv/bin/python).
# Usage: deploy/tests/local_smoke.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CADDY="${CADDY:-caddy}"; PY="${PYTHON:-$ROOT/prototype/.venv/bin/python}"
command -v "$CADDY" >/dev/null || { echo "caddy not found: set CADDY=/path/to/caddy"; exit 2; }
T="$(mktemp -d)"; TOKEN="smoke-$(date +%s)-token"; PHONE="+919800000013"
APP_PORT=18000; HTTPS_PORT=18443; HTTP_PORT=18080
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; rm -rf "$T"; }
trap cleanup EXIT
RC=0

echo "== demo DB copy with a test handset (the supplied snapshot is not touched)"
(cd "$ROOT/prototype" && SEED_PHONES="DRV013=$PHONE" "$PY" -I scripts/seed_demo_phones.py --out "$T/demo_world.db" >/dev/null)

start_app() {   # $1 = base URL or empty
  for _ in $(seq 1 40); do lsof -ti tcp:$APP_PORT -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.25; done
  lsof -ti tcp:$APP_PORT -sTCP:LISTEN >/dev/null 2>&1 && { echo "port $APP_PORT is still in use"; exit 1; }
  (cd "$ROOT/prototype" && exec env -i PATH="$PATH" HOME="$T" CHANNEL=console TWILIO_AUTH_TOKEN="$TOKEN" \
     TWILIO_WEBHOOK_BASE_URL="$1" CLOCK_MODE=snapshot SNAPSHOT_ANCHOR=2026-08-04T09:19:00+05:30 SETUHAUL_DEV=1 \
     SETUHAUL_DB="$T/demo_world.db" \
     "$(dirname "$PY")/uvicorn" app:app --host 127.0.0.1 --port $APP_PORT --workers 1 --proxy-headers \
       --forwarded-allow-ips '*' --log-level warning > "$T/app.log" 2>&1) &
  APP_PID=$!; PIDS+=("$APP_PID")
  for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1 && return 0; sleep 0.5; done
  echo "app did not start:"; tail -20 "$T/app.log"; exit 1
}

stop_app() { kill "$APP_PID" 2>/dev/null || true; wait "$APP_PID" 2>/dev/null || true; }
check_base() {   # prove which configuration the running app has
  grep -q "\"webhook_base_url\":\"$1\"" <(curl -fsS "http://127.0.0.1:$APP_PORT/readyz") || { echo "FAIL  app is not running with base URL '$1'"; RC=1; }
}

echo "== Caddy with deploy/server/Caddyfile (DOMAIN=localhost, local CA)"
mkdir -p "$T/mroot/media"; MEDIA_NAME="$(openssl rand -hex 16).ogg"; printf 'OggS-test' > "$T/mroot/media/$MEDIA_NAME"
XDG_DATA_HOME="$T/caddy-data" XDG_CONFIG_HOME="$T/caddy-config" DOMAIN=localhost UPSTREAM="127.0.0.1:$APP_PORT" SITE_ROOT="$ROOT" MEDIA_ROOT="$T/mroot" \
  HTTP_PORT=$HTTP_PORT HTTPS_PORT=$HTTPS_PORT \
  "$CADDY" run --config "$ROOT/deploy/server/Caddyfile" --adapter caddyfile > "$T/caddy.log" 2>&1 &
PIDS+=("$!")
for _ in $(seq 1 60); do curl -fsSk "https://localhost:$HTTPS_PORT/healthz" >/dev/null 2>&1 && break; sleep 0.5; done

echo; echo "== run 1: TWILIO_WEBHOOK_BASE_URL=https://localhost:$HTTPS_PORT (as on the server)"
start_app "https://localhost:$HTTPS_PORT"; check_base "https://localhost:$HTTPS_PORT"
"$PY" -I "$ROOT/deploy/tests/checks.py" --public "https://localhost:$HTTPS_PORT" --local "http://127.0.0.1:$APP_PORT" --token "$TOKEN" --phone "$PHONE" || RC=1
stop_app

echo; echo "== run 2: no TWILIO_WEBHOOK_BASE_URL (URL rebuilt from Caddy's X-Forwarded-Proto/Host)"
start_app ""; check_base ""
"$PY" -I "$ROOT/deploy/tests/checks.py" --public "https://localhost:$HTTPS_PORT" --local "http://127.0.0.1:$APP_PORT" --token "$TOKEN" --phone "$PHONE" || RC=1
stop_app

echo; echo "== voice-note audio served by Caddy"
CT="$(curl -sk -o /dev/null -w '%{http_code} %{content_type}' "https://localhost:$HTTPS_PORT/media/$MEDIA_NAME")"
[ "$CT" = "200 audio/ogg" ] && echo "PASS  /media/<32 hex>.ogg -> 200 audio/ogg" || { echo "FAIL  media served as: $CT"; RC=1; }
for p in /media/ /media/x.ogg /media/../.env; do c="$(curl -sk -o /dev/null -w '%{http_code}' "https://localhost:$HTTPS_PORT$p")"
  [ "$c" = 404 ] && echo "PASS  $p -> 404" || { echo "FAIL  $p -> $c"; RC=1; }; done

echo; echo "== backup (sqlite_backup.py) -> gzip -> restore-check.sh"
SETUHAUL_DB="$T/demo_world.db" "$PY" -I - "$T/backup.db" < "$ROOT/deploy/server/sqlite_backup.py" || RC=1
gzip -9 "$T/backup.db"
bash "$ROOT/deploy/server/restore-check.sh" "$T/backup.db.gz" || RC=1

echo; [ "$RC" = 0 ] && echo "LOCAL SMOKE: PASS" || { echo "LOCAL SMOKE: FAIL"; echo "--- caddy log"; tail -15 "$T/caddy.log"; }
exit "$RC"
