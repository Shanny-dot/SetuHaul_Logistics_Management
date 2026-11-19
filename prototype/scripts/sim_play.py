"""Play the five acceptance scenarios through the simulator (scripts/sim_server.py) and print evidence.

Run with the app (CHANNEL=sim, CLOCK_MODE=snapshot, SETUHAUL_DEV=1) and the simulator already up:
    .venv/bin/python -I scripts/sim_play.py
Talks only HTTP: to the simulator (which signs Twilio-shaped webhooks) and to the app's dev/console API.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

SIM = os.environ.get("SIM_URL", "http://127.0.0.1:8100")
APP = os.environ.get("APP_URL", "http://127.0.0.1:8000")


def call(base, path, body=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=h,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"null")


def jump(as_of):
    """Move the snapshot clock forward; if it is already later, carry on (it never goes back)."""
    try:
        return call(APP, "/dev/clock", {"as_of": as_of})
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        print("  (clock already past", as_of[11:16], "- continuing at the current time)")


def wait_ready():
    for _ in range(60):
        try:
            call(APP, "/healthz")
            call(SIM, "/api/state")
            return
        except OSError:
            time.sleep(0.5)
    sys.exit("app or simulator not reachable")


def msgs(phone):
    return call(SIM, "/api/state")["messages"].get(phone, [])


def settle(phone, n_out, timeout=20):
    """Wait until the phone has at least n_out outbound messages and every one has a final tick."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        out = [m for m in msgs(phone) if m["dir"] == "out"]
        if len(out) >= n_out and all((m.get("status") or "").split(" ")[0] in ("read", "undelivered", "failed") for m in out):
            return out
        time.sleep(0.4)
    return [m for m in msgs(phone) if m["dir"] == "out"]


def say(phone, text=None, **kw):
    before = len([m for m in msgs(phone) if m["dir"] == "out"])
    sid = call(SIM, "/api/send", dict(phone=phone, text=text or "", **kw))["sid"]
    return sid, before


def show(tag, out):
    for m in out:
        opts = " | ".join(o["title"] for o in m.get("options") or [])
        print(f"  [{tag}] <- {m['body'][:150].replace(chr(10), ' / ')}" + (f"  {{buttons: {opts}}}" if opts else "") + f"  [{m.get('status')}]")


def trace(sid):
    return call(APP, f"/dev/trace/{sid}")


def main():
    wait_ready()
    print("ready:", json.dumps(call(APP, "/readyz")["checks"]))
    tok = call(APP, "/auth/dev-login", {"user_id": "neha.bansal@setuhaul.example"})["token"]
    ev = {}

    # ---- C03: late by one hour -> options -> pick; replayed webhook books once; stale tap does not book
    P = "+919000010013"
    jump("2026-08-04T09:19:00+05:30")
    sid, b = say(P, "I am late by one hour.")
    out = settle(P, b + 2)
    show("C03", out[b:])
    first_opts = out[-1]["options"]
    t = trace(sid)
    print("  trace", t["trace_id"], t["state"], t["clock_mode"], t["as_of"])
    sid, b = say(P, "Sorry, reaching 11:40")                             # supersedes the first list
    out = settle(P, b + 1)
    show("C03", out[b:])
    sid, b = say(P, choice_id=first_opts[0]["id"], choice_title=first_opts[0]["title"])   # stale tap
    out = settle(P, b + 1)
    show("C03 stale tap", out[b:])
    ev["stale_tap"] = trace(sid)["status"]
    live = out[-1]["options"]
    sid, b = say(P, choice_id=live[0]["id"], choice_title=live[0]["title"])
    out = settle(P, b + 1)
    show("C03 pick", out[b:])
    ev["c03_pick"] = trace(sid)["state"]
    call(SIM, "/api/resend", {"phone": P})                               # replayed webhook, same MessageSid
    time.sleep(2)
    ob = call(APP, "/console/outbox", token=tok)
    ev["c03_replay_extra_messages"] = len([m for m in msgs(P) if m["dir"] == "out"]) - len(out)

    # ---- C06: Hindi: which trip -> options -> approval -> follow-up
    P = "+919000010004"
    jump("2026-08-04T09:31:00+05:30")
    sid, b = say(P, "I will be late by 45 minutes.")
    show("C06", settle(P, b + 2)[b:])
    sid, b = say(P, "शाम वाला")
    out = settle(P, b + 1)
    show("C06", out[b:])
    o = out[-1]["options"][0]
    sid, b = say(P, choice_id=o["id"], choice_title=o["title"])
    show("C06", settle(P, b + 1)[b:])
    holds = call(APP, "/console/holds", token=tok)
    print("  holds:", [h["hold_id"] for h in holds])
    if holds:
        r = call(APP, f"/console/holds/{holds[0]['hold_id']}/decide", {"approve": True}, token=tok)
        print("  decision:", r["decision"])
        show("C06 follow-up", settle(P, b + 2)[b + 1:])
        ev["c06"] = r["decision"]

    # ---- C07: duplicate retry (same text, new message id)
    P = "+919000010006"
    jump("2026-08-04T09:34:00+05:30")
    t1 = "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"
    sid, b = say(P, t1)
    show("C07", settle(P, b + 2)[b:])
    jump("2026-08-04T09:35:00+05:30")
    sid2, b = say(P, t1)
    time.sleep(2)
    t = trace(sid2)
    print("  retry trace:", t["status"], t.get("tools"))
    ev["c07_retry"] = t["status"]

    # ---- C12: Hinglish safety, then scheduling talk is held
    P = "+919000010011"
    jump("2026-08-04T10:05:00+05:30")
    sid, b = say(P, "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya")
    show("C12", settle(P, b + 2)[b:])
    sid, b = say(P, "slot kab milega?")
    show("C12", settle(P, b + 1)[b:])
    ev["c12_after"] = trace(sid)["status"]

    # ---- C14: no feasible slot
    P = "+919000010015"
    jump("2026-08-04T16:24:00+05:30")
    sid, b = say(P, "Evening traffic. ETA 6:30. Can the reefer unload tonight?")
    show("C14", settle(P, b + 2)[b:])
    ev["c14"] = trace(sid)["state"]

    # ---- failed send shows its ErrorCode; proactive outside the window is template-or-blocked
    P = "+919000010013"
    call(SIM, "/api/settings", {"fail_next": True})
    sid, b = say(P, "Is my slot still active?")
    settle(P, b + 1)
    time.sleep(2)
    r = call(APP, "/console/proactive/SHP1001", {}, token=tok)
    time.sleep(2)
    ob = call(APP, "/console/outbox", token=tok)
    ev["undelivered"] = [(m["msg_id"], m["status"], m["error_code"]) for m in ob if m["status"] == "UNDELIVERED"][:2]
    ev["proactive"] = [(m["msg_id"], m["status"], m["error_code"]) for m in ob if m["proactive"]][:3]
    al = call(APP, "/console/alarms", token=tok)
    ev["alarms"] = sorted({a["kind"] for a in al})
    print("\nEVIDENCE", json.dumps(ev, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
