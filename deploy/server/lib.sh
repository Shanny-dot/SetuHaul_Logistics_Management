#!/bin/bash
# shellcheck disable=SC2034  # used by the scripts that source this file
# Shared settings for the server scripts. Sourced, not run.
set -euo pipefail
SETUHAUL_HOME="${SETUHAUL_HOME:-/opt/setuhaul}"
SRC="$SETUHAUL_HOME/src"
REGION="ap-south-1"
COMPOSE_FILE="$SRC/deploy/server/docker-compose.yml"
compose() { docker compose --env-file "$SETUHAUL_HOME/compose.env" -f "$COMPOSE_FILE" "$@"; }
envval() {   # read one non-secret value from the generated .env without printing the file
  grep -E "^$1=" "$SETUHAUL_HOME/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true
}
need_root() { [ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo $0 $*" >&2; exit 1; }; }
