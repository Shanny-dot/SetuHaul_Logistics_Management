# Live acceptance test: SetuHaul on WhatsApp through the Twilio Sandbox

This runbook is for whoever runs the live test with the team's sandbox-joined handsets.

**Status (19 Nov 2026):**
- Everything below runs offline in tests, and end to end through the local simulator (section 7).
- The live Twilio round trip in section 4 **has not been run yet**. It needs the team's handsets, a public URL (ngrok) and the Twilio credentials, and none of those were available to the build session.
- Record the evidence in section 7 when you run it. Don't mark a row as passed without a screenshot and a trace id.

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

Then, with ngrok running, run the pre-flight. It sends no WhatsApp message and prints no secret:

```
.venv/bin/python -I scripts/preflight.py --twilio-api
```

Fix every FAIL before you start. The WARN lines tell you which scenarios your handsets can play.

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

**Shortcut with one phone:** add `DEMO_PHONE=+91XXXXXXXXXX` to `.env`, keep ngrok running, then run `prototype/scripts/live_start.sh C03` (or C06, C07, C12, C14). It does four things:
- stops the previous app;
- maps your phone to that scenario's driver on a fresh database copy;
- checks that ngrok's address matches `.env`;
- starts the app and sets the clock.


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

## 5. If nothing happens

Run these first, in order:
1. `.venv/bin/python -I scripts/preflight.py`
2. `.venv/bin/python -I scripts/twilio_log_tail.py --hours 2`. This lists recent messages with their error codes, plus Debugger alerts.

Console equivalent: **Monitor > Logs > Messaging** (filter by the sandbox number) and **Monitor > Logs > Errors**.

| What you see | How to tell | Cause | Fix |
|---|---|---|---|
| No reply, and no POST in the uvicorn log | Twilio log: the inbound message is there, or the sandbox answered with its join instructions | **Join expired** (3 days) or never joined | Send `join <code>` from the handset again |
| No reply; Twilio Debugger shows **11200** | uvicorn log shows `403` on `/webhook/twilio`; preflight `probe` FAILs | **Wrong URL or token.** The app validates against a different URL (old ngrok address, http vs https, a path typo) or a different auth token. | Set `TWILIO_WEBHOOK_BASE_URL` to exactly the current ngrok URL, paste the same URL into the Sandbox settings, and restart the app |
| 11200 with a 404 or a timeout | uvicorn log shows nothing, or a 404 | Sandbox URL missing `/webhook/twilio`, or the app / ngrok is down | Fix the path; restart ngrok (a free ngrok URL changes on every restart) |
| One reply: "This number is not registered with SetuHaul" | Trace status `UNKNOWN_SENDER` | **Unmapped phone.** The handset isn't in the demo DB copy. | Add it to the CSV, re-run `seed_demo_phones.py`, restart the app; check preflight `phones` |
| The reply is about the wrong trip, "no slot", or the wrong times | `/readyz` `as_of` is not the time you expect | **Wrong clock.** `CLOCK_MODE=live`, an anchor off 4 Aug, or the clock already past the case's time | Use `CLOCK_MODE=snapshot`. `/dev/clock` only moves forward, so to go back, restart the app on a fresh `seed_demo_phones.py` copy |
| `/dev/clock` returns 400 "never goes back" | | The snapshot clock is already later | As above: restart on a fresh copy |
| A reply was decided but never arrives | `/console/outbox` shows the row `BLOCKED` / `FAILED` / `UNDELIVERED` with an `error_code` | 63016 (outside the 24 h window, or a proactive message without a template), 63015 (not joined), 63007 (wrong `TWILIO_WHATSAPP_FROM`) | See the code's hint in `twilio_log_tail.py`; templates are expected to be BLOCKED in the sandbox |
| A row is stuck in `NEEDS_REVIEW` | `/console/alarms` shows `SEND_NEEDS_REVIEW` | A crash or timeout left the send unproven, and reconciliation found 0 or several matches it couldn't settle | Find the message in Twilio's log, then `POST /console/outbox/{id}/resolve` with `{"action":"sent","provider_sid":"SM…"}` or `{"action":"resend"}` |
| Replies arrive slowly | | The sandbox sends 1 message every 3 s | Expected |
| Nothing comes back after the driver sent STOP | Trace status `IGNORED_OPTED_OUT`; in the sandbox, `stop` also leaves the sandbox | The driver opted out | Send `START` (and `join <code>` in the sandbox) |

## 6. What to prove, and how to show it

| Claim | Evidence |
|---|---|
| A replayed webhook books once | Same MessageSid posted twice → one `wa_inbound` row, one appointment. `/console/outbox` shows one DRIVER_REPLY. |
| A stale button tap does not book | The trace shows `"binding": {"why": "OFFER_SUPERSEDED"}` and status `STALE_CHOICE`. The appointment count doesn't change. |
| A failed send shows its Twilio ErrorCode | Send to a number that has not joined the sandbox (or let a join expire). `/console/outbox` shows `FAILED` or `UNDELIVERED` with an `error_code` (e.g. 63015 / 63016), and `/console/alarms` has the alarm. |
| A proactive message outside the window uses a template or is blocked | `curl -s -X POST localhost:8000/console/proactive/SHP1013 -H "Authorization: Bearer $TOKEN"`. With no `TWILIO_TPL_DRIVER_CHECKIN_SID`, `/console/outbox` shows `BLOCKED NO_TEMPLATE` plus a `SEND_BLOCKED` alarm. With a sandbox template SID, the handset gets the template. |

## 7. Results

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

**Not run yet.** Fill this in during the live test. "Expected" is in sections 4 and 6. A row counts only with a screenshot and a trace id (`/dev/trace/<MessageSid>`).

| Scenario | Handset | Expected | Actual | Screenshot | Trace id |
|---|---|---|---|---|---|
| C03 on the **AWS server** (20 Nov 2026) | owner's phone as DRV013 | options, then "Booked …" after `1` | options delivered and read; APT-N0001 CONFIRMED; "Booked" reply sent | (add) | `87a65c64…`, `28385148…` |
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |

## 8. Known limits

- **List-picker reply field unknown.** Twilio doesn't document which inbound field carries a list-picker selection. The parser reads `ButtonPayload`, `ListId` and `InteractiveData`. With `MAX_OPTIONS_SHOWN = 3`, lists are not used today.
- **No blind resend after a crash.** Twilio's Create Message has no idempotency key or client reference. So a per-attempt `send_ref` is stored before the call and carried in the signed status-callback URL (`?ref=`). A row left in SENDING is reconciled, not resent:
  - a callback with its ref → SENT
  - exactly one matching message in Twilio's log (To, From, time window, body) → SENT
  - a successful lookup that finds nothing → retry
  - anything else → NEEDS_REVIEW with an alarm. A person settles it with `POST /console/outbox/{id}/resolve`.
- **No timestamp on inbound webhooks.** Replay protection is MessageSid dedupe (24 h in Redis, permanent in SQLite).
- **Sandbox templates are generic.** Only Twilio's three samples can be used. Real templates are in `templates/twilio_templates.yaml`, ready for approval with a production sender.
- **Phone call fallback is a stub.** It writes an outbox row with status `STUB`.
