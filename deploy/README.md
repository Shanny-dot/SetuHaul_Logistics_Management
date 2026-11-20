# Deploy SetuHaul to AWS for under US$10 a month

This runbook puts the SetuHaul WhatsApp demo on **one small server in Mumbai** (ap-south-1) with HTTPS, so Twilio can reach it without ngrok and without your laptop staying on. It is written for a first-time AWS user. Each step says what it costs.

> This is a **demo / pilot-demo configuration**: one machine, SQLite, a single availability zone. It is not the production design (see `docs/deployment.md`).

## What you get, and what it costs

| Item | Monthly cost (ap-south-1, on-demand, checked 2026-11-20) |
|---|---|
| EC2 **t4g.micro** (2 vCPU arm64, 1 GiB RAM), CPU credits *standard* | about $4.09 ($0.0056/h) |
| **Elastic IP** (one public IPv4 address) | about $3.65 ($0.005/h, **billed even when the server is stopped**) |
| **EBS gp3 10 GiB**, encrypted | about $0.91 ($0.0912/GB-month; re-check the EBS pricing page) |
| **S3** backups (30 days kept) | about $0.01-0.10 |
| IAM role, security group, SSM parameters, CloudWatch alarm, AWS Budget | $0 |
| **Total** | **about $8.75-8.85** |

Nothing else is created: no load balancer, NAT gateway, RDS, ElastiCache, Fargate, Secrets Manager or CloudFront. With new-account Free Tier credits ($100, plus up to $100 more), this costs nothing until the credits run out.

```
 Driver's WhatsApp -> Twilio Sandbox --HTTPS--> Elastic IP : 443
                                                   |
            EC2 t4g.micro (Amazon Linux 2023, Docker Compose)
            +-- caddy  (HTTPS certificate; only /webhook/twilio, /webhook/twilio/status, /healthz are public)
            +-- app    (FastAPI, 1 uvicorn worker, SQLite on /opt/setuhaul/data, outbox worker inside)
            +-- redis  (sessions, dedupe, locks; 64 MB cap)
            nightly backup -> private S3 bucket (30-day expiry); secrets <- SSM Parameter Store
```

---

## Steps

Each step takes a few minutes. Every script first prints **what it will create and the monthly cost**, then waits until you type `yes`. Every script can be run again safely: it reuses what exists. Add `--dry-run` to any AWS script to see the plan without changing anything.

### 1. Make the AWS account safe (once, about 10 minutes)
1. Create the account at **aws.amazon.com/free**. Choose the **Free plan**, which cannot charge you; you can upgrade later and keep your credits.
2. **Turn on MFA for the root user:** top-right menu → *Security credentials* → *Assign MFA device*.
3. In the top bar, switch the region to **Asia Pacific (Mumbai) ap-south-1**. Every step below is in Mumbai.
4. Don't create access keys. You'll use **CloudShell**, the terminal inside the AWS console, which is already logged in.

### 2. Put the deploy files on GitHub (on your laptop)
The server downloads the code from your public fork, so push the branch first:
```
git push -u fork whatsapp-api
```
(Or use VS Code → Source Control → … → Push to… → `fork`.)

### 3. Open CloudShell and get the scripts
In the AWS console, click the **CloudShell** icon (`>_`) in the top bar, with the region still Mumbai. Then:
```
git clone -b whatsapp-api https://github.com/Shanny-dot/SetuHaul_Logistics_Management.git
cd SetuHaul_Logistics_Management
```

### 4. Create the server's permission role ($0)
```
deploy/aws/create_role.sh
```
This lets the server use Session Manager (no SSH), read only `/setuhaul/*` secrets, and only *write* backups to the bucket.

### 5. Create the backup bucket (~$0.10/month)
```
deploy/aws/create_bucket.sh
```
The bucket is private and encrypted, accepts HTTPS only, and deletes backups after 30 days.

### 6. Store your secrets ($0)
```
deploy/aws/put_secrets.sh
```
Paste each value when asked. **Nothing appears on screen**, and nothing is saved in a file or in shell history.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`: from the Twilio Console home page.
- `ANTHROPIC_API_KEY`, `LANGSMITH_API_KEY`: optional; press Enter to skip.
- `DEMO_PHONE`: your WhatsApp number, e.g. `+91XXXXXXXXXX`, for the one-phone demo.
- `DEMO_PHONES`: optional, for several handsets, e.g. `DRV013=+91…,CON003=+91…`.

### 7. Launch the server (~$8.65/month)
```
deploy/aws/launch_instance.sh
```
At the end it prints your **hostname**, for example `13-233-45-67.sslip.io`, and the two Twilio URLs. **Write them down.**

On first boot the server installs Docker, adds 2 GiB of swap, downloads the code, builds the app and starts it. This takes **about 5-10 minutes**.

### 8. Check the server is healthy
1. EC2 console → **Instances** → `setuhaul` → **Connect** → **Session Manager** → **Connect**. A terminal opens in your browser.
2. Run:
```
sudo tail -n 30 /var/log/setuhaul-bootstrap.log       # should end with "DEPLOYED <commit>" and "bootstrap finished"
sudo /opt/setuhaul/src/deploy/server/healthcheck.sh
```
You want `OK /readyz` and `OK https://<host>/healthz`. If HTTPS says WARN, wait a minute (the certificate is being issued) and run it again.

### 9. Point Twilio at the server
Twilio Console → Messaging → Try it out → Send a WhatsApp message → **Sandbox settings**:

| Field | Value | Method |
|---|---|---|
| When a message comes in | `https://<your-host>/webhook/twilio` | POST |
| Status callback URL | `https://<your-host>/webhook/twilio/status` | POST |

Click **Save**. Each test phone must have sent `join <code>` to +1 415 523 8886 within the last 3 days.

### 10. Run a demo scenario
In the Session Manager terminal:
```
sudo /opt/setuhaul/src/deploy/server/reset-demo.sh C03
```
This gives you a fresh 4 Aug world, maps your phone to driver DRV013, and sets the clock to 09:19. Then, on WhatsApp, send `I am late by one hour.` to +1 415 523 8886 and reply `1`.

Other scenarios work the same way: `C06`, `C07`, `C12`, `C14`. See `docs/twilio_live_test.md` for what to send and what to expect.

### 11. Turn on the safety nets ($0)
Back in CloudShell:
```
deploy/aws/create_alarm_and_budget.sh
```
- A **CloudWatch alarm** recovers the server automatically if AWS hardware fails. You keep the same IP and the same disk.
- An **AWS Budget** of $10 emails you when real usage passes **$8** or **$10**, or when the forecast passes $10. Credits are excluded, so you see what the server really costs even while credits pay for it.

---

## Running it day to day

### Look at the coordinator side (private)
The console API and `/dev/*` are **not** reachable from the internet; only the two webhooks and `/healthz` are. Use them inside a Session Manager terminal:
```
TOKEN=$(curl -s localhost:8000/auth/dev-login -H 'content-type: application/json' \
  -d '{"user_id":"neha.bansal@setuhaul.example"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s localhost:8000/console/holds -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/console/holds/HLD-0001/decide -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"approve":true}'
curl -s localhost:8000/console/outbox -H "Authorization: Bearer $TOKEN"
```

### Update to the latest code
1. On your laptop, commit and push to the fork.
2. On the server:
```
sudo /opt/setuhaul/src/deploy/server/update.sh
```
This pulls, builds a new image, restarts, and checks `/readyz`. **If `/readyz` fails within 2 minutes, it rolls back** to the previous commit and image on its own.

A restart resets the demo clock, so run `reset-demo.sh` afterwards for a clean demo.

To roll back by hand: `sudo cat /opt/setuhaul/APP_TAG` shows the current image. To go back, run `cd /opt/setuhaul/src && sudo git reset --hard <older-commit> && sudo /opt/setuhaul/src/deploy/server/update.sh --no-pull`.

### Backups and restore (test this once)
Backups protect your **data**, not uptime. A backup runs **every night at 02:30 IST** and goes to `s3://setuhaul-backups-<account>-ap-south-1/backups/`.

1. **Take one now:** `sudo /opt/setuhaul/src/deploy/server/backup.sh`. It prints `integrity=ok` and the S3 path.
2. **Test a restore without touching the live database.** In the S3 console, open the bucket → `backups/` → pick a file → **Download**. Then, in CloudShell (Actions → Upload file) or on your laptop:
   ```
   deploy/server/restore-check.sh demo_world-XXXXXXXXTXXXXXXZ.db.gz
   ```
   It restores into a scratch copy and prints `integrity_check: ok` plus row counts. The server role can only *write* backups (least privilege), which is why you download through the console.
3. **Real restore.** Put the file on the server (it has the last 3 local copies in `/opt/setuhaul/backups/`), then:
   ```
   cd /opt/setuhaul/src/deploy/server && sudo docker compose --env-file /opt/setuhaul/compose.env stop app
   sudo sh -c 'gunzip -c /opt/setuhaul/backups/<file>.db.gz > /opt/setuhaul/data/demo_world.db && chown 10001:10001 /opt/setuhaul/data/demo_world.db && rm -f /opt/setuhaul/data/demo_world.db-wal /opt/setuhaul/data/demo_world.db-shm'
   sudo docker compose --env-file /opt/setuhaul/compose.env start app && sudo ./healthcheck.sh --wait 60
   ```

### Memory (1 GiB box): confirm nothing was killed
```
sudo docker stats --no-stream          # caddy <128 MiB, app <512 MiB, redis <96 MiB
free -m                                 # 2 GiB swap is there
sudo dmesg | grep -i -E 'out of memory|oom'   # no output = no process killed for memory
```
`healthcheck.sh` reports all of these too.

### HTTPS hostname (sslip.io, DuckDNS or your own domain)
The default is `<ip-with-dashes>.sslip.io`, which needs no signup. Caddy gets a free Let's Encrypt certificate and keeps it on disk, so restarts don't request a new one.

If certificate issuance fails (Let's Encrypt limits certificates per domain, and sslip.io is shared by many people), use your own name:
1. Get a free name at **duckdns.org** and point it at your Elastic IP.
2. On the server: `echo 'DOMAIN=myname.duckdns.org' | sudo tee -a /opt/setuhaul/settings.env`.
3. Run `sudo /opt/setuhaul/src/deploy/server/update.sh --no-pull`.
4. Paste the new URLs into Twilio.

`TWILIO_WEBHOOK_BASE_URL` always follows `DOMAIN`, and **it must match the URL in Twilio exactly**, or Twilio's signature check fails and the app answers 403.

### Pausing vs deleting
- **Stopping** the instance (EC2 → Instance state → Stop) saves ~$4.09 but **not** the Elastic IP's ~$3.65 or the disk's ~$0.91.
- **To stop all charges**, run **`deploy/aws/teardown.sh`** in CloudShell. It releases the Elastic IP and terminates the server. It then asks separately before deleting the disk and the backup bucket, so you can keep your data. Type `delete setuhaul` to confirm.
- Afterwards check **EC2 → Elastic IPs / Volumes** and **Billing → Bills**.

---

## If something goes wrong

| Symptom | Check | Fix |
|---|---|---|
| WhatsApp messages get no reply | Twilio Console → Monitor → Logs → Errors | **11200 + 403**: the Sandbox URL doesn't exactly match `https://<DOMAIN>/webhook/twilio`. **63015**: the phone must re-join the sandbox. |
| `healthcheck.sh`: `/readyz` FAIL | `sudo docker compose --env-file /opt/setuhaul/compose.env -f /opt/setuhaul/src/deploy/server/docker-compose.yml logs --tail 50 app` | Missing secret: re-run `put_secrets.sh`, then `sudo …/load-env.sh && sudo …/update.sh --no-pull` |
| HTTPS WARN for more than 10 minutes | `… logs caddy` | Certificate rate-limited or port 80 blocked: use a DuckDNS `DOMAIN` (see above) |
| "Connect" button greyed out | Instance → Security → IAM role must be `setuhaul-ec2-role` | Re-run `create_role.sh`, then reboot the instance |
| Reply is about the wrong trip or time | Demo clock | `sudo …/reset-demo.sh C03` (or the scenario you want) |
| Budget email arrives | Billing → Bills | Run `teardown.sh` if the demo is over |

## What you must do by hand (checklist)
- [ ] Create the AWS account (Free plan), turn on root MFA, select Mumbai
- [ ] Push `whatsapp-api` to your fork (step 2)
- [ ] Run steps 4-7 and 11 in CloudShell, typing `yes` at each prompt
- [ ] Paste the two webhook URLs into Twilio's Sandbox settings and **Save** (step 9)
- [ ] Join the sandbox (`join <code>`) on every test phone, at least once every 3 days
- [ ] Take and restore-check one backup (Backups, steps 1-2)
- [ ] When finished: `deploy/aws/teardown.sh`, then check Billing

## Files
| Path | What it does |
|---|---|
| `aws/common.sh` | Shared helpers: Mumbai region, `--dry-run`, cost line, `yes` prompt |
| `aws/create_role.sh` | IAM role + instance profile (SSM core + least-privilege inline policy) |
| `aws/create_bucket.sh` | Private S3 backup bucket, 30-day lifecycle, TLS-only; saves its name in SSM |
| `aws/put_secrets.sh` | Hidden prompts → SSM SecureString `/setuhaul/*` (values go via stdin, never arguments) |
| `aws/launch_instance.sh` | Security group (80/443), t4g.micro (IMDSv2, standard credits, 10 GiB gp3 encrypted, no key), Elastic IP |
| `aws/create_alarm_and_budget.sh` | Auto-recover alarm; $10 budget with alerts at $8/$10 |
| `aws/teardown.sh` | Releases the EIP, terminates the server; asks before deleting the disk and the bucket |
| `server/bootstrap.sh` | First boot: Docker, compose/buildx (checksums verified), swap, folders, cron, first deploy |
| `server/Dockerfile`, `entrypoint.sh` | python:3.11-slim, wheels only, 1 uvicorn worker with proxy headers; creates the demo DB copy |
| `server/docker-compose.yml`, `Caddyfile` | caddy + app + redis with memory caps; only the webhooks and `/healthz` are public |
| `server/load-env.sh` | Builds `/opt/setuhaul/.env` (chmod 600) from the template, settings and SSM |
| `server/update.sh`, `healthcheck.sh` | Deploy with automatic rollback; health, memory, OOM and disk report |
| `server/backup.sh`, `sqlite_backup.py`, `setuhaul-backup.cron`, `restore-check.sh` | Nightly backup to S3; restore test |
| `server/reset-demo.sh` | Fresh demo world, phones mapped, clock set per scenario |
| `.env.template` | Every variable the app reads, with demo defaults (no secrets) |
| `tests/local_smoke.sh` | No-Docker test: real Caddyfile + app + signed webhook + backup/restore |
| `tests/smoke.sh` | Docker test: builds the arm64 image and runs the real compose stack |
