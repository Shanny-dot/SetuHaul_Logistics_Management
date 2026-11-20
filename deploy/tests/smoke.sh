#!/bin/bash
# Docker smoke test of the REAL deployment files: builds the linux/arm64 image (QEMU emulation on an Intel/AMD
# machine; native on Apple silicon or Graviton), starts deploy/server/docker-compose.yml with DOMAIN=localhost on
# host ports 8080/8443, then runs deploy/tests/checks.py (healthz, readyz, private paths 404, signed Twilio webhook,
# bad signature 403) and a backup + restore check. Cleans up afterwards. Needs Docker with buildx.
# Usage: deploy/tests/smoke.sh            (from anywhere; takes a few minutes the first time)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
command -v docker >/dev/null || { echo "Docker is not installed: install Docker Desktop, or run deploy/tests/local_smoke.sh"; exit 2; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx missing"; exit 2; }
if [ "$(uname -m)" != arm64 ] && [ "$(uname -m)" != aarch64 ]; then
  docker buildx ls | grep -q 'linux/arm64' || {
    echo "This machine cannot build linux/arm64 yet. Enable QEMU once:  docker run --privileged --rm tonistiigi/binfmt --install arm64"; exit 2; }
fi
H="$ROOT/deploy/tests/.smoke-$$"; TOKEN="smoke-$(date +%s)-token"; PHONE="+919800000013"
mkdir -p "$H"/{data/backups,redis,caddy/data,caddy/config,backups}
chmod 777 "$H/data" "$H/data/backups"            # container user uid 10001 must write here
cat > "$H/.env" <<ENV
CHANNEL=console
TWILIO_AUTH_TOKEN=$TOKEN
TWILIO_WEBHOOK_BASE_URL=https://localhost:8443
CLOCK_MODE=snapshot
SNAPSHOT_ANCHOR=2026-08-04T09:19:00+05:30
SETUHAUL_DEV=1
SEED_PHONES=DRV013=$PHONE
ENV
printf 'APP_TAG=smoke\nDOMAIN=localhost\nSETUHAUL_HOME=%s\nHTTP_PORT=8080\nHTTPS_PORT=8443\nAPP_LOCAL_PORT=18000\n' "$H" > "$H/compose.env"
dc() { docker compose -p setuhaul-smoke --env-file "$H/compose.env" -f "$ROOT/deploy/server/docker-compose.yml" "$@"; }
cleanup() { dc down --remove-orphans >/dev/null 2>&1 || true; rm -rf "$H"; }
trap cleanup EXIT

echo "== build linux/arm64 image"
docker buildx build --platform linux/arm64 --load -f "$ROOT/deploy/server/Dockerfile" -t setuhaul-app:smoke "$ROOT"
docker image inspect setuhaul-app:smoke --format 'image architecture: {{.Architecture}}'

echo "== start caddy + app + redis"
dc up -d
for _ in $(seq 1 90); do curl -fsS http://127.0.0.1:18000/readyz 2>/dev/null | grep -q '"ok":true' && break; sleep 2; done
dc ps

RC=0
python3 "$ROOT/deploy/tests/checks.py" --public https://localhost:8443 --local http://127.0.0.1:18000 --token "$TOKEN" --phone "$PHONE" || RC=1

echo "== memory per container (limits: caddy 128m, app 512m, redis 96m)"
# shellcheck disable=SC2046  # one argument per container id
docker stats --no-stream --format '  {{.Name}}: {{.MemUsage}}' $(dc ps -q) || true

echo "== backup + restore check (the same files the server uses)"
dc exec -T app python -I - /data/backups/smoke.db < "$ROOT/deploy/server/sqlite_backup.py" || RC=1
gzip -9 "$H/data/backups/smoke.db" && bash "$ROOT/deploy/server/restore-check.sh" "$H/data/backups/smoke.db.gz" || RC=1

[ "$RC" = 0 ] && echo "DOCKER SMOKE: PASS" || { echo "DOCKER SMOKE: FAIL"; dc logs --tail 40; }
exit "$RC"
