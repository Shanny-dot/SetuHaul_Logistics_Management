"""Smoke checks against a running stack (Caddy in front of the app). Standard library only.

  python3 checks.py --public https://localhost:8443 --local http://127.0.0.1:8000 --token <test token> --phone +919800000013

1. public /healthz through Caddy (HTTPS)                     -> 200
2. local /readyz                                            -> ok:true
3. private paths through Caddy (/readyz, /auth/dev-login, /console/holds, /dev/clock) -> 404
4. a Twilio-shaped webhook, signed locally for the PUBLIC URL, through Caddy -> 200, then the trace shows the turn ran
5. the same webhook with a wrong signature                   -> 403
"""
import argparse, base64, hashlib, hmac, json, ssl, sys, time, urllib.error, urllib.parse, urllib.request, uuid

ap = argparse.ArgumentParser()
ap.add_argument("--public", required=True)
ap.add_argument("--local", required=True)
ap.add_argument("--token", required=True)
ap.add_argument("--phone", required=True)
a = ap.parse_args()
CTX = ssl._create_unverified_context()          # local test CA only
fails = []


def call(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX if url.startswith("https") else None) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def sign(url, params, token):
    s = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()


code, body = call(a.public + "/healthz")
check("public /healthz through Caddy over HTTPS", code == 200 and '"ok"' in body, f"{code} {body[:80]}")
code, body = call(a.local + "/readyz")
check("local /readyz ok", code == 200 and '"ok":true' in body, f"{code} {body[:120]}")
for path, method in (("/readyz", "GET"), ("/auth/dev-login", "POST"), ("/console/holds", "GET"), ("/dev/clock", "POST")):
    code, _ = call(a.public + path, data=b"{}" if method == "POST" else None, method=method,
                   headers={"content-type": "application/json"})
    check(f"private {path} is 404 from outside", code == 404, str(code))

url = a.public + "/webhook/twilio"
sid = "SM" + uuid.uuid4().hex
params = {"MessageSid": sid, "SmsMessageSid": sid, "AccountSid": "AC" + "0" * 32, "From": "whatsapp:" + a.phone,
          "To": "whatsapp:+14155238886", "Body": "I am late by one hour.", "NumMedia": "0"}
data = urllib.parse.urlencode(params).encode()
form = {"content-type": "application/x-www-form-urlencoded"}
code, body = call(url, data, dict(form, **{"X-Twilio-Signature": sign(url, params, a.token)}))
check("signed webhook through Caddy -> 200 + empty TwiML", code == 200 and "<Response>" in body, f"{code} {body[:80]}")
trace = {}
for _ in range(30):
    _, t = call(f"{a.local}/dev/trace/{sid}")
    trace = json.loads(t) if t.startswith("{") else {}
    if trace.get("state"):
        break
    time.sleep(0.5)
check("the turn ran (trace state OPTIONS_SHOWN)", trace.get("state") == "OPTIONS_SHOWN", json.dumps(trace)[:200])
params2 = dict(params, MessageSid="SM" + uuid.uuid4().hex)
code, _ = call(url, urllib.parse.urlencode(params2).encode(), dict(form, **{"X-Twilio-Signature": "bad"}))
check("wrong signature -> 403", code == 403, str(code))
print("RESULT:", "PASS" if not fails else f"FAIL ({len(fails)})")
sys.exit(1 if fails else 0)
