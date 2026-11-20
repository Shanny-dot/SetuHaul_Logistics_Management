#!/bin/bash
# Create the private backup bucket: public access blocked, encrypted, TLS-only, backups deleted after 30 days.
# Also records the bucket name in SSM (/setuhaul/BACKUP_BUCKET, plain String) so the server knows it.
# Usage: deploy/aws/create_bucket.sh [--dry-run]
. "$(dirname "$0")/common.sh"
check_aws
BUCKET="$(bucket_name)"

LIFECYCLE='{"Rules":[{"ID":"expire-backups-30d","Status":"Enabled","Filter":{"Prefix":"backups/"},"Expiration":{"Days":30},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
TLS_ONLY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*",
 "Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
)

step "S3 bucket $BUCKET"
cost "private S3 bucket s3://$BUCKET in $REGION, 30-day expiry on backups/" \
     "about US\$0.01-0.10/month (30 nightly backups of a few MB; S3 Standard ~\$0.025/GB-month)"
EXISTS=0
[ "$HAVE_CREDS" = 1 ] && aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 && EXISTS=1
[ "$EXISTS" = 1 ] && say "  Bucket already exists: settings will be re-applied."
confirm

if [ "$EXISTS" = 0 ]; then
  act aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
fi
act aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
act aws s3api put-bucket-encryption --bucket "$BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
act aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration "$LIFECYCLE" >/dev/null
act aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$TLS_ONLY"
act aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging "TagSet=[{Key=Project,Value=$PROJECT}]"
act aws ssm put-parameter --name "$PARAM_PATH/BACKUP_BUCKET" --type String --value "$BUCKET" --overwrite >/dev/null
say "  Done: s3://$BUCKET (private, encrypted, 30-day expiry). Name saved as $PARAM_PATH/BACKUP_BUCKET."
