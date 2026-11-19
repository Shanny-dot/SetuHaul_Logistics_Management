# Live acceptance test: SetuHaul on WhatsApp through the Twilio Sandbox

This runbook is for whoever runs the live test with the team's sandbox-joined handsets.

**Status (19 Nov 2026):**
- Everything below runs offline in tests, and end to end through the local simulator (section 5).
- The live Twilio round trip in section 4 **has not been run yet**. It needs the team's handsets, a public URL (ngrok) and the Twilio credentials, and none of those were available to the build session.
- Record the evidence in section 6 when you run it. Don't mark a row as passed without a screenshot and a trace id.

## 1. What you need

| Item | Where |
|---|---|
| Twilio account SID and auth token | Twilio Console. Put them in `.env`, never on the command line. |
| WhatsApp Sandbox joined on every test handset | Console > Messaging > Try it out > WhatsApp. Each handset sends `join <code>` to +1 415 523 8886. The join lasts **3 days**. |
| A public HTTPS URL | `ngrok http 8000`. Copy the `https://…ngrok-free.app` URL. |
| Five driver handsets and one staff handset | `drivers.phone` is unique, so each role needs its own number. |
| Python venv | `cd prototype && python3 -m venv .venv && .venv/bin/pip install -r requirements-prod.txt` |

Sandbox limits (from Twilio's docs, also recorded in `setuhaul/config.py`):
- The sandbox sends at most **1 message every 3 seconds**. The worker paces itself when `TWILIO_SANDBOX=true`.
- Business-initiated messages may use **only the sandbox's pre-approved templates**, not custom ones.
- Typing `stop` makes a handset leave the sandbox. Twilio handles that word itself, so the app may never see it.

## 2. Configure

1. Copy `.env.example` to `.env` at the repo root and fill it in:
   - `CHANNEL=twilio`
   - `TWILIO_*`
   - `TWILIO_WEBHOOK_BASE_URL=<ngrok URL>`
   - `CLOCK_MODE=snapshot`
   - `SNAPSHOT_ANCHOR=2026-08-04T09:15:00+05:30`
   - `SETUHAUL_DB=out/demo_world.db`
   - `SETUHAUL_DEV=1`
2. Map the handsets onto a **copy** of the database. The supplied snapshot is never written.

   ```
   cd prototype
   cat > /tmp/handsets.csv <<'CSV'
   id,phone
   DRV013,+91XXXXXXXXXX
   DRV004,+91XXXXXXXXXX
   DRV006,+91XXXXXXXXXX
   DRV011,+91XXXXXXXXXX
   DRV015,+91XXXXXXXXXX
   CON003,+91XXXXXXXXXX
   CSV
   .venv/bin/python -I scripts/seed_demo_phones.py --csv /tmp/handsets.csv --out out/demo_world.db
   ```

   The handset columns are C03, C06 (Hindi), C07, C12 (safety) and C14. CON003 is the shift manager, who receives the safety page.

3. Optional, for buttons instead of numbered text:

   ```
   set -a; . ../.env; set +a
   .venv/bin/python -I scripts/create_twilio_content.py --create
   ```

   Put the printed `TWILIO_CONTENT_QR1..3` lines in `.env`, then set `TWILIO_INTERACTIVE=true`.

   **Verify first:** Twilio's docs don't confirm that the sandbox renders Content API quick-replies, or that variables work in button ids. If buttons don't appear, set it back to `false`. Numbered text uses the same offer binding.

4. In the Sandbox settings:
   - "When a message comes in": `<ngrok URL>/webhook/twilio`, POST
   - "Status callback URL": `<ngrok URL>/webhook/twilio/status`

   The app also sets `status_callback` on every message it sends.

## 3. Start

```
cd prototype
set -a; . ../.env; set +a
.venv/bin/uvicorn app:app --port 8000
curl -s localhost:8000/readyz          # db ok, redis ok (or memory), twilio "account active", clock_mode snapshot
```

The snapshot clock starts at the anchor and runs in real time. Move it forward between cases with the dev endpoint; it never goes back:

```
curl -s localhost:8000/dev/clock -H 'content-type: application/json' -d '{"as_of":"2026-08-04T09:19:00+05:30"}'
```

Sign in as the coordinator once:

```
TOKEN=$(curl -s localhost:8000/auth/dev-login -H 'content-type: application/json' \
  -d '{"user_id":"neha.bansal@setuhaul.example"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
```

## 4. Cases on real handsets

For every step, save a screenshot of the handset. Also save the trace from `curl -s localhost:8000/dev/trace/<MessageSid>`; the MessageSid is in the uvicorn log and in Twilio's message log.

| Case | Clock | On the handset | Expect |
|---|---|---|---|
| C03 | 09:19 | `I am late by one hour.` | Privacy notice, then three options ("Reply 1, 2 or 3", or buttons) |
| C03 | 09:21 | `1` (or tap the first button) | `Booked: Dock D2 …` with a ref `APT-N…` |
| C03 replay | | Twilio Console > Monitor > the inbound message > copy its parameters, or use the simulator's "Resend same" | No second reply. `SELECT COUNT(*) FROM appointments WHERE shipment_id='SHP1013' AND appointment_id LIKE 'APT-N%'` = 1 |
| C03 stale | | Before step 2, send `Sorry, reaching 11:40`, then tap a button from the **first** list | "Those options have changed … nothing was booked" plus the current list. No new appointment row. |
| C06 | 09:31 | `I will be late by 45 minutes.` | "You have 2 trips today. Which one…" |
| C06 | 09:32 | `शाम वाला` | Hindi options for the 6 PM trip |
| C06 | 09:33 | `1` | Hindi "मंज़ूरी माँगी है …" (approval requested; not booked) |
| C06 | 09:41 | `curl -s localhost:8000/console/holds -H "Authorization: Bearer $TOKEN"`, then `curl -s -X POST localhost:8000/console/holds/<HLD-…>/decide -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"approve":true}'` | The handset gets the Hindi "पक्का हो गया …" follow-up |
| C07 | 09:34 | `Traffic after Shahpura. Reaching around 11:20. Any slot after 12?` | Options |
| C07 | 09:35 | The same text again | No reply. The trace shows `dedupe:SUPPRESSED`. |
| C12 | 10:05 | `bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya` | Hinglish safety reply (112, shift manager named) |
| C12 | 10:07 | `slot kab milega?` | "Aapka safety case (ESC-…) …": no scheduling talk |
| C12 page | | Wait for the worker | Without `TWILIO_TPL_STAFF_PAGE_SID`, the staff page is **BLOCKED / NO_TEMPLATE** with an alarm. With a sandbox template SID, the CON003 handset gets it. Reply `ACK ESC-…` from that handset to stop the chain. |
| C12 clear | | `curl -s -X POST localhost:8000/console/safety/<ESC-…>/clear -H "Authorization: Bearer $TOKEN"` | Scheduling resumes for that driver |
| C14 | 16:24 | `Evening traffic. ETA 6:30. Can the reefer unload tonight?` | "I'm sorry - I can't find a reefer dock …" plus a named person and a time |

## 5. What to prove, and how to show it

| Claim | Evidence |
|---|---|
| A replayed webhook books once | Same MessageSid posted twice → one `wa_inbound` row, one appointment. `/console/outbox` shows one DRIVER_REPLY. |
| A stale button tap does not book | The trace shows `"binding": {"why": "OFFER_SUPERSEDED"}` and status `STALE_CHOICE`. The appointment count doesn't change. |
| A failed send shows its Twilio ErrorCode | Send to a number that has not joined the sandbox (or let a join expire). `/console/outbox` shows `FAILED` or `UNDELIVERED` with an `error_code` (e.g. 63015 / 63016), and `/console/alarms` has the alarm. |
| A proactive message outside the window uses a template or is blocked | `curl -s -X POST localhost:8000/console/proactive/SHP1013 -H "Authorization: Bearer $TOKEN"`. With no `TWILIO_TPL_DRIVER_CHECKIN_SID`, `/console/outbox` shows `BLOCKED NO_TEMPLATE` plus a `SEND_BLOCKED` alarm. With a sandbox template SID, the handset gets the template. |

## 6. Results

### Local simulator run (19 Nov 2026)

Run with `CHANNEL=sim` and `scripts/sim_play.py`. Webhooks were form-encoded and signed exactly as Twilio signs them; status callbacks were signed too.

| Check | Result |
|---|---|
| C03 options → pick | BOOKED, APT-N0001 (Dock D2 11:55 AM after the driver revised the ETA) |
| C03 stale tap (superseded list) | STALE_CHOICE; nothing booked; current list re-sent |
| C03 replayed webhook (same MessageSid) | 0 extra messages; SHP1013 has 1 booking |
| C06 which trip → options (Hindi) → hold → approve | HLD-0001 → CONFIRMED; Hindi follow-up delivered |
| C07 duplicate retry (new id, same text) | DUPLICATE_TEXT, `dedupe:SUPPRESSED` |
| C12 Hinglish safety, then a scheduling question | ESCALATED to Anita Joshi; next message SAFETY_HOLD |
| C14 no feasible slot | ESCALATED with a named person and time |
| Failed send | OUT-0027 UNDELIVERED, ErrorCode 63016, DELIVERY_UNDELIVERED alarm |
| Proactive and staff pages without template SIDs | BLOCKED NO_TEMPLATE + SEND_BLOCKED alarms |

### Live Twilio Sandbox run

| Case | Screenshot | Trace id | Result |
|---|---|---|---|
| C03 | | | not run yet |
| C06 | | | not run yet |
| C07 | | | not run yet |
| C12 | | | not run yet |
| C14 | | | not run yet |
| Replay books once | | | not run yet |
| Stale tap | | | not run yet |
| Failed send ErrorCode | | | not run yet |
| Proactive template / blocked | | | not run yet |

## 7. Known limits

- **List-picker reply field unknown.** Twilio doesn't document which inbound field carries a list-picker selection. The parser reads `ButtonPayload`, `ListId` and `InteractiveData`. With `MAX_OPTIONS_SHOWN = 3`, lists are not used today.
- **Possible double send after a crash.** If the process dies after Twilio accepts a message but before the SID is stored, the message can be sent twice. The Messages API has no idempotency key.
- **No timestamp on inbound webhooks.** Replay protection is MessageSid dedupe (24 h in Redis, permanent in SQLite).
- **Sandbox templates are generic.** Only Twilio's three samples can be used. Real templates are in `templates/twilio_templates.yaml`, ready for approval with a production sender.
- **Phone call fallback is a stub.** It writes an outbox row with status `STUB`.
