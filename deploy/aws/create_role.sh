#!/bin/bash
# Create the instance's IAM role + instance profile (least privilege).
#   AmazonSSMManagedInstanceCore   -> Session Manager access (no SSH, no port 22)
#   inline "setuhaul-least-privilege":
#     ssm:GetParameter(s)(ByPath) on parameter/setuhaul and parameter/setuhaul/* only
#     s3:PutObject on the backup bucket only
# Usage: deploy/aws/create_role.sh [--dry-run]
. "$(dirname "$0")/common.sh"
check_aws
ACCT="$(account_id)"
BUCKET="$(bucket_name)"

TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"ReadSetuhaulParameters","Effect":"Allow",
  "Action":["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],
  "Resource":["arn:aws:ssm:$REGION:$ACCT:parameter$PARAM_PATH","arn:aws:ssm:$REGION:$ACCT:parameter$PARAM_PATH/*"]},
 {"Sid":"WriteBackups","Effect":"Allow","Action":["s3:PutObject"],
  "Resource":["arn:aws:s3:::$BUCKET/backups/*"]}
]}
JSON
)

step "IAM role $ROLE_NAME"
cost "IAM role + instance profile '$ROLE_NAME' (SSM core + read /setuhaul/*, write s3://$BUCKET/backups/)" "US\$0.00/month (IAM is free)"
EXISTS="$(ro iam get-role --role-name "$ROLE_NAME" --query Role.RoleName --output text)"
[ -n "$EXISTS" ] && say "  Role already exists: it will be kept, and its policies refreshed."
confirm

if [ -z "$EXISTS" ]; then
  act aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" \
      --description "SetuHaul demo EC2 instance" --tags "Key=Project,Value=$PROJECT" >/dev/null
fi
act aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
act aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name setuhaul-least-privilege --policy-document "$POLICY"

if [ -z "$(ro iam get-instance-profile --instance-profile-name "$ROLE_NAME" --query InstanceProfile.InstanceProfileName --output text)" ]; then
  act aws iam create-instance-profile --instance-profile-name "$ROLE_NAME" >/dev/null
fi
if ! ro iam get-instance-profile --instance-profile-name "$ROLE_NAME" --query 'InstanceProfile.Roles[].RoleName' --output text | grep -qw "$ROLE_NAME"; then
  act aws iam add-role-to-instance-profile --instance-profile-name "$ROLE_NAME" --role-name "$ROLE_NAME"
fi
say "  Done: role and instance profile '$ROLE_NAME'. (A new instance profile can take ~10 s before EC2 accepts it.)"
