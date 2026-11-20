#!/bin/bash
# Build /opt/setuhaul/.env (chmod 600, root only):
#   1. non-secret defaults from deploy/.env.template
#   2. computed: DOMAIN (<elastic-ip-with-dashes>.sslip.io) and TWILIO_WEBHOOK_BASE_URL=https://$DOMAIN
#   3. your overrides in /opt/setuhaul/settings.env
#   4. secrets from SSM Parameter Store /setuhaul/* (SecureString, decrypted with the instance role)
# Nothing is printed except variable NAMES.
. "$(dirname "$0")/lib.sh"
need_root
umask 077
TOKEN="$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
IP="$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || true)"
PARAMS="$(mktemp)"; trap 'rm -f "$PARAMS"' EXIT
aws ssm get-parameters-by-path --region "$REGION" --path /setuhaul --recursive --with-decryption --output json > "$PARAMS"
IP="$IP" python3 - "$SRC/deploy/.env.template" "$SETUHAUL_HOME/settings.env" "$PARAMS" "$SETUHAUL_HOME/.env" <<'PY'
import json, os, re, sys
template, settings, params, out = sys.argv[1:5]
env = {}
def read(path):
    d = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
            if m and m.group(2) != "":
                d[m.group(1)] = m.group(2)
    return d
env.update(read(template))
ip = os.environ.get("IP", "")
if ip:
    env["DOMAIN"] = ip.replace(".", "-") + ".sslip.io"
env.update(read(settings))
secrets = {p["Name"].rsplit("/", 1)[1]: p["Value"] for p in json.load(open(params))["Parameters"]}
env.update(secrets)
if env.get("DOMAIN"):
    env["TWILIO_WEBHOOK_BASE_URL"] = "https://" + env["DOMAIN"]
env.setdefault("SEED_PHONES", env.get("DEMO_PHONES", ""))
missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "DOMAIN") if not env.get(k)]
with open(out, "w", encoding="utf-8") as f:
    for k in sorted(env):
        f.write(f"{k}={env[k].replace(chr(10), '')}\n")
os.chmod(out, 0o600)
print("wrote", out, "with", len(env), "variables:", ", ".join(sorted(env)))
if missing:
    print("MISSING:", ", ".join(missing)); sys.exit(1)
PY
