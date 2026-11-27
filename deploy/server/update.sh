#!/bin/bash
# Deploy the latest commit of the branch: git pull -> build image -> refresh .env -> restart -> /readyz.
# If /readyz does not become ok within 120 s, roll back to the previous commit and image automatically.
# Note: a restart resets the demo clock to SNAPSHOT_ANCHOR while the database keeps its bookings;
#       run reset-demo.sh afterwards for a clean demo.
# Usage (root): update.sh [--no-pull]
. "$(dirname "$0")/lib.sh"
need_root
cd "$SRC" || exit 1
BRANCH="$(cat "$SETUHAUL_HOME/BRANCH" 2>/dev/null || echo whatsapp-api)"
PREV_COMMIT="$(git rev-parse HEAD)"
PREV_TAG="$(cat "$SETUHAUL_HOME/APP_TAG" 2>/dev/null || true)"
[ "${1:-}" = "--no-pull" ] || git pull --ff-only origin "$BRANCH"
NEW_TAG="$(git rev-parse --short=12 HEAD)"

write_compose_env() { printf 'APP_TAG=%s\nDOMAIN=%s\nSETUHAUL_HOME=%s\n' "$1" "$(envval DOMAIN)" "$SETUHAUL_HOME" > "$SETUHAUL_HOME/compose.env"; }

echo "== build setuhaul-app:$NEW_TAG"
docker image inspect "setuhaul-app:$NEW_TAG" >/dev/null 2>&1 || docker build -f deploy/server/Dockerfile -t "setuhaul-app:$NEW_TAG" .
bash "$SRC/deploy/server/load-env.sh"

mkdir -p "$SETUHAUL_HOME/media" && chown 10001:10001 "$SETUHAUL_HOME/media"      # voice-note reply audio
echo "== start $NEW_TAG (previous: ${PREV_TAG:-none})"
write_compose_env "$NEW_TAG"
compose up -d --remove-orphans
if bash "$SRC/deploy/server/healthcheck.sh" --wait 120; then
  echo "$NEW_TAG" > "$SETUHAUL_HOME/APP_TAG"
  docker images setuhaul-app --format '{{.Tag}}' | grep -v -e "^$NEW_TAG\$" -e "^${PREV_TAG:-none}\$" | xargs -r -I{} docker rmi "setuhaul-app:{}" >/dev/null 2>&1 || true
  echo "DEPLOYED $NEW_TAG"
  exit 0
fi

echo "== ROLLBACK: $NEW_TAG failed /readyz"
compose logs --tail 40 app || true
if [ -z "$PREV_TAG" ]; then echo "no previous version to roll back to (first deploy): fix the error above"; exit 1; fi
git reset -q --hard "$PREV_COMMIT"
write_compose_env "$PREV_TAG"
compose up -d --remove-orphans
bash "$SRC/deploy/server/healthcheck.sh" --wait 120 && echo "ROLLED BACK to $PREV_TAG" || echo "rollback also unhealthy: see logs"
exit 1
