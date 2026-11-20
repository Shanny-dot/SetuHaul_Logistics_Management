#!/bin/bash
# Delete what the setup created. Asks before each billable item; the data volume and the backup bucket are
# kept unless you say so (they hold the database). Stopping the instance is NOT enough: the Elastic IP keeps
# billing (~US$3.65/month) until it is released, which this script does.
# Usage: deploy/aws/teardown.sh [--dry-run]
. "$(dirname "$0")/common.sh"
check_aws
IID="$(instance_id)"; ALLOC="$(eip_alloc)"; BUCKET="$(bucket_name)"
VOL="$( [ -n "$IID" ] && ro ec2 describe-volumes --filters "Name=attachment.instance-id,Values=$IID" --query 'Volumes[0].VolumeId' --output text | grep -v None || true)"
[ -z "$VOL" ] && VOL="$(ro ec2 describe-volumes --filters "Name=tag:Name,Values=$INSTANCE_NAME-root" --query 'Volumes[0].VolumeId' --output text | grep -v None || true)"

step "Found (billable items marked \$)"
say "  \$ instance          ${IID:-none}            ~US\$4.09/month while running"
say "  \$ Elastic IP        ${ALLOC:-none}          ~US\$3.65/month until RELEASED (even if stopped)"
say "  \$ volume            ${VOL:-none}            ~US\$0.91/month until DELETED (kept after termination)"
say "  \$ S3 bucket         s3://$BUCKET            ~US\$0.01-0.10/month"
say "    alarm, budget, security group, IAM role, SSM parameters   free"
if [ "$DRY_RUN" = 0 ]; then
  read -r -p "  Type 'delete setuhaul' to delete the instance and release the Elastic IP: " a
  [ "$a" = "delete setuhaul" ] || { say "  Cancelled. Nothing was changed."; exit 1; }
fi
real() { [ "$DRY_RUN" = 0 ]; }
yesno() { [ "$DRY_RUN" = 1 ] && return 0; local a; read -r -p "  $1 [y/N] " a; [ "$a" = y ] || [ "$a" = Y ]; }

if [ -n "$ALLOC" ]; then
  ASSOC="$(ro ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].AssociationId' --output text | grep -v None || true)"
  [ -n "$ASSOC" ] && act aws ec2 disassociate-address --association-id "$ASSOC"
  act aws ec2 release-address --allocation-id "$ALLOC" && real && say "  released Elastic IP $ALLOC"
fi
if [ -n "$IID" ]; then
  act aws ec2 terminate-instances --instance-ids "$IID" >/dev/null && real && say "  terminating $IID ..."
  [ "$DRY_RUN" = 0 ] && aws ec2 wait instance-terminated --instance-ids "$IID"
fi
act aws cloudwatch delete-alarms --alarm-names "$ALARM_NAME"

if [ -n "$VOL" ] && yesno "Delete the data volume $VOL? (the demo database is on it; a backup is in S3)"; then
  act aws ec2 delete-volume --volume-id "$VOL" && real && say "  deleted volume $VOL"
elif [ -n "$VOL" ]; then say "  kept volume $VOL (~US\$0.91/month). Delete later: aws ec2 delete-volume --volume-id $VOL"; fi

if yesno "Empty and delete the backup bucket s3://$BUCKET?"; then
  act aws s3 rm "s3://$BUCKET" --recursive
  act aws s3api delete-bucket --bucket "$BUCKET" && real && say "  deleted s3://$BUCKET"
else say "  kept s3://$BUCKET (backups expire after 30 days on their own)"; fi

if yesno "Delete the free leftovers too (budget, security group, IAM role, SSM parameters)?"; then
  act aws budgets delete-budget --region us-east-1 --account-id "$(account_id)" --budget-name "$BUDGET_NAME" || true
  SG="$(ro ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" --query 'SecurityGroups[0].GroupId' --output text | grep -v None || true)"
  [ -n "$SG" ] && act aws ec2 delete-security-group --group-id "$SG"
  act aws iam remove-role-from-instance-profile --instance-profile-name "$ROLE_NAME" --role-name "$ROLE_NAME" || true
  act aws iam delete-instance-profile --instance-profile-name "$ROLE_NAME" || true
  act aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name setuhaul-least-privilege || true
  act aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore || true
  act aws iam delete-role --role-name "$ROLE_NAME" || true
  for n in $(ro ssm get-parameters-by-path --path "$PARAM_PATH" --query 'Parameters[].Name' --output text); do
    act aws ssm delete-parameter --name "$n"
  done
fi
step "Check nothing billable is left"
say "  EC2 console (ap-south-1): Instances, Elastic IPs, Volumes all empty for 'setuhaul'"
say "  Billing > Bills: the next bill should show no EC2/EBS/VPC (public IPv4) charges"
