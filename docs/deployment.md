# Deployment: AWS demo configuration (pilot-demo)

**Status:** files written and tested locally (20 Nov 2026); not yet deployed to AWS. The runbook is `deploy/README.md`.

> **This is a demo / pilot-demo configuration, not the production design.** It runs the whole service on one small machine with SQLite, to let drivers try SetuHaul on real WhatsApp for under US$10 a month. The production path in the Decision Register (one container plus a managed Postgres database, then Doc A's AWS design at scale) is unchanged.

## Architecture

- **Region:** ap-south-1 (Mumbai) only. One availability zone.
- **Server:** one EC2 **t4g.micro** (arm64, 1 GiB RAM + 2 GiB swap), Amazon Linux 2023.
  - Disk: 10 GiB encrypted gp3.
  - Security: IMDSv2 only, CPU credits *standard*, no SSH key, no port 22. Administration through SSM Session Manager.
- **Network:** one Elastic IP. Hostname `<ip-with-dashes>.sslip.io`, or any `DOMAIN` (DuckDNS or your own). The security group allows only 80 and 443.
- **Containers (Docker Compose):**
  - `caddy`: automatic HTTPS. It exposes **only** `/webhook/twilio`, `/webhook/twilio/status` and `/healthz`.
  - `app`: FastAPI with **one** uvicorn worker, because SQLite has one writer and the engine uses `BEGIN IMMEDIATE`. The outbox worker runs inside it.
  - `redis`: 64 MB cap, `allkeys-lru`.
  - Memory caps: 128 / 512 / 96 MiB.
- **Data:** SQLite at `/opt/setuhaul/data/demo_world.db`, a copy of the supplied snapshot (the snapshot itself is never written). Redis data is on the same volume.
- **Backups:** nightly SQLite online backup with an integrity check, sent to a private S3 bucket with 30-day expiry. The restore procedure is in the runbook and was tested locally.
- **Secrets:** SSM Parameter Store SecureString under `/setuhaul/*`, read by the instance role. Non-secret settings are in a generated `.env` (chmod 600).
- **Monitoring:**
  - CloudWatch alarm on `StatusCheckFailed_System` with the EC2 **recover** action.
  - AWS Budget of US$10 with emails at $8 and $10.
- **Coordinator console and `/dev/*`:** reachable only from the machine itself (127.0.0.1), through a Session Manager terminal.

## Cost (list prices, ap-south-1, on-demand, checked 20 Nov 2026)

| Item | Unit price | Per month |
|---|---|---|
| EC2 t4g.micro | $0.0056 / hour | $4.09 |
| Elastic IP (public IPv4) | $0.005 / hour, billed even when the instance is stopped | $3.65 |
| EBS gp3, 10 GiB | $0.0912 / GB-month (from a 2021 source; re-check before launch) | $0.91 |
| S3 backups (≈30 × a few MB) | $0.025 / GB-month + requests | ≤ $0.10 |
| IAM, security group, SSM standard parameters, 1 CloudWatch alarm, 1 Budget | free | $0 |
| **Total** | | **≈ $8.75 (plan figure $8.85 with EBS rounded to $1)** |

New AWS accounts receive $100 in Free Tier credits, plus up to $100 more for console activities. On the Free plan the account stops after 6 months or when the credits run out, whichever comes first. Upgrading keeps the remaining credits until 12 months after sign-up. WhatsApp message charges are separate: they are billed by Twilio, and the Sandbox is free.

**Conflict to note, not resolved here.** The Decision Register, Working Prototype, Client Readout and Solution Design all give the **pilot** as "one small container plus managed Postgres, about US$20-30 a month". This page describes a cheaper **demo** configuration (about $8.85, SQLite, no managed database). Both figures are correct for what they describe. The Word documents were not edited; if the readout quotes a hosting cost, it should say which configuration it means.

## Limits (by design)

- **Single box, single AZ.** If the instance or the zone fails, the service is down until auto-recovery or a manual relaunch. Recovery keeps the volume; backups protect data, not uptime.
- **SQLite only:** one writer, one uvicorn worker. Fine for a demo and a handful of drivers. Not for two facilities at pilot volume with a coordinator console under load; that needs the Postgres path.
- **Demo clock.** In `CLOCK_MODE=snapshot`, every restart resets the clock to `SNAPSHOT_ANCHOR` while the database keeps its bookings. `reset-demo.sh` rebuilds a clean 4 Aug world for each demo run.
- **Twilio Sandbox:**
  - test phones must join with a code, and the join lasts 3 days;
  - it sends 1 message every 3 seconds;
  - business-initiated messages are limited to the sandbox's own templates, so proactive check-ins and staff pages are BLOCKED with an ops alarm, by design.
- **sslip.io** is a shared domain, so certificate issuance can hit Let's Encrypt limits. DOMAIN override documented.
- **No high availability, no autoscaling, no WAF, no central log storage.** Logs stay in Docker on the instance.

## What was verified (20 Nov 2026)

| Check | Result |
|---|---|
| Every Python pin has a Linux **arm64 / Python 3.11** wheel (`pip download --platform manylinux2014_aarch64 … --only-binary=:all:`) | 67 packages, all binary wheels |
| `shellcheck` on all deploy scripts | no warnings or errors |
| All six AWS scripts with `--dry-run` | each prints its plan and monthly cost; nothing created |
| `deploy/tests/local_smoke.sh`: the real `Caddyfile` (Caddy 2.8.4) in front of the app started like the container (1 worker, proxy headers), on localhost with TLS | healthz, readyz, private paths 404 from outside, **signed Twilio webhook through the proxy → turn ran**, bad signature → 403. Passed both with `TWILIO_WEBHOOK_BASE_URL` set and with the URL rebuilt from `X-Forwarded-Proto/Host` |
| Backup → gzip → restore into a scratch DB → integrity + counts | `integrity_check: ok` |
| Docker image build for linux/arm64 and the compose stack (`deploy/tests/smoke.sh`) | **Not run**: Docker is not installed on the build machine |
| Anything on AWS | **Not run**: no AWS credentials on the build machine (script mode) |
