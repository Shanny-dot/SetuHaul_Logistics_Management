"""Create the Content API templates used for interactive options, and print their SIDs as env lines.

    .venv/bin/python -I scripts/create_twilio_content.py            # dry run: prints the requests, sends nothing
    .venv/bin/python -I scripts/create_twilio_content.py --create   # POST https://content.twilio.com/v1/Content

Creates: QR1, QR2, QR3 (twilio/quick-reply with 1-3 buttons) and LIST4 ... LIST10 (twilio/list-picker).
Variable layout (channels/twilio_wa.py build_params relies on it):
    {{1}} body; then per option: title, id (and for lists, description).
These are session messages (sent only inside the 24 h window), so they need no WhatsApp approval;
list-pickers cannot be approved at all (Twilio list-picker docs).

VERIFY before relying on it (not confirmed in Twilio's docs on 2026-11-19):
  * that variables are accepted in button/item ids and titles, and in a body that is only "{{1}}";
  * that the WhatsApp Sandbox renders Content API quick-replies / list-pickers.
If either fails, keep TWILIO_INTERACTIVE=false: options go as numbered text with the same offer binding.
Needs TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in the environment (never on the command line).
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import config as C          # noqa: E402

URL = "https://content.twilio.com/v1/Content"


def quick_reply(n):
    v, actions = {"1": "Pick a slot"}, []
    for i in range(n):
        t, d = 2 + 2 * i, 3 + 2 * i
        v[str(t)], v[str(d)] = f"Option {i + 1}", f"opt:00000000:{i + 1}"
        actions.append({"title": "{{%d}}" % t, "id": "{{%d}}" % d})
    return {"friendly_name": f"setuhaul_qr{n}", "language": "en", "variables": v,
            "types": {"twilio/quick-reply": {"body": "{{1}}", "actions": actions}, "twilio/text": {"body": "{{1}}"}}}


def list_picker(n):
    v, items = {"1": "Pick a slot"}, []
    for i in range(n):
        t, d, x = 2 + 3 * i, 3 + 3 * i, 4 + 3 * i
        v[str(t)], v[str(d)], v[str(x)] = f"Option {i + 1}", f"opt:00000000:{i + 1}", "-"
        items.append({"item": "{{%d}}" % t, "id": "{{%d}}" % d, "description": "{{%d}}" % x})
    return {"friendly_name": f"setuhaul_list{n}", "language": "en", "variables": v,
            "types": {"twilio/list-picker": {"body": "{{1}}", "button": "Choose", "items": items}, "twilio/text": {"body": "{{1}}"}}}


def main():
    create = "--create" in sys.argv
    specs = {f"QR{n}": quick_reply(n) for n in range(1, C.TWILIO_QR_MAX_BUTTONS + 1)}
    specs.update({f"LIST{n}": list_picker(n) for n in range(C.TWILIO_QR_MAX_BUTTONS + 1, C.TWILIO_LIST_MAX_ITEMS + 1)})
    if not create:
        for k, s in specs.items():
            print(f"# {k}\n{json.dumps(s, ensure_ascii=False)}")
        print("\n(dry run: nothing sent; add --create to create them)")
        return
    sid, tok = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and tok):
        sys.exit("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    for k, s in specs.items():
        req = urllib.request.Request(URL, data=json.dumps(s).encode(), headers={"Content-Type": "application/json",
                                                                               "Authorization": "Basic " + auth})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"TWILIO_CONTENT_{k}={json.loads(r.read())['sid']}")
        except urllib.error.HTTPError as e:
            print(f"# {k} failed: HTTP {e.code} {e.read()[:300]!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
