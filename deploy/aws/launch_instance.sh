#!/bin/bash
# Launch the one EC2 instance and attach one Elastic IP.
#   t4g.micro (arm64), Amazon Linux 2023, 10 GiB encrypted gp3, IMDSv2 only, CPU credits STANDARD,
#   no SSH key, security group with 80/443 only, instance role from create_role.sh.
#   First boot (user-data) installs git, clones the repo and runs deploy/server/bootstrap.sh.
# Usage: deploy/aws/launch_instance.sh [--dry-run]
#   REPO_URL (default: the Shanny-dot fork) and BRANCH (default: whatsapp-api) can be overridden.
. "$(dirname "$0")/common.sh"
check_aws
REPO_URL="${REPO_URL:-https://github.com/Shanny-dot/SetuHaul_Logistics_Management.git}"
BRANCH="${BRANCH:-whatsapp-api}"
case "$REPO_URL" in https://*) ;; *) die "REPO_URL must be an https:// git URL (public repo, no token)";; esac

step "Plan"
cost "EC2 t4g.micro (on-demand, Linux, ap-south-1)" "about US\$4.09/month (\$0.0056/hour x 730 h)"
cost "Elastic IP (one public IPv4 address)"        "about US\$3.65/month (\$0.005/hour, charged even when the instance is stopped)"
cost "EBS gp3 10 GiB, encrypted (root volume)"     "about US\$0.91/month (\$0.0912/GB-month; check the EBS pricing page)"
cost "security group '$SG_NAME' (80, 443 in)"      "US\$0.00"
say  "  TOTAL for this script: about US\$8.65/month (+ S3 backups ~\$0.10 = ~\$8.75; plan figure ~\$8.85 with EBS rounded to \$1)"

# ---------------------------------------------------------------- network
VPC="$(ro ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text | grep -v None || true)"
if [ "$HAVE_CREDS" = 1 ] && [ -z "$VPC" ]; then
  die "No default VPC in $REGION. Create one (free): aws ec2 create-default-vpc   then run this again."
fi
SUBNET="$(ro ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" Name=default-for-az,Values=true \
          --query 'sort_by(Subnets,&AvailabilityZone)[0].SubnetId' --output text | grep -v None || true)"
SG="$(ro ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC" \
      --query 'SecurityGroups[0].GroupId' --output text | grep -v None || true)"
IID="$(instance_id)"
ALLOC="$(eip_alloc)"
say ""
say "  Found: VPC=${VPC:-?} subnet=${SUBNET:-?} security group=${SG:-<new>} instance=${IID:-<new>} elastic IP=${ALLOC:-<new>}"
[ -n "$IID" ] && say "  An instance named '$INSTANCE_NAME' already exists: it will be reused (no second instance)."
confirm

# ---------------------------------------------------------------- security group
if [ -z "$SG" ]; then
  if [ "$DRY_RUN" = 1 ]; then act aws ec2 create-security-group --group-name "$SG_NAME" ...; SG="sg-<new>"
  else
    SG="$(aws ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC" \
          --description "SetuHaul demo: HTTP and HTTPS only" \
          --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=$SG_NAME},{Key=Project,Value=$PROJECT}]" \
          --query GroupId --output text)"
  fi
fi
for port in 80 443; do
  if ! ro ec2 describe-security-groups --group-ids "$SG" --query "SecurityGroups[0].IpPermissions[?FromPort==\`$port\`].FromPort" --output text | grep -q "$port"; then
    act aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port "$port" --cidr 0.0.0.0/0 >/dev/null
  fi
done
# Remove anything else that may have been added by hand (e.g. port 22).
EXTRA="$(ro ec2 describe-security-groups --group-ids "$SG" --query 'SecurityGroups[0].IpPermissions[?FromPort!=`80` && FromPort!=`443`].FromPort' --output text)"
[ -n "$EXTRA" ] && say "  WARNING: security group also allows port(s) $EXTRA. Remove them in the EC2 console (only 80/443 are planned)."

# ---------------------------------------------------------------- instance
if [ -z "$IID" ]; then
  AMI="$(ro ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 --query Parameter.Value --output text)"
  USERDATA="$(cat <<UD
#!/bin/bash
set -eux
dnf install -y git
mkdir -p /opt/setuhaul
[ -d /opt/setuhaul/src/.git ] || git clone --branch "$BRANCH" --depth 50 "$REPO_URL" /opt/setuhaul/src
bash /opt/setuhaul/src/deploy/server/bootstrap.sh "$REPO_URL" "$BRANCH" > /var/log/setuhaul-bootstrap.log 2>&1
UD
)"
  if [ "$DRY_RUN" = 1 ]; then
    act aws ec2 run-instances --instance-type t4g.micro --image-id "${AMI:-<al2023-arm64>}" "(no key pair, IMDSv2, standard credits, 10 GiB gp3 encrypted)"
    IID="i-<new>"
  else
    IID="$(aws ec2 run-instances \
      --image-id "$AMI" --instance-type t4g.micro --count 1 \
      --subnet-id "$SUBNET" --security-group-ids "$SG" \
      --iam-instance-profile "Name=$ROLE_NAME" \
      --metadata-options "HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=1" \
      --credit-specification CpuCredits=standard \
      --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=10,VolumeType=gp3,Encrypted=true,DeleteOnTermination=false}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME},{Key=Project,Value=$PROJECT}]" \
                           "ResourceType=volume,Tags=[{Key=Name,Value=$INSTANCE_NAME-root},{Key=Project,Value=$PROJECT}]" \
      --user-data "$USERDATA" \
      --query 'Instances[0].InstanceId' --output text)"
    say "  launched $IID; waiting until it is running ..."
    aws ec2 wait instance-running --instance-ids "$IID"
  fi
fi

# ---------------------------------------------------------------- elastic IP
if [ -z "$ALLOC" ]; then
  if [ "$DRY_RUN" = 1 ]; then act aws ec2 allocate-address --domain vpc ...; ALLOC="eipalloc-<new>"
  else
    ALLOC="$(aws ec2 allocate-address --domain vpc \
             --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$EIP_NAME},{Key=Project,Value=$PROJECT}]" \
             --query AllocationId --output text)"
  fi
fi
CUR="$(ro ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].InstanceId' --output text | grep -v None || true)"
if [ "$CUR" != "$IID" ]; then
  act aws ec2 associate-address --allocation-id "$ALLOC" --instance-id "$IID" >/dev/null
fi
IP="$(ro ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text)"
HOST="${IP:-<elastic-ip>}"; HOST="${HOST//./-}.sslip.io"

step "Result"
say "  Instance:   ${IID}   (connect: EC2 console > instance > Connect > Session Manager)"
say "  Public IP:  ${IP:-<after a real run>}"
say "  Hostname:   $HOST   (or set DOMAIN later, e.g. a DuckDNS name)"
say "  Twilio 'When a message comes in':  https://$HOST/webhook/twilio        (POST)"
say "  Twilio 'Status callback URL':      https://$HOST/webhook/twilio/status (POST)"
say "  First boot installs Docker and clones $BRANCH (about 3-5 minutes). Then follow deploy/README.md step 7."
