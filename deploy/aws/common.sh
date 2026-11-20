#!/bin/bash
# shellcheck disable=SC2034  # the names below are used by the scripts that source this file
# Shared helpers for deploy/aws/*.sh. Sourced, not run.
# Every script: shows what it will create and the monthly cost, asks "yes", supports --dry-run,
# and is safe to run again (it reuses what already exists).
set -euo pipefail

REGION="ap-south-1"                       # Mumbai only (approved plan)
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
export AWS_PAGER=""
PROJECT="setuhaul"
ROLE_NAME="setuhaul-ec2-role"             # role and instance profile share this name
SG_NAME="setuhaul-web"
INSTANCE_NAME="setuhaul"
EIP_NAME="setuhaul-eip"
ALARM_NAME="setuhaul-auto-recover"
BUDGET_NAME="setuhaul-monthly"
PARAM_PATH="/setuhaul"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# The plan line printed before anything is created or changed.
cost() {
  printf '\n  WILL CREATE/CHANGE: %s\n  ESTIMATED COST:     %s\n' "$1" "$2"
}

# Ask before acting. Skipped in --dry-run (nothing is done) or with YES=1 (non-interactive re-runs).
confirm() {
  if [ "$DRY_RUN" = 1 ]; then say "  [dry-run] would ask for confirmation here"; return 0; fi
  if [ "${YES:-0}" = 1 ]; then return 0; fi
  local answer
  read -r -p "  Type yes to continue: " answer
  [ "$answer" = "yes" ] || { say "  Cancelled. Nothing was changed."; exit 1; }
}

# Run an AWS-modifying command, or only show it in --dry-run.
# Never pass a secret as an argument to this function (arguments are printed in dry-run).
act() {
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*" >&2; return 0; fi
  "$@"
}

# Read-only AWS calls. In --dry-run without credentials they return nothing instead of failing.
HAVE_CREDS=0
check_aws() {
  command -v aws >/dev/null 2>&1 || {
    if [ "$DRY_RUN" = 1 ]; then say "  (AWS CLI not installed: dry-run shows the plan only)"; return 0; fi
    die "AWS CLI not found. Use AWS CloudShell (see deploy/README.md), or install the AWS CLI v2."
  }
  if aws sts get-caller-identity >/dev/null 2>&1; then
    HAVE_CREDS=1
  elif [ "$DRY_RUN" = 1 ]; then
    say "  (not logged in to AWS: dry-run shows the plan only)"
  else
    die "Not logged in to AWS (aws sts get-caller-identity failed). Open AWS CloudShell, or run 'aws configure sso'."
  fi
}
ro() { if [ "$HAVE_CREDS" = 1 ]; then aws "$@" 2>/dev/null || true; fi; }

account_id() {
  if [ "$HAVE_CREDS" = 1 ]; then aws sts get-caller-identity --query Account --output text; else echo "<account-id>"; fi
}
bucket_name() { echo "setuhaul-backups-$(account_id)-$REGION"; }

instance_id() {
  ro ec2 describe-instances \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text | grep -v '^None$' || true
}
eip_alloc() {
  ro ec2 describe-addresses --filters "Name=tag:Name,Values=$EIP_NAME" \
    --query 'Addresses[0].AllocationId' --output text | grep -v '^None$' || true
}
