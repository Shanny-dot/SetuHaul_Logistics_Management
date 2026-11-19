"""Local WhatsApp simulator: the fallback when the Twilio sandbox is unavailable, and the dev tool.

It behaves like Twilio towards the app, and never imports the engine:
  * driver messages  -> POST {APP_URL}/webhook/twilio, form-encoded, signed with X-Twilio-Signature
  * app's replies    <- the app (CHANNEL=sim) POSTs them to /api/deliver here
  * delivery ticks   -> signed POSTs to {APP_URL}/webhook/twilio/status (sent, delivered, read; or undelivered)
  * coordinator panel -> the app's console API (/auth/dev-login, /console/holds, /console/holds/{id}/decide)

Run (two terminals, from the prototype folder):
  CHANNEL=sim CLOCK_MODE=snapshot SNAPSHOT_ANCHOR=2026-08-04T09:15:00+05:30 SETUHAUL_DEV=1 \\
      TWILIO_AUTH_TOKEN=sim-token .venv/bin/uvicorn app:app --port 8000
  TWILIO_AUTH_TOKEN=sim-token .venv/bin/python -I scripts/sim_server.py        # then open http://127.0.0.1:8100

The page lists drivers from the app database opened READ-ONLY (SETUHAUL_DB, default out/app_world.db).
Standard library only.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
SIGN_BASE = (os.environ.get("TWILIO_WEBHOOK_BASE_URL") or APP).rstrip("/")   # must be the URL the app validates against
TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
SANDBOX_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
DB = os.environ.get("SETUHAUL_DB", os.path.join(HERE, "out", "app_world.db"))
PORT = int(os.environ.get("SIM_PORT", "8100"))

STATE = {"messages": {}, "last_payload": {}, "delay_s": 0, "fail_next": False, "coord_token": None, "log": []}
LOCK = threading.Lock()

SCENARIOS = [
    {"case": "C03", "phone": "+919000010013", "at": "2026-08-04T09:19:00+05:30", "text": "I am late by one hour.",
     "then": "Tap an option (or type 1). A replayed webhook (Resend) must not book twice."},
    {"case": "C06", "phone": "+919000010004", "at": "2026-08-04T09:31:00+05:30", "text": "I will be late by 45 minutes.",
     "then": "Answer शाम वाला, tap option 1, then Approve the hold in the coordinator panel."},
    {"case": "C07", "phone": "+919000010006", "at": "2026-08-04T09:34:00+05:30",
     "text": "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?",
     "then": "Send the same text again (a new message id): the duplicate is suppressed. Resend = same id: dropped by dedupe."},
    {"case": "C12", "phone": "+919000010011", "at": "2026-08-04T10:05:00+05:30",
     "text": "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya",
     "then": "Then ask 'slot kab milega?': no scheduling talk until a person clears the case."},
    {"case": "C14", "phone": "+919000010015", "at": "2026-08-04T16:24:00+05:30",
     "text": "Evening traffic. ETA 6:30. Can the reefer unload tonight?", "then": "No feasible slot: hand-off with a named person and a time."},
]


def sign(url, params):
    s = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(TOKEN.encode(), s.encode(), hashlib.sha1).digest()).decode()


def post_form(path, params):
    url = SIGN_BASE + path
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(APP + path, data=data, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                                  "X-Twilio-Signature": sign(url, params)})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def app_json(method, path, body=None, auth=True):
    h = {"Content-Type": "application/json"}
    if auth and STATE["coord_token"]:
        h["Authorization"] = "Bearer " + STATE["coord_token"]
    req = urllib.request.Request(APP + path, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except OSError as e:
        return 0, {"error": str(e)}


def add_msg(phone, m):
    with LOCK:
        STATE["messages"].setdefault(phone, []).append(m)


def drivers():
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rs = con.execute("SELECT driver_id, driver_name, phone FROM drivers ORDER BY driver_id").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [{"driver_id": d, "name": n, "phone": "+" + "".join(c for c in p if c.isdigit())} for d, n, p in rs]


def send_inbound(phone, text="", choice_id=None, choice_title=None, lat=None, lng=None, media=None):
    sid = "SM" + uuid.uuid4().hex
    p = {"MessageSid": sid, "SmsMessageSid": sid, "AccountSid": "ACsimulator", "From": "whatsapp:" + phone, "To": SANDBOX_FROM,
         "Body": choice_title or text or "", "NumMedia": "0", "ProfileName": "Sim", "WaId": phone.lstrip("+")}
    if choice_id:
        p.update(ButtonPayload=choice_id, ButtonText=choice_title or "")
    if lat is not None:
        p.update(Latitude=str(lat), Longitude=str(lng))
    if media:
        p.update(NumMedia="1", MediaContentType0=media, MediaUrl0="https://example.invalid/media")
    add_msg(phone, {"dir": "in", "sid": sid, "body": p["Body"] or (f"[{media}]" if media else "[location]" if lat is not None else ""),
                    "t": time.time(), "http": None})
    STATE["last_payload"][phone] = p
    _post_later(phone, p)
    return sid


def _post_later(phone, p):
    def go():
        if STATE["delay_s"]:
            time.sleep(STATE["delay_s"])
        code = post_form("/webhook/twilio", p)
        with LOCK:
            for m in STATE["messages"].get(phone, []):
                if m.get("sid") == p["MessageSid"] and m["dir"] == "in":
                    m["http"] = (m.get("http") or "") + (" " if m.get("http") else "") + str(code)
    threading.Thread(target=go, daemon=True).start()


def deliver(payload):
    """The app's outbound message. Returns a fake SID; status callbacks follow, signed like Twilio's."""
    to = payload["to"].split(":", 1)[-1]
    sid = "SM" + uuid.uuid4().hex
    fail = STATE["fail_next"]
    STATE["fail_next"] = False
    body = payload.get("body")
    if not body and payload.get("content_variables"):
        v = json.loads(payload["content_variables"])
        body = v.get("1", "") if payload.get("kind") != "template" else f"[template {payload.get('template_ref')}] " + " | ".join(v.values())
    add_msg(to, {"dir": "out", "sid": sid, "body": body or "", "options": payload.get("options") or [], "status": "queued",
                 "kind": payload.get("kind"), "content_sid": payload.get("content_sid"), "outbox_id": payload.get("outbox_id"),
                 "t": time.time()})

    def callbacks():
        seq = [("sent", None), ("undelivered", "63016")] if fail else [("sent", None), ("delivered", None), ("read", None)]
        for i, (st, code) in enumerate(seq):
            time.sleep(0.4 + 0.6 * i)
            p = {"MessageSid": sid, "MessageStatus": st, "To": payload["to"], "From": SANDBOX_FROM, "AccountSid": "ACsimulator"}
            if code:
                p.update(ErrorCode=code, ErrorMessage="Simulated: outside the allowed window")
            post_form("/webhook/twilio/status", p)
            with LOCK:
                for m in STATE["messages"].get(to, []):
                    if m.get("sid") == sid:
                        m["status"] = st + (f" ({code})" if code else "")
    threading.Thread(target=callbacks, daemon=True).start()
    return {"sid": sid, "status": "queued"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/":
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if self.path == "/api/state":
            with LOCK:
                return self._json(200, {"drivers": drivers(), "messages": STATE["messages"], "delay_s": STATE["delay_s"],
                                        "fail_next": STATE["fail_next"], "signed_in": bool(STATE["coord_token"]), "scenarios": SCENARIOS})
        if self.path.startswith("/api/coord/"):
            what = self.path.rsplit("/", 1)[-1]
            path = {"holds": "/console/holds", "handoffs": "/console/handoffs", "alarms": "/console/alarms", "outbox": "/console/outbox"}.get(what)
            if path:
                return self._json(*app_json("GET", path))
        if self.path == "/api/ready":
            return self._json(*app_json("GET", "/readyz", auth=False))
        self._json(404, {})

    def do_POST(self):
        b = self._body()
        if self.path == "/api/deliver":
            return self._json(200, deliver(b))
        if self.path == "/api/send":
            return self._json(200, {"sid": send_inbound(b["phone"], b.get("text", ""), b.get("choice_id"), b.get("choice_title"),
                                                        b.get("lat"), b.get("lng"), b.get("media"))})
        if self.path == "/api/resend":
            p = STATE["last_payload"].get(b["phone"])
            if not p:
                return self._json(404, {"error": "nothing sent yet"})
            add_msg(b["phone"], {"dir": "in", "sid": p["MessageSid"], "body": p["Body"] + "  (resent, same MessageSid)", "t": time.time()})
            _post_later(b["phone"], p)
            return self._json(200, {"sid": p["MessageSid"]})
        if self.path == "/api/settings":
            STATE["delay_s"] = float(b.get("delay_s", STATE["delay_s"]))
            STATE["fail_next"] = bool(b.get("fail_next", STATE["fail_next"]))
            return self._json(200, {"delay_s": STATE["delay_s"], "fail_next": STATE["fail_next"]})
        if self.path == "/api/clock":
            return self._json(*app_json("POST", "/dev/clock", {"as_of": b["as_of"]}, auth=False))
        if self.path == "/api/coord/login":
            code, r = app_json("POST", "/auth/dev-login", {"user_id": b.get("user_id", "neha.bansal@setuhaul.example")}, auth=False)
            if code == 200:
                STATE["coord_token"] = r["token"]
            return self._json(code, {"ok": code == 200, "role": (r or {}).get("role")})
        if self.path == "/api/coord/decide":
            return self._json(*app_json("POST", f"/console/holds/{b['hold_id']}/decide", {"approve": bool(b.get("approve")), "note": "simulator"}))
        if self.path == "/api/coord/ack":
            return self._json(*app_json("POST", f"/console/handoffs/{b['escalation_id']}/ack", {}))
        if self.path == "/api/coord/clear":
            return self._json(*app_json("POST", f"/console/safety/{b['escalation_id']}/clear", {}))
        self._json(404, {})


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SetuHaul WhatsApp simulator</title>
<style>
:root{--bg:#efeae2;--in:#fff;--out:#d9fdd3;--ink:#111b21;--muted:#667781;--accent:#008069;--bad:#c62828}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;color:var(--ink);background:#f0f2f5}
.wrap{display:grid;grid-template-columns:260px 1fr 340px;height:100vh}
@media(max-width:900px){.wrap{grid-template-columns:1fr;height:auto}}
aside,section{overflow:auto;padding:12px}aside{background:#fff;border-right:1px solid #ddd}
h2{font-size:13px;text-transform:uppercase;color:var(--muted);margin:16px 0 6px}
.drv{padding:8px;border-radius:6px;cursor:pointer}.drv.on{background:#e7f3ef}.drv small{color:var(--muted);display:block}
.chat{background:var(--bg);display:flex;flex-direction:column;padding:0}
.msgs{flex:1;overflow:auto;padding:16px;display:flex;flex-direction:column;gap:6px;min-height:300px}
.b{max-width:75%;padding:6px 9px;border-radius:8px;white-space:pre-wrap;box-shadow:0 1px .5px rgba(0,0,0,.13)}
.b.in{align-self:flex-end;background:var(--out)}.b.out{align-self:flex-start;background:var(--in)}
.meta{font-size:11px;color:var(--muted);text-align:right;margin-top:3px}.read{color:#53bdeb}.bad{color:var(--bad)}
.opts{display:flex;flex-direction:column;gap:4px;margin-top:6px}.opts button{background:#fff;border:1px solid #cfd8dc;color:var(--accent);border-radius:6px;padding:6px;cursor:pointer}
.bar{display:flex;gap:6px;padding:8px;background:#f0f2f5;flex-wrap:wrap}.bar input{flex:1;min-width:160px;padding:9px;border-radius:8px;border:1px solid #ccc}
button{font:inherit;cursor:pointer}.btn{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 10px}
.btn.s{background:#fff;color:var(--accent);border:1px solid var(--accent)}
.card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:8px;margin-bottom:6px;font-size:13px}
pre{white-space:pre-wrap;font-size:11px;max-height:200px;overflow:auto;background:#fafafa;padding:6px}
</style></head><body><div class="wrap">
<aside><h2>Drivers</h2><div id="drv"></div><h2>Scenarios</h2><div id="sc"></div>
<h2>Test switches</h2>
<label><input type="checkbox" id="delay"> Delay webhook 5 s</label><br>
<label><input type="checkbox" id="fail"> Next reply fails (63016)</label></aside>
<section class="chat"><div class="msgs" id="msgs"></div>
<div class="bar"><input id="text" placeholder="Type a message"><button class="btn" onclick="send()">Send</button>
<button class="btn s" onclick="resend()" title="Re-post the last webhook with the same MessageSid">Resend same</button>
<button class="btn s" onclick="media()">Voice note</button><button class="btn s" onclick="loc()">Location</button></div></section>
<aside><h2>Coordinator</h2><button class="btn" onclick="login()">Sign in as Neha (CON001)</button>
<div id="coord"></div><h2>Alarms</h2><div id="alarms"></div></aside></div>
<script>
let cur=null,S={};
const j=(u,b)=>fetch(u,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{}).then(r=>r.json());
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function tick(m){if(m.dir!=='out')return m.http?('webhook '+m.http):'';const s=m.status||'';
 if(s.startsWith('read'))return '<span class=read>✓✓ read</span>';if(s.startsWith('delivered'))return '✓✓ delivered';
 if(s.startsWith('sent'))return '✓ sent';if(s.startsWith('undeliv')||s.startsWith('fail'))return '<span class=bad>✗ '+esc(s)+'</span>';return '🕓 '+esc(s)}
async function refresh(){S=await j('/api/state');
 document.getElementById('drv').innerHTML=S.drivers.map(d=>`<div class="drv ${d.phone===cur?'on':''}" onclick="pick('${d.phone}')">${esc(d.name)}<small>${d.driver_id} · ${d.phone.slice(0,6)}…${d.phone.slice(-2)}</small></div>`).join('');
 document.getElementById('sc').innerHTML=S.scenarios.map((s,i)=>`<div class=card><b>${s.case}</b> ${esc(s.text)}<br><small>${esc(s.then)}</small><br><button class="btn s" onclick="play(${i})">Jump clock + fill</button></div>`).join('');
 const box=document.getElementById('msgs'),ms=(S.messages[cur]||[]);
 box.innerHTML=ms.map(m=>`<div class="b ${m.dir}">${esc(m.body)}${(m.options||[]).length?'<div class=opts>'+m.options.map(o=>`<button onclick='tap(${JSON.stringify(o.id)},${JSON.stringify(o.title)})'>${esc(o.title)}</button>`).join('')+'</div>':''}<div class=meta>${tick(m)}</div></div>`).join('');
 box.scrollTop=box.scrollHeight;
 document.getElementById('delay').checked=S.delay_s>0;document.getElementById('fail').checked=S.fail_next;
 if(S.signed_in){const h=await j('/api/coord/holds');document.getElementById('coord').innerHTML=(Array.isArray(h)&&h.length?h:[]).map(x=>`<div class=card><b>${x.hold_id}</b> ${x.shipment_id} · ${x.slot_id}<br>decide by ${(x.expires_at||'').slice(11,16)}<br><button class=btn onclick="decide('${x.hold_id}',true)">Approve</button> <button class="btn s" onclick="decide('${x.hold_id}',false)">Reject</button></div>`).join('')||'<p>No holds waiting.</p>';
  const a=await j('/api/coord/alarms');document.getElementById('alarms').innerHTML=(Array.isArray(a)?a:[]).slice(0,12).map(x=>`<div class=card><b>${esc(x.kind)}</b> ${esc(x.ref||'')}<pre>${esc(x.detail_json)}</pre></div>`).join('')}}
function pick(p){cur=p;refresh()}
async function send(t){t=t??document.getElementById('text').value;if(!cur||!t)return;document.getElementById('text').value='';await j('/api/send',{phone:cur,text:t});setTimeout(refresh,300)}
async function tap(id,title){await j('/api/send',{phone:cur,choice_id:id,choice_title:title});setTimeout(refresh,300)}
async function resend(){await j('/api/resend',{phone:cur});setTimeout(refresh,300)}
async function media(){await j('/api/send',{phone:cur,media:'audio/ogg'});setTimeout(refresh,300)}
async function loc(){await j('/api/send',{phone:cur,lat:26.91,lng:75.79});setTimeout(refresh,300)}
async function play(i){const s=S.scenarios[i];const r=await j('/api/clock',{as_of:s.at});if(r.detail)alert(r.detail);cur=s.phone;document.getElementById('text').value=s.text;refresh()}
async function login(){await j('/api/coord/login',{});refresh()}
async function decide(h,a){await j('/api/coord/decide',{hold_id:h,approve:a});setTimeout(refresh,500)}
document.getElementById('delay').onchange=e=>j('/api/settings',{delay_s:e.target.checked?5:0});
document.getElementById('fail').onchange=e=>j('/api/settings',{fail_next:e.target.checked});
document.getElementById('text').addEventListener('keydown',e=>{if(e.key==='Enter')send()});
setInterval(refresh,1500);refresh();
</script></body></html>"""


def main():
    if not TOKEN:
        sys.exit("Set TWILIO_AUTH_TOKEN (any value; the app must use the same one) so the simulator can sign webhooks.")
    print(f"Simulator on http://127.0.0.1:{PORT}  ->  app {APP} (signing for {SIGN_BASE})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
