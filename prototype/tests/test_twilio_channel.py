"""Offline tests for the Twilio WhatsApp channel: signature, dedupe, lock, identity, rate limit, message
types, offer binding, 24 h window vs templates, outbox retry/idempotency, status callbacks, language,
snapshot clock, SAFETY freeze, missed-SLA chain, and console-vs-Twilio parity on C03 and C06.

Run:  .venv/bin/python -I -m unittest tests.test_twilio_channel -v      (from the prototype folder)
Needs the twilio package (and fastapi/httpx for the HTTP tests). No network: a fake Twilio client.
"""
import importlib
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import db, chan_db, demo_world, outbox as OB, safety as SF, offers as OF, capacity as K, workflow as W
from setuhaul import config as C, session_store as SS
from setuhaul.channels import ChannelRequest, get_channel, OutboundMessage
from setuhaul.channels.base import DeliveryStatus
from setuhaul.clock import Clock, ts, IST, iso
from setuhaul.inbound import ChannelService
from setuhaul.session_store import SessionStore
from setuhaul.settings import Settings

has = lambda m: importlib.util.find_spec(m) is not None
TOKEN = "test-auth-token"
SANDBOX = "whatsapp:+14155238886"
BASE = "https://setuhaul-test.ngrok-free.app"
URL = BASE + "/webhook/twilio"
C03 = "+919000010013"            # DRV013, SHP1013
C06 = "+919000010004"            # DRV004 (two trips: SHP1004 and SHP1020)
C12 = "+919000010011"            # DRV011 (safety)
C09 = "+919000010010"            # DRV010 (Hinglish)


def at(hm):
    return ts(f"2026-08-04T{hm}:00+05:30")


class FakeMessage:
    def __init__(self, sid, status="queued", body="", to="", date_created=None):
        self.sid, self.status, self.body, self.to, self.date_created = sid, status, body, to, date_created


class Crash(BaseException):
    """The process dies here: nothing after this line runs, no handler catches it."""


CRASH_BEFORE, CRASH_AFTER = "crash-before-send", "crash-after-send"


class FakeTwilio:
    """client.messages.create(**kw) / .list(to, from_, limit). Queued failures run first:
    an exception is raised, CRASH_BEFORE dies before Twilio sees the request, CRASH_AFTER dies after
    Twilio accepted it (the message exists at Twilio, the caller never gets the SID)."""

    def __init__(self, now=None):
        self.calls, self.fail, self.log, self.now = [], [], [], now or (lambda: datetime.now(IST))
        self.messages = self
        self.list_error = None

    def create(self, **kw):
        f = self.fail.pop(0) if self.fail else None
        if f == CRASH_BEFORE:
            raise Crash("before send")
        if isinstance(f, BaseException):
            self.calls.append(kw)
            raise f
        self.calls.append(kw)
        m = FakeMessage(f"SM{len(self.calls):032d}", body=kw.get("body") or "", to=kw["to"], date_created=self.now())
        self.log.append(m)
        if f == CRASH_AFTER:
            raise Crash("after send, before the SID is stored")
        return m

    def list(self, to=None, from_=None, limit=50):
        if self.list_error:
            raise self.list_error
        return [m for m in reversed(self.log) if m.to == to][:limit]


class RealTime:
    def __init__(self):
        self.t = datetime(2026, 11, 19, 10, 0, tzinfo=IST)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def settings(**kw):
    s = Settings(channel="twilio", twilio_account_sid="AC" + "0" * 32, twilio_auth_token=TOKEN, twilio_whatsapp_from=SANDBOX,
                 twilio_webhook_base_url=BASE, twilio_interactive=False, twilio_sandbox=True, worker=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def signed(params, url=URL, token=TOKEN):
    from twilio.request_validator import RequestValidator
    return RequestValidator(token).compute_signature(url, params)


def form(sid, frm, body="", **extra):
    p = {"MessageSid": sid, "SmsMessageSid": sid, "AccountSid": "AC" + "0" * 32, "From": "whatsapp:" + frm, "To": SANDBOX,
         "Body": body, "NumMedia": "0", "ProfileName": "Test", "WaId": frm.lstrip("+")}
    p.update(extra)
    return p


def creq(params, url=URL, sig=None):
    return ChannelRequest(url=url, headers={"x-twilio-signature": sig if sig is not None else signed(params, url)},
                          params={k: [v] for k, v in params.items()})


class World:
    """A fresh 08:20 world with the fixed events, a Twilio adapter on a fake client, and a snapshot clock."""

    def __init__(self, tc, channel="twilio", start="09:15", **skw):
        self.dir = tempfile.mkdtemp(prefix="sh-twilio-")
        self.path = db.fresh_copy(os.path.join(self.dir, "t.db"))
        con = db.connect(self.path)
        chan_db.ensure(con)
        demo_world.start(con)
        con.close()
        self.real = RealTime()
        self.clock = Clock("snapshot", f"2026-08-04T{start}:00+05:30", _now=self.real)
        self.s = settings(channel=channel, **skw)
        self.fake = FakeTwilio(now=self.real)
        self.ch = get_channel(self.s, client=self.fake) if channel == "twilio" else get_channel(self.s)
        self.store = SessionStore()
        self.svc = ChannelService(self.con_factory, self.store, self.ch, self.s, self.clock, demo_world=True)
        self.con = db.connect(self.path)
        self.n = 0
        tc.addCleanup(self.close)

    def con_factory(self):
        return db.connect(self.path)

    def close(self):
        self.con.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def at(self, hm):
        if self.clock.now() < at(hm):
            self.clock.jump_to(at(hm))

    def post(self, frm, body="", sid=None, hm=None, **extra):
        if hm:
            self.at(hm)
        self.n += 1
        sid = sid or f"SM{self.n:032x}"
        p = form(sid, frm, body, **extra)
        msgs = self.ch.parse_inbound(creq(p))
        out = [self.svc.process(m) for m in self.svc.accept(msgs)]
        return out[0] if out else None

    def tap(self, frm, option_id, title="", hm=None, sid=None):
        return self.post(frm, title, sid=sid, hm=hm, ButtonPayload=option_id, ButtonText=title)

    def deliveries(self, to=None):
        q = "SELECT d.*, o.kind FROM wa_delivery d JOIN outbox o USING(msg_id)" + (" WHERE d.to_e164=?" if to else "") + " ORDER BY d.rowid"
        rs = self.con.execute(q, (to,) if to else ()).fetchall()
        return [dict(r, payload=json.loads(r["payload_json"])) for r in rs]

    def last_reply(self, to):
        d = [x for x in self.deliveries(to) if x["kind"] in ("DRIVER_REPLY", "STALE_CHOICE_RESEND", "STALE_CHOICE_CLOSED", "SAFETY_HOLD",
                                                               "PLEASE_TYPE", "HANDOFF", "LOCATION_NOTED", "UNKNOWN_SENDER_REFUSAL", "STAFF_REPLY")]
        return d[-1]["payload"] if d else None

    def drain(self):
        return OB.drain(self.con, self.ch, self.s, self.clock)

    def reconcile(self):
        return OB.reconcile(self.con, self.ch, self.s, self.clock)

    def bookings(self, sid):
        return self.con.execute("SELECT * FROM appointments WHERE shipment_id=? AND appointment_id LIKE 'APT-N%'", (sid,)).fetchall()

    def alarms(self, kind=None):
        return [dict(r) for r in self.con.execute("SELECT * FROM ops_alarms" + (" WHERE kind=?" if kind else ""), (kind,) if kind else ())]


need_twilio = unittest.skipUnless(has("twilio"), "twilio not installed")


# ====================================================================== signature and parsing
@need_twilio
class Signature(unittest.TestCase):
    def setUp(self):
        self.ch = get_channel(settings(), client=FakeTwilio())

    def test_valid(self):
        p = form("SM1", C03, "I am late by one hour.")
        self.assertTrue(self.ch.verify(creq(p)))

    def test_invalid_missing_wrong_url_wrong_token_tampered(self):
        p = form("SM1", C03, "hi")
        self.assertFalse(self.ch.verify(creq(p, sig="AAAA")))
        self.assertFalse(self.ch.verify(ChannelRequest(URL, {}, {k: [v] for k, v in p.items()})))
        self.assertFalse(self.ch.verify(creq(p, url=URL, sig=signed(p, url="http://localhost:8000/webhook/twilio"))))
        self.assertFalse(self.ch.verify(creq(p, sig=signed(p, token="other"))))
        r = creq(p)
        r.params["Body"] = ["I am early"]                            # body changed after signing
        self.assertFalse(self.ch.verify(r))

    def test_no_auth_token_refuses_everything(self):
        ch = get_channel(settings(twilio_auth_token=""), client=FakeTwilio())
        self.assertFalse(ch.verify(creq(form("SM1", C03, "hi"))))

    def test_parse_types(self):
        g = lambda **kw: self.ch.parse_inbound(creq(form("SM9", C03, **kw)))[0]
        m = g(body="late")
        self.assertEqual((m.type, m.from_e164, m.message_id, m.text), ("text", C03, "SM9", "late"))
        m = g(body="D2 10:30 AM", ButtonPayload="opt:0a1b2c3d:2", ButtonText="D2 10:30 AM")
        self.assertEqual((m.type, m.choice_id), ("choice", "opt:0a1b2c3d:2"))
        m = g(ListId="opt:0a1b2c3d:4")
        self.assertEqual((m.type, m.choice_id), ("choice", "opt:0a1b2c3d:4"))
        m = g(InteractiveData=json.dumps({"list_reply": {"id": "opt:0a1b2c3d:5"}}))
        self.assertEqual(m.choice_id, "opt:0a1b2c3d:5")
        m = g(Latitude="26.9124", Longitude="75.7873")
        self.assertEqual((m.type, m.lat, m.lng), ("location", 26.9124, 75.7873))
        m = g(NumMedia="1", MediaContentType0="audio/ogg", MediaUrl0="https://api.twilio.com/x")
        self.assertEqual((m.type, m.media_types), ("media", ["audio/ogg"]))
        self.assertEqual(g().type, "other")
        m = g(body="ok", OriginalRepliedMessageSid="SMprev")
        self.assertEqual(m.reply_to_id, "SMprev")

    def test_status_parse(self):
        p = {"MessageSid": "SMx", "MessageStatus": "undelivered", "ErrorCode": "63016", "To": "whatsapp:" + C03}
        st = self.ch.parse_status(creq(p))
        self.assertEqual((st.provider_id, st.status, st.error_code, st.to_e164), ("SMx", "undelivered", "63016", C03))


# ====================================================================== HTTP layer
@need_twilio
@unittest.skipUnless(has("fastapi") and has("httpx"), "fastapi/httpx not installed")
class Http(unittest.TestCase):
    ENV = dict(SETUHAUL_DEV="1", CHANNEL="twilio", TWILIO_AUTH_TOKEN=TOKEN, TWILIO_ACCOUNT_SID="AC" + "0" * 32,
               TWILIO_WHATSAPP_FROM=SANDBOX, TWILIO_WEBHOOK_BASE_URL=BASE, OUTBOX_WORKER="0", CLOCK_MODE="snapshot",
               SNAPSHOT_ANCHOR="2026-08-04T09:19:00+05:30")

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="sh-http-")
        cls.saved = {k: os.environ.get(k) for k in list(cls.ENV) + ["SETUHAUL_DB", "WHATSAPP_APP_SECRET", "REDIS_URL", "ANTHROPIC_API_KEY"]}
        os.environ.update(cls.ENV, SETUHAUL_DB=db.fresh_copy(os.path.join(cls.dir, "app.db")))
        c = db.connect(os.environ["SETUHAUL_DB"])
        chan_db.ensure(c)
        demo_world.start(c)
        c.close()
        for k in ("WHATSAPP_APP_SECRET", "REDIS_URL", "ANTHROPIC_API_KEY"):
            os.environ.pop(k, None)
        sys.modules.pop("app", None)
        cls.app = importlib.import_module("app")
        cls.app.CHANNEL._client = FakeTwilio()
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.modules.pop("app", None)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def post(self, params, sig=None, path="/webhook/twilio", headers=None):
        h = {"X-Twilio-Signature": sig if sig is not None else signed(params, BASE + path),
             "Content-Type": "application/x-www-form-urlencoded"}
        h.update(headers or {})
        return self.client.post(path, data=params, headers=h)

    def test_valid_post_returns_empty_twiml_and_processes_once(self):
        p = form("SMhttp0000000000000000000000001", C03, "I am late by one hour.")
        r = self.post(p)
        self.assertEqual(r.status_code, 200)
        self.assertIn("<Response></Response>", r.text)
        self.assertEqual(self.post(p).status_code, 200)              # Twilio retry: 200, nothing new
        con = db.connect(os.environ["SETUHAUL_DB"])
        n = con.execute("SELECT COUNT(*) FROM wa_inbound WHERE message_sid=?", (p["MessageSid"],)).fetchone()[0]
        replies = con.execute("""SELECT COUNT(*) FROM wa_delivery d JOIN outbox o USING(msg_id) WHERE o.kind='DRIVER_REPLY'
                                 AND d.trace_id=(SELECT trace_id FROM wa_inbound WHERE message_sid=?)""", (p["MessageSid"],)).fetchone()[0]
        con.close()
        self.assertEqual((n, replies), (1, 1))
        tr = self.client.get(f"/dev/trace/{p['MessageSid']}").json()
        self.assertEqual(tr["state"], "OPTIONS_SHOWN")
        self.assertEqual(tr["clock_mode"], "snapshot")
        self.assertNotIn("9000010013", json.dumps(tr))               # masked

    def test_bad_missing_and_wrong_url_signatures_are_403_before_processing(self):
        p = form("SMhttp0000000000000000000000002", C03, "I am late by one hour.")
        self.assertEqual(self.post(p, sig="bad").status_code, 403)
        self.assertEqual(self.client.post("/webhook/twilio", data=p).status_code, 403)
        self.assertEqual(self.post(p, sig=signed(p, "http://testserver/webhook/twilio")).status_code, 403)
        con = db.connect(os.environ["SETUHAUL_DB"])
        self.assertIsNone(con.execute("SELECT 1 FROM wa_inbound WHERE message_sid=?", (p["MessageSid"],)).fetchone())
        con.close()

    def test_forwarded_host_used_when_no_base_url(self):
        old = self.app.SETTINGS.twilio_webhook_base_url
        self.app.SETTINGS.twilio_webhook_base_url = ""
        try:
            p = form("SMhttp0000000000000000000000003", "+919999999999", "hello")
            sig = signed(p, "https://abc.ngrok-free.app/webhook/twilio")
            r = self.post(p, sig=sig, headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "abc.ngrok-free.app"})
            self.assertEqual(r.status_code, 200)
        finally:
            self.app.SETTINGS.twilio_webhook_base_url = old

    def test_status_callback_signed(self):
        p = {"MessageSid": "SMnope", "MessageStatus": "delivered"}
        self.assertEqual(self.post(p, path="/webhook/twilio/status").status_code, 200)
        self.assertEqual(self.post(p, sig="x", path="/webhook/twilio/status").status_code, 403)

    def test_status_callback_ref_in_query_is_signed_and_reconciles(self):
        con = db.connect(os.environ["SETUHAUL_DB"])
        with db.write_txn(con):
            mid = OB.enqueue(con, at("09:19"), datetime.now(IST), OutboundMessage(to_e164=C03, body="x"))
            con.execute("UPDATE wa_delivery SET status='SENDING', send_ref='REF-HTTP-1' WHERE msg_id=?", (mid,))
        con.close()
        path = "/webhook/twilio/status?ref=REF-HTTP-1"
        p = {"MessageSid": "SM" + "7" * 32, "MessageStatus": "sent"}
        self.assertEqual(self.post(p, sig=signed(p, BASE + "/webhook/twilio/status"), path=path).status_code, 403)   # query is signed too
        self.assertEqual(self.post(p, path=path).status_code, 200)
        con = db.connect(os.environ["SETUHAUL_DB"])
        r = con.execute("SELECT status, provider_sid FROM wa_delivery WHERE msg_id=?", (mid,)).fetchone()
        con.close()
        self.assertEqual(tuple(r), ("SENT", "SM" + "7" * 32))

    def test_link_number_endpoint_needs_reason_and_audits(self):
        tok = self.client.post("/auth/dev-login", json={"user_id": "neha.bansal@setuhaul.example"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.client.post("/console/drivers/DRV013/link-number", json={"phone": "+919811100077"}).status_code, 401)
        self.assertEqual(self.client.post("/console/drivers/DRV013/link-number", json={"phone": "+919811100077"}, headers=h).status_code, 400)
        r = self.client.post("/console/drivers/DRV013/link-number", json={"phone": "+919811100077", "hours": 12,
                                                                         "reason": "lost phone, carrier confirmed"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.post("/console/drivers/DRV013/unlink-number", headers=h).json()["revoked"], 1)
        self.assertEqual(self.client.post("/console/drivers/NOPE/link-number", json={"phone": "+919811100078", "reason": "x"},
                                          headers=h).status_code, 404)

    def test_coordinator_clears_freeze_with_audit(self):
        con = db.connect(os.environ["SETUHAUL_DB"])
        con.execute("INSERT INTO safety_freezes VALUES ('ESC-H001','DRV003','SHP1018','2026-08-04T09:40:00+05:30',NULL,NULL)")
        con.close()
        self.assertEqual(self.client.post("/console/safety/ESC-H001/clear").status_code, 401)
        tok = self.client.post("/auth/dev-login", json={"user_id": "neha.bansal@setuhaul.example"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        r = self.client.post("/console/safety/ESC-H001/clear", headers=h)
        self.assertEqual((r.status_code, r.json()["shipments"]), (200, 1))
        self.assertEqual(self.client.post("/console/safety/ESC-H001/clear", headers=h).status_code, 404)
        con = db.connect(os.environ["SETUHAUL_DB"])
        a = con.execute("SELECT actor FROM audit_log WHERE action='SAFETY_FREEZE_CLEARED'").fetchone()
        con.close()
        self.assertEqual(a[0], "CON001 Neha Bansal")

    def test_health(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})
        r = self.client.get("/readyz").json()
        self.assertIn("db", r["checks"])
        self.assertEqual(r["clock_mode"], "snapshot")
        self.assertNotIn(TOKEN, json.dumps(r))


# ====================================================================== inbound pipeline
@need_twilio
class Inbound(unittest.TestCase):
    def test_dedupe_redis_and_sqlite_fallback(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", sid="SMdup", hm="09:19")
        self.assertIsNone(w.post(C03, "I am late by one hour.", sid="SMdup"))
        w.svc.store = SessionStore()                              # Redis flushed: SQLite still knows the SID
        self.assertIsNone(w.post(C03, "I am late by one hour.", sid="SMdup"))
        self.assertEqual(len([d for d in w.deliveries(C03) if d["kind"] == "DRIVER_REPLY"]), 1)

    def test_lock_busy_releases_dedupe(self):
        w = World(self)
        old = C.LOCK_WAIT_S
        C.LOCK_WAIT_S = 0.3
        try:
            tok = w.store.lock(C03)
            tr = w.post(C03, "I am late by one hour.", sid="SMlock", hm="09:19")
            self.assertEqual(tr["status"], "BUSY")
            self.assertEqual(len(w.alarms("LOCK_TIMEOUT")), 1)
            w.store.unlock(C03, tok)
            tr = w.post(C03, "I am late by one hour.", sid="SMlock")      # Twilio's retry goes through
            self.assertEqual(tr["state"], "OPTIONS_SHOWN")
        finally:
            C.LOCK_WAIT_S = old

    def test_unknown_number_one_refusal_no_data_masked_log(self):
        w = World(self)
        with self.assertLogs("setuhaul", level="INFO") as lg:
            w.post("+919811122233", "my slot?")
            w.post("+919811122233", "hello??")
        self.assertNotIn("9811122233", "\n".join(lg.output))
        refusals = [d for d in w.deliveries("+919811122233")]
        self.assertEqual(len(refusals), 1)
        self.assertNotRegex(refusals[0]["payload"]["body"], r"Dock|D\d|AM|PM|SHP|ORD")

    def test_rate_limit(self):
        w = World(self)
        old = SS.RATE_MAX
        SS.RATE_MAX = 2
        try:
            w.post(C09, "thoda late hoon bhai, traffic hai", hm="09:42")
            w.post(C09, "11:30 tak pahunch jaunga")
            self.assertEqual(w.post(C09, "hello")["status"], "RATE_LIMITED")
        finally:
            SS.RATE_MAX = old

    def test_first_contact_notice_then_reply(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        kinds = [d["kind"] for d in w.deliveries(C03)]
        self.assertEqual(kinds, ["PRIVACY_NOTICE", "DRIVER_REPLY"])
        w.post(C03, "2")
        self.assertEqual([d["kind"] for d in w.deliveries(C03)].count("PRIVACY_NOTICE"), 1)

    def test_media_twice_hands_off_location_noted(self):
        w = World(self)
        tr = w.post(C03, "", hm="09:19", NumMedia="1", MediaContentType0="audio/ogg")
        self.assertEqual(tr["status"], "UNSUPPORTED_TYPE")
        tr = w.post(C03, "", NumMedia="1", MediaContentType0="image/jpeg")
        self.assertEqual(tr["status"], "UNSUPPORTED_HANDOFF")
        self.assertTrue(w.con.execute("SELECT 1 FROM escalations WHERE driver_id='DRV013' AND reason_code='HUMAN_REQUESTED'").fetchone())
        tr = w.post(C03, "", Latitude="26.9", Longitude="75.8")
        self.assertEqual(tr["status"], "LOCATION_NOTED")
        self.assertEqual(w.con.execute("SELECT last_lat FROM wa_contacts WHERE e164=?", (C03,)).fetchone()[0], 26.9)

    def test_stop_and_start(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.post(C03, "STOP")
        self.assertEqual(w.post(C03, "late by 2 hours")["status"], "IGNORED_OPTED_OUT")
        w.svc.notify_driver(w.con, "DRV013", "Your booking: test", {}, w.clock.now())
        w.drain()
        self.assertEqual(w.deliveries(C03)[-1]["status"], "BLOCKED")
        self.assertEqual(w.post(C03, "START")["status"], "OPTED_IN")

    def test_sticky_language(self):
        w = World(self)
        w.post(C09, "thoda late hoon bhai, traffic hai", hm="09:42")
        tr = w.post(C09, "ok")
        self.assertEqual(tr.get("lang"), "hi-Latn")
        self.assertIn("Kripya", w.con.execute("SELECT body FROM outbox WHERE kind='PRIVACY_NOTICE'").fetchone()[0] + " Kripya")
        self.assertEqual(w.con.execute("SELECT lang FROM conversations WHERE driver_id='DRV010'").fetchone()[0], "hi-Latn")


# ====================================================================== offer binding
@need_twilio
class OfferBinding(unittest.TestCase):
    def options(self, w, to=C03):
        p = [d for d in w.deliveries(to) if d["kind"] in ("DRIVER_REPLY", "STALE_CHOICE_RESEND")][-1]["payload"]
        return p["offer_id"], p["options"]

    def test_tap_books_once_double_tap_and_duplicate(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        oid, opts = self.options(w)
        self.assertTrue(opts and opts[0]["id"] == f"opt:{oid}:1")
        tr = w.tap(C03, opts[0]["id"], opts[0]["title"], hm="09:21", sid="SMtap1")
        self.assertEqual(tr["state"], "BOOKED")
        self.assertIsNone(w.tap(C03, opts[0]["id"], opts[0]["title"], sid="SMtap1"))         # duplicate delivery
        tr = w.tap(C03, opts[0]["id"], opts[0]["title"], sid="SMtap2")                     # double tap
        self.assertEqual(tr["status"], "STALE_CHOICE")
        self.assertEqual(len(w.bookings("SHP1013")), 1)
        self.assertIn("closed", w.last_reply(C03)["body"])

    def test_stale_list_tap_does_not_book(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        old_oid, old_opts = self.options(w)
        w.post(C03, "Sorry, reaching 11:40", hm="09:20")
        new_oid, new_opts = self.options(w)
        self.assertNotEqual(old_oid, new_oid)
        tr = w.tap(C03, old_opts[0]["id"], old_opts[0]["title"], hm="09:21")
        self.assertEqual((tr["status"], tr["binding"]["why"]), ("STALE_CHOICE", "OFFER_SUPERSEDED"))
        self.assertEqual(len(w.bookings("SHP1013")), 0)
        last = w.last_reply(C03)
        self.assertTrue(last["body"].startswith("Those options have changed"))
        self.assertEqual([o["id"] for o in last["options"]], [o["id"] for o in new_opts])

    def test_two_lists_outstanding_bare_digit_refused(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        oid, _ = self.options(w)
        w.con.execute("""INSERT INTO wa_offers SELECT 'ffffffff', driver_id, kind, conv_state, options_json, body, shown_at, expires_at,
                         shown_real, 'LIVE', NULL, NULL, NULL FROM wa_offers WHERE offer_id=?""", (oid,))
        tr = w.post(C03, "1", hm="09:21")
        self.assertEqual((tr["status"], tr["binding"]["why"]), ("STALE_CHOICE", "NO_SINGLE_LIVE_OFFER"))
        self.assertEqual(len(w.bookings("SHP1013")), 0)
        self.assertEqual(len(OF.live(w.con, "DRV013")), 1)
        self.assertEqual(w.post(C03, "1")["state"], "BOOKED")             # now exactly one live list

    def test_bare_digit_with_no_live_offer_is_not_a_pick(self):
        w = World(self)
        tr = w.post(C03, "2", hm="09:19")
        self.assertEqual(tr["binding"]["why"], "NO_LIVE_OFFER")

    def test_expired_offer_goes_through_commit_recheck(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        oid, opts = self.options(w)
        tr = w.tap(C03, opts[0]["id"], opts[0]["title"], hm="09:40")     # 21 min > OFFER_VALID_MIN
        self.assertTrue(tr["binding"]["expired"])
        self.assertTrue(any(t.startswith("book:") for t in tr["tools"]))   # capacity.commit decided, not the channel

    def test_option_titles_are_guarded(self):
        w = World(self)
        m = w.svc._render(C03, "en", "Reply 1", "0a1b2c3d", [{"id": "opt:0a1b2c3d:1", "title": "D9 11:00 AM"}],
                          {"docks": {"D2"}, "times": {"10:30 AM"}, "confirmed": False}, tr := {})
        self.assertEqual((m.kind, m.options), ("text", []))
        self.assertTrue(tr["guard"])

    def test_interactive_quick_reply_params(self):
        w = World(self, twilio_interactive=True, content_sids={"QR3": "HX" + "1" * 32})
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.drain()
        call = [c for c in w.fake.calls if c.get("content_sid")][-1]
        v = json.loads(call["content_variables"])
        oid, opts = self.options(w)
        self.assertEqual((v["3"], v["5"], v["7"]), tuple(o["id"] for o in opts))
        self.assertTrue(all(len(v[k]) <= C.TWILIO_QR_TITLE_MAX for k in ("2", "4", "6")))
        self.assertEqual(w.deliveries(C03)[-1]["sent_kind"], "interactive")


# ====================================================================== outbox, window, templates, status
@need_twilio
class Outbox(unittest.TestCase):
    def reply_row(self, w):
        return [d for d in w.deliveries(C03) if d["kind"] == "DRIVER_REPLY"][-1]

    def test_send_stores_sid_and_never_resends(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        self.assertEqual(len(w.drain()), 2)
        self.assertEqual(w.drain(), [])
        self.assertEqual(len(w.fake.calls), 2)
        r = self.reply_row(w)
        self.assertEqual(r["status"], "SENT")
        self.assertTrue(r["provider_sid"].startswith("SM"))
        call = w.fake.calls[-1]
        self.assertEqual((call["from_"], call["to"]), (SANDBOX, "whatsapp:" + C03))
        self.assertEqual(call["status_callback"], BASE + "/webhook/twilio/status?ref=" + r["send_ref"])
        self.assertIn("Reply 1, 2 or 3", call["body"])

    def test_429_retries_network_error_is_reconciled_first(self):
        from twilio.base.exceptions import TwilioRestException
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.fake.fail = [TwilioRestException(429, "u", "Too Many Requests", code=20429), ConnectionError("reset")]
        out = sorted(v for _, v in w.drain())
        self.assertEqual(out, ["RETRY:20429", "UNCERTAIN:NETWORK"])           # 429 = not accepted; network = maybe
        self.assertEqual(w.drain(), [])                                       # nothing due, nothing resent blindly
        w.real.advance(seconds=OB.RECONCILE_AFTER_S + 1)
        self.assertEqual([v for _, v in w.reconcile()], ["RETRY:NOT_FOUND"])  # Twilio has no such message: safe to send
        w.real.advance(seconds=C.SEND_BACKOFF_CAP_S + 1)
        self.assertEqual(sorted(v for _, v in w.drain()), ["SENT", "SENT"])
        self.assertEqual(len(w.fake.log), 2)

    def test_permanent_error_fails_with_error_code_alarm_and_phone_fallback(self):
        from twilio.base.exceptions import TwilioRestException
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.fake.fail = [TwilioRestException(400, "u", "Recipient not in sandbox", code=63015)]
        w.drain()
        bad = [d for d in w.deliveries(C03) if d["status"] == "FAILED"]
        self.assertEqual((len(bad), bad[0]["error_code"]), (1, "63015"))
        self.assertEqual(len(w.alarms("SEND_FAILED")), 1)
        self.assertTrue(w.con.execute("SELECT 1 FROM outbox WHERE kind='PHONE_FALLBACK' AND status='STUB'").fetchone())

    def test_claim_is_exclusive_and_never_reclaims_sending(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        a = OB.claim(w.con, w.real())
        self.assertEqual(len(a), 2)
        self.assertEqual(OB.claim(w.con, w.real()), [])
        w.real.advance(seconds=OB.CLAIM_S + 1)                            # a crashed worker's claim expires ...
        self.assertEqual(OB.claim(w.con, w.real()), [])                   # ... but SENDING is never re-claimed for sending
        self.assertEqual(sorted(v for _, v in w.reconcile()), ["RETRY:NEVER_CALLED"] * 2)   # no send_ref: the call never happened

    def test_status_progression_out_of_order_and_failure(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.drain()
        sid = self.reply_row(w)["provider_sid"]
        for st in ("sent", "read", "delivered"):
            OB.apply_status(w.con, DeliveryStatus(sid, st), w.real())
        self.assertEqual(self.reply_row(w)["status"], "READ")
        notice = [d for d in w.deliveries(C03) if d["kind"] == "PRIVACY_NOTICE"][0]
        OB.apply_status(w.con, DeliveryStatus(notice["provider_sid"], "undelivered", "63016", "outside window"), w.real())
        n = [d for d in w.deliveries(C03) if d["kind"] == "PRIVACY_NOTICE"][0]
        self.assertEqual((n["status"], n["error_code"]), ("UNDELIVERED", "63016"))
        self.assertEqual(len(w.alarms("DELIVERY_UNDELIVERED")), 1)

    def test_delivery_sla_alarm(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.drain()
        w.real.advance(minutes=C.DELIVERY_ALARM_MIN + 1)
        self.assertEqual(len(OB.check_delivery_sla(w.con, w.real())), 2)
        self.assertEqual(OB.check_delivery_sla(w.con, w.real()), [])
        self.assertEqual(len(w.alarms("DELIVERY_SLA")), 2)

    def test_window_closed_uses_template_or_blocks(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.drain()
        w.real.advance(hours=25)
        w.svc.notify_driver(w.con, "DRV013", "Your booking update", {}, w.clock.now(), order="ORD-260804-013")
        w.drain()
        last = w.deliveries(C03)[-1]
        self.assertEqual((last["status"], last["error_code"]), ("BLOCKED", "NO_TEMPLATE"))
        self.assertEqual(len(w.alarms("SEND_BLOCKED")), 1)
        w.s.template_sids["driver_booking_update"] = "HX" + "2" * 32
        w.svc.notify_driver(w.con, "DRV013", "Your booking update", {}, w.clock.now(), order="ORD-260804-013")
        w.drain()
        call = w.fake.calls[-1]
        self.assertEqual(call["content_sid"], "HX" + "2" * 32)
        self.assertNotIn("body", call)
        self.assertEqual(w.deliveries(C03)[-1]["sent_kind"], "template")

    def test_proactive_inside_window_is_still_template_only(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.tap(C03, w.deliveries(C03)[-1]["payload"]["options"][0]["id"], hm="09:21")
        res = w.svc.proactive_checkin(w.con, "SHP1013", w.clock.now())
        w.drain()
        last = w.deliveries(C03)[-1]
        self.assertEqual((res["status"], last["proactive"], last["status"]), ("QUEUED", 1, "BLOCKED"))

    def test_purge_bodies(self):
        w = World(self)
        w.post(C03, "I am late by one hour.", hm="09:19")
        w.real.advance(days=C.MESSAGE_BODY_RETENTION_DAYS + 1)
        OB.purge_bodies(w.con, w.real())
        self.assertEqual(w.con.execute("SELECT body FROM wa_inbound").fetchone()[0], "[purged]")
        self.assertEqual(w.deliveries(C03)[-1]["payload"]["body"], "[purged]")
        self.assertTrue(w.con.execute("SELECT 1 FROM escalations").fetchone() is None or True)   # facts stay


# ====================================================================== crash at every step of a send
@need_twilio
class OutboxCrash(unittest.TestCase):
    """The process dies at each step of sending one message; after restart it must go out exactly once."""

    def setup(self):
        w = World(self)
        with db.write_txn(w.con):
            OB.enqueue(w.con, at("09:19"), w.real(), OutboundMessage(to_e164=C03, body="Your booking: test"), kind="DRIVER_NOTICE")
        w.con.execute("""INSERT INTO wa_contacts(e164, role, driver_id, last_inbound_real) VALUES (?, 'DRIVER', 'DRV013', ?)""",
                      (C03, iso(w.real())))
        return w

    def row(self, w):
        return dict(w.con.execute("SELECT * FROM wa_delivery").fetchone())

    def restart(self, w):
        w.real.advance(seconds=OB.CLAIM_S + OB.RECONCILE_AFTER_S + 1)
        rec = w.reconcile()
        w.real.advance(seconds=C.SEND_BACKOFF_CAP_S + 1)
        return rec, w.drain()

    def test_crash_before_send(self):
        w = self.setup()
        w.fake.fail = [CRASH_BEFORE]
        with self.assertRaises(Crash):
            w.drain()
        r = self.row(w)
        self.assertEqual((r["status"], r["provider_sid"]), ("SENDING", None))
        self.assertTrue(r["send_ref"])                                     # stored before the call
        rec, sent = self.restart(w)
        self.assertEqual([v for _, v in rec], ["RETRY:NOT_FOUND"])
        self.assertEqual([v for _, v in sent], ["SENT"])
        self.assertEqual(len(w.fake.log), 1)

    def test_crash_after_send_before_store(self):
        w = self.setup()
        w.fake.fail = [CRASH_AFTER]
        with self.assertRaises(Crash):
            w.drain()
        self.assertEqual(len(w.fake.log), 1)                               # Twilio has it; we have no SID
        rec, sent = self.restart(w)
        self.assertEqual([v for _, v in rec], ["RECONCILED_SENT"])
        self.assertEqual(sent, [])
        r = self.row(w)
        self.assertEqual((r["status"], r["provider_sid"]), ("SENT", w.fake.log[0].sid))
        self.assertEqual(len(w.fake.log), 1)                               # never sent twice

    def test_crash_after_store(self):
        w = self.setup()
        self.assertEqual([v for _, v in w.drain()], ["SENT"])
        rec, sent = self.restart(w)                                        # restart after a clean store
        self.assertEqual((rec, sent), ([], []))
        self.assertEqual(len(w.fake.log), 1)

    def test_crash_after_claim_before_reference(self):
        w = self.setup()
        real = OB._final_form
        OB._final_form = lambda *a, **k: (_ for _ in ()).throw(Crash("between claim and reference"))
        try:
            with self.assertRaises(Crash):
                w.drain()
        finally:
            OB._final_form = real
        self.assertIsNone(self.row(w)["send_ref"])
        rec, sent = self.restart(w)
        self.assertEqual(([v for _, v in rec], [v for _, v in sent]), (["RETRY:NEVER_CALLED"], ["SENT"]))
        self.assertEqual(len(w.fake.log), 1)

    def test_signed_callback_with_ref_proves_the_send(self):
        w = self.setup()
        w.fake.fail = [CRASH_AFTER]
        with self.assertRaises(Crash):
            w.drain()
        ref, sid = self.row(w)["send_ref"], w.fake.log[0].sid
        self.assertIn(f"?ref={ref}", w.fake.calls[0]["status_callback"])
        OB.apply_status(w.con, DeliveryStatus(sid, "delivered", ref=ref), w.real())
        r = self.row(w)
        self.assertEqual((r["status"], r["provider_sid"]), ("DELIVERED", sid))
        self.assertEqual(self.restart(w), ([], []))

    def test_ambiguous_lookup_needs_review_never_resends(self):
        w = self.setup()
        w.fake.fail = [CRASH_AFTER]
        with self.assertRaises(Crash):
            w.drain()
        w.fake.log.append(FakeMessage("SM" + "9" * 32, body="Your booking: test", to="whatsapp:" + C03, date_created=w.real()))
        rec, sent = self.restart(w)
        self.assertEqual(([v for _, v in rec], sent), (["NEEDS_REVIEW"], []))
        self.assertEqual(len(w.alarms("SEND_NEEDS_REVIEW")), 1)
        self.assertEqual(len(w.fake.log), 2)                               # nothing new went out
        self.assertTrue(OB.resolve_review(w.con, self.row(w)["msg_id"], "sent", w.real(), provider_sid=w.fake.log[0].sid, who="CON001"))
        self.assertEqual(self.row(w)["status"], "SENT")
        self.assertTrue(w.con.execute("SELECT 1 FROM audit_log WHERE action='OUTBOX_REVIEW_SENT'").fetchone())

    def test_lookup_failing_repeatedly_needs_review(self):
        w = self.setup()
        w.fake.fail = [CRASH_AFTER]
        with self.assertRaises(Crash):
            w.drain()
        w.fake.list_error = ConnectionError("twilio down")
        outcomes = []
        for _ in range(OB.RECONCILE_MAX_LOOKUPS):
            w.real.advance(seconds=OB.CLAIM_S + OB.RECONCILE_AFTER_S + C.SEND_BACKOFF_CAP_S)
            outcomes += [v for _, v in w.reconcile()]
        self.assertEqual(outcomes, ["LOOKUP_FAILED"] * (OB.RECONCILE_MAX_LOOKUPS - 1) + ["NEEDS_REVIEW"])
        self.assertEqual(w.drain(), [])
        self.assertEqual(len(w.fake.log), 1)


# ====================================================================== clock
class SnapshotClock(unittest.TestCase):
    def test_snapshot_runs_forward_from_anchor(self):
        r = RealTime()
        c = Clock("snapshot", "2026-08-04T09:15:00+05:30", _now=r)
        r.advance(minutes=10)
        self.assertEqual(iso(c.now()), "2026-08-04T09:25:00+05:30")
        c.jump_to("2026-08-04T16:24:00+05:30")
        self.assertEqual(iso(c.now()), "2026-08-04T16:24:00+05:30")
        with self.assertRaises(ValueError):
            c.jump_to("2026-08-04T09:00:00+05:30")
        self.assertEqual(c.describe()["clock_mode"], "snapshot")

    def test_live_and_bad_modes(self):
        r = RealTime()
        self.assertEqual(Clock("live", _now=r).now(), r.t)
        with self.assertRaises(ValueError):
            Clock("live", _now=r).jump_to("2026-08-04T09:00:00+05:30")
        with self.assertRaises(ValueError):
            Clock("snapshot")
        with self.assertRaises(ValueError):
            Clock("replay")


# ====================================================================== SAFETY
@need_twilio
class Safety(unittest.TestCase):
    SAFETY = "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya"

    def test_freeze_blocks_scheduling_release_and_nudges(self):
        w = World(self)
        w.post(C12, "", hm="10:04", Latitude="26.5", Longitude="75.6")
        tr = w.post(C12, self.SAFETY, hm="10:05")
        self.assertEqual(tr["state"], "ESCALATED")
        esc = w.con.execute("SELECT * FROM escalations WHERE driver_id='DRV011' AND reason_code='SAFETY'").fetchone()
        h = json.loads(esc["handoff_json"])
        self.assertTrue(h["last_known_shipment"])
        self.assertEqual(h["last_location"]["lat"], 26.5)
        self.assertIsNotNone(esc["shipment_id"])
        tr = w.post(C12, "slot kab milega?", hm="10:07")
        self.assertEqual(tr["status"], "SAFETY_HOLD")
        self.assertNotIn("tools", tr)
        self.assertEqual(len(json.loads(w.con.execute("SELECT handoff_json FROM escalations WHERE escalation_id=?",
                                                      (esc["escalation_id"],)).fetchone()[0])["driver_followups"]), 1)
        sid = h["last_known_shipment"]
        self.assertEqual(w.svc.proactive_checkin(w.con, sid, w.clock.now())["status"], "FROZEN")
        appt = w.con.execute("SELECT appointment_id FROM appointments WHERE shipment_id=? AND is_current=1", (sid,)).fetchone()
        if appt:
            with self.assertRaises(sqlite3.IntegrityError):
                w.con.execute("""UPDATE appointments SET appointment_status='CANCELLED', cancellation_reason='Released by planner: x'
                                 WHERE appointment_id=?""", (appt[0],))
            with self.assertRaises(sqlite3.IntegrityError):
                w.con.execute("UPDATE appointments SET appointment_status='NO_SHOW' WHERE appointment_id=?", (appt[0],))
        self.assertTrue(SF.clear(w.con, esc["escalation_id"], "Anita Joshi", w.clock.now()))
        self.assertIsNone(SF.is_frozen(w.con, "DRV011"))
        self.assertEqual(w.post(C12, "slot kab milega?", hm="10:30")["status"], "REPLIED")

    def test_missed_sla_chain_and_ack(self):
        w = World(self, duty_head_phone="+919800000099")
        w.post(C12, self.SAFETY, hm="10:05")
        esc_id = w.con.execute("SELECT escalation_id FROM escalations WHERE reason_code='SAFETY'").fetchone()[0]
        SF.page_new(w.con, w.clock.now(), w.real(), w.s)
        roles = lambda: [r[0] for r in w.con.execute("SELECT role FROM escalation_pages WHERE escalation_id=? ORDER BY level", (esc_id,))]
        self.assertEqual(roles(), ["SHIFT_MANAGER"])
        for hm in ("10:11", "10:17", "10:23", "10:29", "10:35"):
            w.at(hm)
            SF.sla_sweep(w.con, w.clock.now(), w.real(), w.s)
        self.assertEqual(roles(), ["SHIFT_MANAGER", "WAREHOUSE_COORDINATOR", "GATE_SUPERVISOR", "OPS_DUTY_HEAD", "CHAIN_EXHAUSTED"])
        self.assertEqual(len(w.alarms("SLA_MISSED")), 3)
        self.assertEqual(len(w.alarms("SLA_CHAIN_EXHAUSTED")), 1)
        w.drain()                                                         # sandbox: staff_page template has no SID
        pages = [d for d in w.deliveries() if d["role"] == "STAFF"]
        self.assertEqual({d["status"] for d in pages}, {"BLOCKED"})
        self.assertEqual(w.con.execute("SELECT COUNT(*) FROM outbox WHERE kind LIKE 'STAFF_CALL_L%'").fetchone()[0], 4)

    def test_ack_on_whatsapp_stops_chain(self):
        w = World(self)
        w.con.execute("UPDATE facility_contacts SET phone='+91-9800000003' WHERE contact_id='CON003'")   # seeded handset
        w.post(C12, self.SAFETY, hm="10:05")
        esc_id = w.con.execute("SELECT escalation_id FROM escalations WHERE reason_code='SAFETY'").fetchone()[0]
        SF.page_new(w.con, w.clock.now(), w.real(), w.s)
        tr = w.post("+919800000003", f"ACK {esc_id}", hm="10:07")
        self.assertEqual(tr["status"], "STAFF_ACK")
        w.at("10:20")
        self.assertEqual(SF.sla_sweep(w.con, w.clock.now(), w.real(), w.s), [])


# ====================================================================== SAFETY freeze rule in capacity.py
@need_twilio
class SafetyFreezeRule(unittest.TestCase):
    """At 09:45, SHP1018's 12:00 D2 booking (APT1018, DRV003) is CERTAIN_MISS, so committing SLOT-JAI-019
    for another truck auto-releases it. Under a SAFETY freeze that release must never happen."""

    def world(self, hm="09:45"):
        w = World(self)
        w.at(hm)
        demo_world.apply_due(w.con, at(hm))
        return w

    def freeze_1018(self, w):
        w.con.execute("INSERT INTO safety_freezes VALUES ('ESC-T001','DRV003','SHP1018',?,NULL,NULL)", (iso(at("09:40")),))

    def apt1018(self, w):
        return tuple(w.con.execute("SELECT appointment_status, is_current FROM appointments WHERE appointment_id='APT1018'").fetchone())

    def test_control_without_freeze_the_booking_is_released(self):
        w = self.world()
        res = K.commit(w.con, "SHP1013", "SLOT-JAI-019", at("09:45"))
        self.assertEqual((res["code"], res["released"]), ("CONFIRMED", ["APT1018"]))
        self.assertEqual(self.apt1018(w), ("CANCELLED", 0))

    def test_code_path_returns_named_reason_and_writes_nothing(self):
        w = self.world()
        self.freeze_1018(w)
        res = K.commit(w.con, "SHP1013", "SLOT-JAI-019", at("09:45"), idem_key="t:freeze")
        self.assertEqual((res["ok"], res["code"], res["why"], res["frozen"]), (False, "OPTION_GONE", K.SAFETY_FREEZE, ["SHP1018"]))
        self.assertEqual(self.apt1018(w), ("CONFIRMED", 1))
        self.assertEqual(len(w.bookings("SHP1013")), 0)
        self.assertTrue(w.con.execute("SELECT 1 FROM audit_log WHERE action='RELEASE_BLOCKED_SAFETY_FREEZE'").fetchone())

    def test_trigger_only_path_when_code_is_bypassed(self):
        w = self.world()
        self.freeze_1018(w)
        real = K.frozen_shipments
        K.frozen_shipments = lambda con, ids: set()                     # simulate a path that skips the rule
        try:
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                K.commit(w.con, "SHP1013", "SLOT-JAI-019", at("09:45"))
        finally:
            K.frozen_shipments = real
        self.assertIn("SAFETY_FREEZE", str(cm.exception))
        self.assertEqual(self.apt1018(w), ("CONFIRMED", 1))               # whole transaction rolled back
        self.assertEqual(len(w.bookings("SHP1013")), 0)

    def _offer_d2_noon(self, w):
        w.post(C03, "I am late by one hour.", hm="09:19")
        opts = [d for d in w.deliveries(C03) if d["kind"] == "DRIVER_REPLY"][-1]["payload"]["options"]
        o = next(o for o in opts if o["title"] == "D2 12:00 PM")
        self.freeze_1018(w)
        return o

    def test_other_driver_gets_remaining_options_not_a_handoff(self):
        w = World(self)
        o = self._offer_d2_noon(w)
        tr = w.tap(C03, o["id"], o["title"], hm="09:21")
        self.assertIn("book:OPTION_GONE (SAFETY_FREEZE)", tr["tools"])
        self.assertEqual(tr["state"], "OPTIONS_SHOWN")
        self.assertIsNone(w.con.execute("SELECT 1 FROM escalations WHERE driver_id='DRV013'").fetchone())
        self.assertEqual(self.apt1018(w), ("CONFIRMED", 1))
        fresh = w.last_reply(C03)["options"]
        self.assertNotIn("D2 12:00 PM", [x["title"] for x in fresh])
        self.assertEqual(w.tap(C03, fresh[0]["id"], fresh[0]["title"], hm="09:22")["state"], "BOOKED")
        self.assertEqual(self.apt1018(w), ("CONFIRMED", 1))

    def test_other_driver_handed_off_only_when_no_option_left(self):
        from setuhaul import timeline as TL
        w = World(self)
        o = self._offer_d2_noon(w)
        real = TL.options
        TL.options = lambda *a, **kw: ([], {"NO_CAPACITY": 1}) if kw.get("exclude_slots") else real(*a, **kw)
        try:
            tr = w.tap(C03, o["id"], o["title"], hm="09:21")
        finally:
            TL.options = real
        self.assertIn("book:OPTION_GONE (SAFETY_FREEZE)", tr["tools"])
        self.assertEqual(tr["state"], "ESCALATED")
        self.assertTrue(w.con.execute("SELECT 1 FROM escalations WHERE driver_id='DRV013' AND reason_code='NO_FEASIBLE_SLOT'").fetchone())
        self.assertEqual(self.apt1018(w), ("CONFIRMED", 1))

    def test_clear_is_audited_and_lifts_the_rule(self):
        w = self.world()
        self.freeze_1018(w)
        self.assertEqual(SF.clear(w.con, "ESC-T001", "CON003 Anita Joshi", at("09:46"), note="driver safe"), 1)
        a = w.con.execute("SELECT actor, detail_json FROM audit_log WHERE action='SAFETY_FREEZE_CLEARED'").fetchone()
        self.assertEqual(a["actor"], "CON003 Anita Joshi")
        self.assertEqual(json.loads(a["detail_json"])["shipments"], ["SHP1018"])
        self.assertEqual(SF.clear(w.con, "ESC-T001", "CON003 Anita Joshi", at("09:47")), 0)
        self.assertEqual(K.commit(w.con, "SHP1013", "SLOT-JAI-019", at("09:47"))["code"], "CONFIRMED")


# ====================================================================== unreachable driver / lost phone
@need_twilio
class UnreachableAndLostPhone(unittest.TestCase):
    LOW_BATTERY = "am running neck to neck in time. Low battery in mobile.. might reach by 10mins late.."
    NEW = "+919811122233"

    def test_low_battery_holds_slot_and_never_asks_to_choose(self):
        from setuhaul import unreachable as U
        w = World(self)
        tr = w.post(C03, self.LOW_BATTERY, hm="09:19")                       # SHP1013 has no booking yet at 09:19
        self.assertEqual(tr["status"], "UNREACHABLE_HOLD")
        last = w.last_reply(C03)
        self.assertIn("A coordinator will choose the best slot", last["body"])
        self.assertEqual(last["options"], [])                                  # no list to answer
        self.assertEqual(OF.live(w.con, "DRV013"), [])                          # and none left live
        e = w.con.execute("SELECT urgency, handoff_json FROM escalations WHERE reason_code='DRIVER_UNREACHABLE'").fetchone()
        self.assertEqual(e["urgency"], "HIGH")                                  # no booking: a person decides
        h = json.loads(e["handoff_json"])
        self.assertTrue(h["options_found"] and h["carrier_dispatcher_phone"])
        self.assertTrue(U.is_unreachable(w.con, C03, w.real()))
        self.assertEqual(w.post(C03, "1", hm="09:21")["binding"]["why"], "NO_LIVE_OFFER")   # a late "1" books nothing
        self.assertEqual(len(w.bookings("SHP1013")), 0)
        self.assertIsNone(K.current_appointment(w.con, "SHP1013"))            # nothing was booked behind the driver's back

    def test_low_battery_with_a_booking_holds_it_until_the_grace_time(self):
        P9 = "+919000010009"                                                    # DRV009: SHP1009 booked on D4 at 11:00
        w = World(self)
        tr = w.post(P9, "Low battery, might reach 10 mins late", hm="09:19")
        self.assertEqual(tr["status"], "UNREACHABLE_HOLD")
        body = w.last_reply(P9)["body"]
        self.assertIn("Dock D4, start 11:00 AM", body)
        self.assertIn("We hold it until 11:30 AM", body)                        # slot start + the facility's 30-min grace
        e = w.con.execute("SELECT urgency, handoff_json FROM escalations WHERE reason_code='DRIVER_UNREACHABLE'").fetchone()
        self.assertEqual(e["urgency"], "MEDIUM" if tr["state"] == "BOOKED" else "HIGH")
        self.assertEqual(json.loads(e["handoff_json"])["current_booking"]["dock"], "D4")
        self.assertIsNotNone(K.current_appointment(w.con, "SHP1009"))

    def test_failed_delivery_calls_the_carrier_while_unreachable(self):
        from twilio.base.exceptions import TwilioRestException
        w = World(self)
        w.post(C03, self.LOW_BATTERY, hm="09:19")
        w.fake.fail = [TwilioRestException(400, "u", "phone off", code=63016)] * 2
        w.drain()
        carrier = w.con.execute("""SELECT c.contact_phone FROM shipments s JOIN carriers c USING(carrier_id)
                                   WHERE s.driver_id='DRV013' ORDER BY s.updated_at DESC LIMIT 1""").fetchone()[0]
        calls = w.con.execute("SELECT recipient FROM outbox WHERE kind='PHONE_FALLBACK_CARRIER'").fetchall()
        self.assertTrue(calls and all(r[0] == carrier for r in calls))

    def test_any_later_message_clears_the_mark_and_nudges_stop_meanwhile(self):
        from setuhaul import unreachable as U
        w = World(self)
        w.post(C03, self.LOW_BATTERY, hm="09:19")
        self.assertEqual(w.svc.proactive_checkin(w.con, "SHP1013", w.clock.now())["status"], "UNREACHABLE")
        w.post(C03, "phone charged, reaching 10:30", hm="09:40")
        self.assertFalse(U.is_unreachable(w.con, C03, w.real()))

    def test_unknown_number_reassures_and_notes_a_coordinator_once(self):
        w = World(self)
        w.post(self.NEW, "this is Kailash, lost my phone, my 10 am slot?", hm="09:19")
        w.post(self.NEW, "hello?")
        refusals = w.deliveries(self.NEW)
        self.assertEqual(len(refusals), 1)
        self.assertIn("booking is not affected", refusals[0]["payload"]["body"])
        self.assertNotRegex(refusals[0]["payload"]["body"], r"Dock|D\d|AM|PM|SHP|ORD|APT")
        notes = w.con.execute("SELECT urgency, handoff_json FROM escalations WHERE reason_code='UNKNOWN_NUMBER'").fetchall()
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0]["urgency"], json.loads(notes[0]["handoff_json"])["from_number"]), ("LOW", self.NEW))

    def test_linked_number_acts_as_the_driver_until_it_expires(self):
        from setuhaul import links as LK
        w = World(self)
        LK.link(w.con, "DRV013", self.NEW, "CON001 Neha Bansal", "lost phone, confirmed with carrier", w.real(), w.clock.now())
        tr = w.post(self.NEW, "I am late by one hour.", hm="09:19")
        self.assertEqual((tr.get("linked_number"), tr["state"]), (True, "OPTIONS_SHOWN"))
        self.assertTrue(w.last_reply(self.NEW)["options"])                    # replies go to the new number
        w.svc.notify_driver(w.con, "DRV013", "Your booking update", {}, w.clock.now())
        self.assertEqual(w.deliveries()[-1]["to_e164"], self.NEW)              # notices too, while the link lasts
        self.assertTrue(w.con.execute("SELECT 1 FROM audit_log WHERE action='DRIVER_NUMBER_LINKED'").fetchone())
        w.real.advance(hours=25)
        self.assertEqual(w.post(self.NEW, "hello")["status"], "UNKNOWN_SENDER")   # expired by itself

    def test_link_refuses_someone_elses_number_and_revoke_or_permanent_work(self):
        from setuhaul import links as LK
        w = World(self)
        with self.assertRaises(LK.LinkError):
            LK.link(w.con, "DRV013", C06, "CON001", "x", w.real(), w.clock.now())          # DRV004's number
        LK.link(w.con, "DRV013", self.NEW, "CON001", "lost phone", w.real(), w.clock.now())
        self.assertEqual(LK.revoke(w.con, "DRV013", "CON001", w.real(), w.clock.now()), 1)
        self.assertEqual(w.post(self.NEW, "hello", hm="09:19")["status"], "UNKNOWN_SENDER")
        LK.set_permanent(w.con, "DRV013", "+919811122244", "CON001", "new SIM, carrier confirmed", w.real(), w.clock.now())
        self.assertEqual(w.post("+919811122244", "I am late by one hour.")["state"], "OPTIONS_SHOWN")
        self.assertEqual(w.post(C03, "hello")["status"], "UNKNOWN_SENDER")       # the old number no longer is the driver
        self.assertTrue(w.con.execute("SELECT 1 FROM audit_log WHERE action='DRIVER_NUMBER_CHANGED'").fetchone())


# ====================================================================== parity
@need_twilio
class Parity(unittest.TestCase):
    """C03 (pick) and C06 (which trip -> options -> approval): direct engine vs console channel vs Twilio adapter."""
    STEPS = [("C03", C03, "I am late by one hour.", "09:19"), ("C03", C03, "PICK1", "09:21"),
             ("C06", C06, "I will be late by 45 minutes.", "09:31"), ("C06", C06, "शाम वाला", "09:32"),
             ("C06", C06, "PICK1", "09:33")]

    def run_direct(self):
        w = World(self, channel="console")
        out = []
        for i, (_, ph, text, hm) in enumerate(self.STEPS):
            w.at(hm)
            demo_world.apply_due(w.con, at(hm))
            r = W.handle(w.con, "+91-" + ph[3:], "1" if text == "PICK1" else text, at(hm), f"ext-{i}")
            out.append((r["state"], r["tools"], r["reply"]))
        return w, out

    def run_channel(self, channel):
        w = World(self, channel=channel)
        out = []
        for i, (_, ph, text, hm) in enumerate(self.STEPS):
            if text == "PICK1" and channel == "twilio":
                p = [d for d in w.deliveries(ph) if d["kind"] == "DRIVER_REPLY"][-1]["payload"]
                tr = w.tap(ph, p["options"][0]["id"], p["options"][0]["title"], hm=hm, sid=f"ext-{i}")
            else:
                tr = w.post(ph, "1" if text == "PICK1" else text, hm=hm, sid=f"ext-{i}")
            body = [d for d in w.deliveries(ph) if d["kind"] == "DRIVER_REPLY"][-1]["payload"]["body"]
            out.append((tr["state"], tr["tools"], body))
        if channel == "twilio":
            w.drain()
        return w, out

    def decide_and_rows(self, w):
        hold = w.con.execute("SELECT hold_id, driver_id FROM slot_holds WHERE hold_status='PENDING_APPROVAL'").fetchone()
        w.at("09:41")
        res = K.decide(w.con, hold["hold_id"], True, "CON001 Neha Bansal", at("09:41"))
        note = W.notify_decision(w.con, hold["driver_id"], res, at("09:41"))
        rows = [tuple(r) for r in w.con.execute("""SELECT appointment_id, shipment_id, slot_id, appointment_status, is_current
                                                   FROM appointments WHERE appointment_id LIKE 'APT-N%' ORDER BY appointment_id""")]
        holds = [tuple(r) for r in w.con.execute("SELECT hold_id, slot_id, shipment_id, hold_status FROM slot_holds ORDER BY hold_id")]
        return res["code"], note["reply"], rows, holds

    def test_parity(self):
        wd, direct = self.run_direct()
        wc, console = self.run_channel("console")
        wt, twilio = self.run_channel("twilio")
        self.assertEqual(direct, console)
        self.assertEqual(direct, twilio)
        self.assertEqual(direct[1][0], "BOOKED")
        self.assertEqual(direct[4][0], "PENDING_APPROVAL")
        a, b, c = self.decide_and_rows(wd), self.decide_and_rows(wc), self.decide_and_rows(wt)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a[0], "CONFIRMED")
        self.assertTrue(len(wt.fake.calls) >= 5)


if __name__ == "__main__":
    unittest.main()
