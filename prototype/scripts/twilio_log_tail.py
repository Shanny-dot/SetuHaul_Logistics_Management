"""Recent Twilio message logs and Debugger alerts, for "nothing happens" / 403 / 63016 style failures.

    set -a; . ../.env; set +a
    .venv/bin/python -I scripts/twilio_log_tail.py              # last 20 messages + error alerts of the last 24 h
    .venv/bin/python -I scripts/twilio_log_tail.py --n 50 --hours 2

Read-only: Messages list (sid, direction, status, error_code) and Monitor Alerts
(GET https://monitor.twilio.com/v1/Alerts; alerts older than 30 days are not available).
Phone numbers are masked; message bodies are not printed.

Console equivalent: Monitor > Logs > Messaging (filter by the sandbox number), and Monitor > Logs > Errors
(the Debugger). A webhook that returned 403/404/timeout shows there as 11200 with the request URL.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul.tracing import mask      # noqa: E402

# Titles quoted from twilio.com/docs/api/errors/<code> (checked 2026-11-19), with what to do here.
HINTS = {
    "63015": "Sandbox can only send to numbers that joined it -> the handset must send 'join <code>' again (joins last 3 days)",
    "63016": "Outside the 24 h window -> free-form not allowed; needs an approved template (expected for proactive sends)",
    "63007": "No channel for the From address -> TWILIO_WHATSAPP_FROM must be whatsapp:+14155238886 on the same account",
    "11200": "HTTP retrieval failure: your webhook answered non-2xx or timed out -> 403 = signature/URL mismatch (run preflight.py), "
             "404 = wrong path, timeout = app or ngrok down",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()
    sid, tok = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and tok):
        sys.exit("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be in the environment")
    from twilio.rest import Client
    c = Client(sid, tok)
    frm = os.environ.get("TWILIO_WHATSAPP_FROM")
    print(f"Messages (latest {a.n}{', sandbox sender' if frm else ''}):")
    msgs = []
    if frm:
        msgs = list(c.messages.list(from_=frm, limit=a.n)) + list(c.messages.list(to=frm, limit=a.n))
    else:
        msgs = list(c.messages.list(limit=a.n))
    msgs.sort(key=lambda m: m.date_created or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for m in msgs[:a.n]:
        code = str(m.error_code) if m.error_code else ""
        print(f"  {m.date_created:%Y-%m-%d %H:%M:%S}  {m.sid}  {m.direction:15s} {mask(m.from_ or ''):>24s} -> {mask(m.to or ''):24s} "
              f"{m.status:11s} {code}")
        if code in HINTS:
            print(f"      {code}: {HINTS[code]}")
    since = datetime.now(timezone.utc) - timedelta(hours=a.hours)
    print(f"\nDebugger alerts (errors since {since:%Y-%m-%d %H:%M} UTC):")
    try:
        alerts = c.monitor.v1.alerts.list(start_date=since, log_level="error", limit=a.n)
    except Exception as e:
        print(f"  could not read alerts ({type(e).__name__})")
        alerts = []
    for al in alerts:
        code = str(al.error_code)
        print(f"  {al.date_created:%Y-%m-%d %H:%M:%S}  {code}  {mask((al.request_url or '').split('?')[0])}  {mask(al.alert_text or '')[:160]}")
        if code in HINTS:
            print(f"      {HINTS[code]}")
    if not alerts:
        print("  none")


if __name__ == "__main__":
    main()
