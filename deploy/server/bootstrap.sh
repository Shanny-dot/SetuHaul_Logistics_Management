#!/bin/bash
# One-time server preparation on Amazon Linux 2023 (arm64). Safe to run again.
#   Docker + compose and buildx plugins (aarch64, checksum-verified), 2 GiB swap, folders, backup cron,
#   then the first deploy (load-env.sh + update.sh) if the secrets are already in SSM.
# Usage (root): bootstrap.sh [REPO_URL] [BRANCH]       (run automatically by the instance's first boot)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
REPO_URL="${1:-https://github.com/Shanny-dot/SetuHaul_Logistics_Management.git}"
BRANCH="${2:-whatsapp-api}"
H=/opt/setuhaul
COMPOSE_V=v2.29.7
BUILDX_V=v0.17.1
[ "$(uname -m)" = aarch64 ] || echo "WARNING: expected aarch64 (t4g), got $(uname -m)"

echo "== packages"
dnf install -y docker git cronie python3 >/dev/null
systemctl enable --now docker crond

echo "== docker compose $COMPOSE_V and buildx $BUILDX_V (aarch64)"
P=/usr/local/lib/docker/cli-plugins; mkdir -p "$P"; T="$(mktemp -d)"
if ! docker compose version 2>/dev/null | grep -q "${COMPOSE_V#v}"; then
  curl -fsSL -o "$T/docker-compose-linux-aarch64" "https://github.com/docker/compose/releases/download/$COMPOSE_V/docker-compose-linux-aarch64"
  curl -fsSL -o "$T/c.sha256" "https://github.com/docker/compose/releases/download/$COMPOSE_V/docker-compose-linux-aarch64.sha256"
  (cd "$T" && sha256sum -c c.sha256)
  install -m 755 "$T/docker-compose-linux-aarch64" "$P/docker-compose"
fi
if ! docker buildx version 2>/dev/null | grep -q "${BUILDX_V#v}"; then
  curl -fsSL -o "$T/buildx-$BUILDX_V.linux-arm64" "https://github.com/docker/buildx/releases/download/$BUILDX_V/buildx-$BUILDX_V.linux-arm64"
  curl -fsSL -o "$T/checksums.txt" "https://github.com/docker/buildx/releases/download/$BUILDX_V/checksums.txt"
  (cd "$T" && grep " \*\?buildx-$BUILDX_V.linux-arm64\$" checksums.txt | sha256sum -c -)
  install -m 755 "$T/buildx-$BUILDX_V.linux-arm64" "$P/docker-buildx"
fi
rm -rf "$T"
docker compose version; docker buildx version

echo "== 2 GiB swap (1 GiB RAM box)"
if ! swapon --show | grep -q /swapfile; then
  [ -f /swapfile ] || { fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; }
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -q -w vm.swappiness=10; echo 'vm.swappiness=10' > /etc/sysctl.d/90-setuhaul.conf

echo "== folders"
mkdir -p "$H"/{data/backups,redis,caddy/data,caddy/config,backups}
chown -R 10001:10001 "$H/data"; chmod 700 "$H/data"
chown -R 999:999 "$H/redis" 2>/dev/null || true
[ -d "$H/src/.git" ] || git clone --branch "$BRANCH" --depth 50 "$REPO_URL" "$H/src"
echo "$BRANCH" > "$H/BRANCH"
[ -f "$H/settings.env" ] || cat > "$H/settings.env" <<'S'
# Non-secret overrides for the generated .env (load-env.sh). Examples:
# DOMAIN=myname.duckdns.org          (default: <elastic-ip-with-dashes>.sslip.io)
# TWILIO_INTERACTIVE=true
S
chmod 644 "$H/settings.env"

echo "== nightly backup (02:30 IST = 21:00 UTC)"
install -m 644 "$H/src/deploy/server/setuhaul-backup.cron" /etc/cron.d/setuhaul-backup

echo "== first deploy"
if bash "$H/src/deploy/server/load-env.sh"; then
  bash "$H/src/deploy/server/update.sh" --no-pull || echo "first deploy failed: see above; fix and run: sudo /opt/setuhaul/src/deploy/server/update.sh --no-pull"
else
  echo "secrets not readable yet (run deploy/aws/put_secrets.sh), then: sudo /opt/setuhaul/src/deploy/server/load-env.sh && sudo /opt/setuhaul/src/deploy/server/update.sh --no-pull"
fi
echo "bootstrap finished"
