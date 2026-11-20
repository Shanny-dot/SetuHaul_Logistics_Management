#!/bin/bash
# 1. CloudWatch alarm: if AWS's hardware check fails (StatusCheckFailed_System) for 2 minutes, EC2 recovers the
#    instance onto healthy hardware (same instance id, same Elastic IP, same volume).
# 2. AWS Budget of US$10/month with email alerts at $8 (80%) and $10 (100%) of ACTUAL spend, and when the
#    FORECAST passes $10. Credits are excluded, so you see the real usage even while free credits pay the bill.
# Usage: deploy/aws/create_alarm_and_budget.sh [--dry-run]     (asks for the alert email; it is not stored in the repo)
. "$(dirname "$0")/common.sh"
check_aws
BUDGET_USD="${BUDGET_USD:-10}"
IID="$(instance_id)"
ACCT="$(account_id)"
[ "$HAVE_CREDS" = 1 ] && [ -z "$IID" ] && die "No instance named '$INSTANCE_NAME'. Run launch_instance.sh first."

step "Plan"
cost "CloudWatch alarm '$ALARM_NAME' (recover on system status check failure) for ${IID:-<instance>}" \
     "US\$0.00/month (CloudWatch free tier includes 10 alarms; status-check metrics are free)"
[ "$REGION" = ap-southeast-2 ] && [ "$BUDGET_USD" = 10 ] && say "  NOTE: in Sydney the list price (~\$12.4) is above \$10, so the \$8 and \$10 emails will arrive every month (credits still pay). Run with BUDGET_USD=15 for quieter alerts."
cost "AWS Budget '$BUDGET_NAME' US\$$BUDGET_USD/month, email at 80% and 100% (actual) and 100% (forecast)" \
     "US\$0.00/month (budgets without actions are free)"
EMAIL="${BUDGET_EMAIL:-}"
if [ -z "$EMAIL" ] && [ "$DRY_RUN" = 0 ]; then read -r -p "  Email for budget alerts: " EMAIL; fi
[ "$DRY_RUN" = 1 ] && EMAIL="${EMAIL:-you@example.com}"
case "$EMAIL" in *@*.*) ;; *) die "That does not look like an email address." ;; esac
confirm

act aws cloudwatch put-metric-alarm --alarm-name "$ALARM_NAME" \
  --alarm-description "Recover the SetuHaul instance when AWS hardware fails" \
  --namespace AWS/EC2 --metric-name StatusCheckFailed_System --dimensions "Name=InstanceId,Value=${IID:-i-unknown}" \
  --statistic Maximum --period 60 --evaluation-periods 2 --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold \
  --alarm-actions "arn:aws:automate:$REGION:ec2:recover"

BUDGET=$(cat <<JSON
{"BudgetName":"$BUDGET_NAME","BudgetLimit":{"Amount":"$BUDGET_USD","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST",
 "CostTypes":{"IncludeCredit":false,"IncludeRefund":false,"IncludeTax":true}}
JSON
)
sub="{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$EMAIL\"}"
NOTES="[
 {\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":80,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[$sub]},
 {\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[$sub]},
 {\"Notification\":{\"NotificationType\":\"FORECASTED\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[$sub]}]"
# AWS Budgets is a global service; its API lives in us-east-1 (no resources are created there).
if [ -n "$(ro budgets describe-budget --region us-east-1 --account-id "$ACCT" --budget-name "$BUDGET_NAME" --query Budget.BudgetName --output text)" ]; then
  say "  Budget '$BUDGET_NAME' already exists: kept as it is (edit it in Billing > Budgets)."
else
  act aws budgets create-budget --region us-east-1 --account-id "$ACCT" --budget "$BUDGET" --notifications-with-subscribers "$NOTES"
fi
say "  Done. AWS emails $EMAIL when spend passes 80% or 100% of \$$BUDGET_USD, or the forecast passes \$$BUDGET_USD."
