"""Tests for the production layer: graph.py, session_store.py, llm.py, auth.py, tracing.py, app.py.

Run:  python3 -I -m unittest tests.test_production_layer -v      (from the prototype folder)

Nothing here needs a network, an API key or a Redis server. Tests that need an optional
package (langgraph, langchain-core, fakeredis, fastapi) are skipped when it is missing.
"""
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import db, world, workflow as W, graph as G, llm as L, auth as A, tracing
from setuhaul.session_store import SessionStore, normalise_phone
from setuhaul.nlu import Facts
from setuhaul.clock import ts

has = lambda m: importlib.util.find_spec(m) is not None
START = ts("2026-08-04T08:20:00+05:30")
C03 = "+91-9000010013"          # driver of SHP1013 (scenario C03)
C12 = "+91-9000010011"          # driver of SHP1010 (scenario C12, safety)
MESSAGES = [                    # (phone, text, time) - a slice of the recorded morning
    (C03, "I am late by one hour.", "09:19"),
    (C03, "3", "09:21"),
    (C12, "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya", "10:05"),
]


def fresh_world():
    d = tempfile.mkdtemp(prefix="sh-prod-")
    path = db.fresh_copy(os.path.join(d, "t.db"))
    con = db.connect(path)
    world.rewind(con, START)
    return con, d


def at(hhmm):
    return ts(f"2026-08-04T{hhmm}:00+05:30")


class GraphParity(unittest.TestCase):
    """The LangGraph wiring must give exactly the same turns as workflow.handle()."""

    def replay(self, runner):
        con, d = fresh_world()
        try:
            return [runner(con, p, t, at(h), f"wa-{i}") for i, (p, t, h) in enumerate(MESSAGES)]
        finally:
            con.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_plain_loop_matches_handle(self):
        a = self.replay(lambda c, p, t, a_, e: W.handle(c, p, t, a_, e))
        b = self.replay(lambda c, p, t, a_, e: G.run_turn(c, p, t, a_, e, use_langgraph=False))
        self.assertEqual(a, b)
        self.assertEqual(a[0]["state"], "OPTIONS_SHOWN")
        self.assertEqual(a[2]["state"], "ESCALATED")

    @unittest.skipUnless(has("langgraph"), "langgraph not installed")
    def test_langgraph_matches_handle(self):
        a = self.replay(lambda c, p, t, a_, e: W.handle(c, p, t, a_, e))
        b = self.replay(lambda c, p, t, a_, e: G.run_turn(c, p, t, a_, e))
        self.assertEqual(a, b)

    def test_unknown_sender_ends_at_verify(self):
        con, d = fresh_world()
        try:
            out = G.run_turn(con, "+91-0000000000", "hello", at("09:00"))
            self.assertEqual(out["state"], "UNKNOWN_SENDER")
        finally:
            con.close()
            shutil.rmtree(d, ignore_errors=True)


class LlmFallbackHook(unittest.TestCase):
    def test_called_only_when_rules_say_unknown(self):
        calls = []

        def fake(text, as_of, history):
            calls.append(text)
            f = Facts()
            f.intent, f.delay_min, f.evidence = "REPORT_DELAY", 60, {"delay_min": "ek ghante"}
            return f
        con, d = fresh_world()
        try:
            W.handle(con, C03, "I am late by one hour.", at("09:19"), "wa-a", llm_fallback=fake)
            self.assertEqual(calls, [])                         # rules understood it: no model call
            out = W.handle(con, C12, "gaadi kharab, ek ghante baad", at("09:30"), "wa-b", llm_fallback=fake)
            self.assertEqual(calls, ["gaadi kharab, ek ghante baad"])   # rules said UNKNOWN -> model asked once
            self.assertIn("llm_fallback:REPORT_DELAY", out["tools"])    # ... and the call is in the trace
            self.assertIn("read_message:REPORT_DELAY", out["tools"])
        finally:
            con.close()
            shutil.rmtree(d, ignore_errors=True)

    def test_none_fallback_keeps_old_behaviour(self):
        con, d = fresh_world()
        try:
            out = W.handle(con, C03, "hmm", at("09:19"), "wa-c", llm_fallback=None)
            self.assertFalse(any(t.startswith("llm_fallback") for t in out["tools"]))
        finally:
            con.close()
            shutil.rmtree(d, ignore_errors=True)


class LlmValidation(unittest.TestCase):
    AS_OF = at("09:30")
    TEXT = "bhai gaadi kharab, shaam 7 baje tak pahunchunga"

    def good(self, **kw):
        raw = {"intent": "REPORT_DELAY", "eta": "19:00", "delay_min": None, "limits": {}, "shipment_hint": "EVENING",
               "choice": None, "safety": False, "evidence": {"eta": "shaam 7 baje", "shipment_hint": "shaam"}}
        raw.update(kw)
        return raw

    def test_good_answer_becomes_facts(self):
        f = L.validate(self.good(), self.TEXT, self.AS_OF)
        self.assertEqual(f.intent, "REPORT_DELAY")
        self.assertEqual(f.eta.hour, 19)
        self.assertEqual(f.eta_confidence, "MEDIUM")          # never HIGH from a model
        self.assertEqual(f.evidence["source"], "llm")

    def test_invented_evidence_is_rejected(self):
        self.assertIsNone(L.validate(self.good(evidence={"eta": "8 baje"}), self.TEXT, self.AS_OF))

    def test_value_without_evidence_is_rejected(self):
        self.assertIsNone(L.validate(self.good(evidence={"shipment_hint": "shaam"}), self.TEXT, self.AS_OF))

    def test_time_out_of_range_is_rejected(self):
        self.assertIsNone(L.validate(self.good(eta="07:00"), self.TEXT, self.AS_OF))

    def test_extra_field_is_rejected(self):
        raw = self.good()
        raw["book_slot"] = "SLOT-JAI-001"
        self.assertIsNone(L.validate(raw, self.TEXT, self.AS_OF))

    def test_choice_beyond_three_is_rejected(self):
        self.assertIsNone(L.validate(self.good(choice=7, evidence={"eta": "shaam 7 baje", "shipment_hint": "shaam", "choice": "7"}),
                                     self.TEXT, self.AS_OF))

    def test_model_cannot_clear_safety(self):
        text = "accident ho gaya"
        raw = {"intent": "UNKNOWN", "eta": None, "delay_min": None, "limits": {}, "shipment_hint": None,
               "choice": None, "safety": False, "evidence": {}}
        f = L.validate(raw, text, self.AS_OF)
        self.assertTrue(f.safety)
        self.assertEqual(f.intent, "SAFETY")

    def test_no_key_means_no_fallback(self):
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertIsNone(L.make_fallback())
        finally:
            if old:
                os.environ["ANTHROPIC_API_KEY"] = old

    @unittest.skipUnless(has("langchain_core"), "langchain-core not installed")
    def test_json_to_langchain_and_back(self):
        hist = [{"role": "human", "text": "late hoon"}, {"role": "ai", "text": "Kitne baje pahunchoge?"}]
        msgs = L.to_langchain(hist, "7 baje")
        self.assertEqual([m.type for m in msgs], ["system", "human", "ai", "human"])
        self.assertEqual(L.from_langchain(msgs), hist + [{"role": "human", "text": "7 baje"}])


class Sessions(unittest.TestCase):
    def stores(self):
        out = [SessionStore()]
        if has("fakeredis"):
            import fakeredis
            out.append(SessionStore(fakeredis.FakeRedis(decode_responses=True)))
        return out

    def test_phone_forms_are_one_user(self):
        self.assertEqual(normalise_phone("+91-90000 10013"), normalise_phone("9000010013"))

    def test_history_dedupe_lock_rate_tokens(self):
        for s in self.stores():
            with self.subTest(backend=s.backend if not has("fakeredis") else type(s.r).__name__):
                s.append_history(C03, "human", "late hoon")
                s.append_history("9000010013", "ai", "Kitne baje?", state="NEED_ETA")
                self.assertEqual([h["role"] for h in s.history(C03)], ["human", "ai"])
                with self.assertRaises(ValueError):
                    s.append_history(C03, "system", "x")
                self.assertTrue(s.first_time("wamid.1"))
                self.assertFalse(s.first_time("wamid.1"))
                tok = s.lock(C03)
                self.assertIsNotNone(tok)
                self.assertIsNone(s.lock(C03))                   # one turn at a time
                s.unlock(C03, tok)
                self.assertIsNotNone(s.lock(C03))
                self.assertTrue(all(s.allow(C12) for _ in range(20)))
                self.assertFalse(s.allow(C12))                   # 21st message in a minute
                jti = s.start_coordinator("neha.bansal@setuhaul.example", {"role": "WAREHOUSE_COORDINATOR"}, 60)
                self.assertTrue(s.token_live(jti))
                s.logout(jti)
                self.assertFalse(s.token_live(jti))


class Auth(unittest.TestCase):
    def test_whatsapp_signature(self):
        import hashlib
        import hmac
        body = b'{"entry":[]}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(A.verify_whatsapp_signature(body, sig, "s3cret"))
        self.assertFalse(A.verify_whatsapp_signature(body + b" ", sig, "s3cret"))
        self.assertFalse(A.verify_whatsapp_signature(body, sig, ""))

    def test_dev_token(self):
        tok, claims = A.issue_dev_token({"user_id": "x@y"}, "k", ttl_s=60, now=1000)
        self.assertEqual(A.verify_dev_token(tok, "k", now=1010)["user_id"], "x@y")
        with self.assertRaises(A.AuthError):
            A.verify_dev_token(tok, "other", now=1010)
        with self.assertRaises(A.AuthError):
            A.verify_dev_token(tok, "k", now=2000)

    def test_coordinator_lookup_by_email_or_phone(self):
        con, d = fresh_world()
        try:
            self.assertEqual(A.coordinator_for(con, "neha.bansal@setuhaul.example")["role"], "WAREHOUSE_COORDINATOR")
            self.assertEqual(A.coordinator_for(con, "+91 141 5001103")["name"], "Anita Joshi")
            self.assertIsNone(A.coordinator_for(con, "someone@else.com"))
            self.assertEqual(A.driver_for_phone(con, "919000010013")["driver_id"], "DRV013")
        finally:
            con.close()
            shutil.rmtree(d, ignore_errors=True)


class Tracing(unittest.TestCase):
    def test_phone_masked(self):
        self.assertNotIn("10013", tracing.mask("driver +91-9000010013 said hi"))

    def test_tags(self):
        self.assertIn("safety", tracing.tags_for({"tools": ["escalate:SAFETY ESC-1"], "state": "ESCALATED"}))


@unittest.skipUnless(has("fastapi") and has("httpx"), "fastapi/httpx not installed")
class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="sh-app-")
        path = db.fresh_copy(os.path.join(cls.dir, "app.db"))
        c = db.connect(path)
        world.rewind(c, START)
        c.close()
        os.environ.update(SETUHAUL_DB=path, SETUHAUL_DEV="1", WHATSAPP_APP_SECRET="s3cret")
        os.environ.pop("REDIS_URL", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        sys.modules.pop("app", None)
        cls.app = importlib.import_module("app")
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app)

    @classmethod
    def tearDownClass(cls):
        for k in ("SETUHAUL_DB", "SETUHAUL_DEV", "WHATSAPP_APP_SECRET"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def signed(self, payload):
        import hashlib
        import hmac
        body = json.dumps(payload).encode()
        return body, {"X-Hub-Signature-256": "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest(),
                      "X-SetuHaul-As-Of": "2026-08-04T09:19:00+05:30", "Content-Type": "application/json"}

    def test_whatsapp_webhook_end_to_end(self):
        msg = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "919000010013", "id": "wamid.T1", "type": "text", "text": {"body": "I am late by one hour."}}]}}]}]}
        body, h = self.signed(msg)
        r = self.client.post("/webhook/whatsapp", content=body, headers=h)
        self.assertEqual(r.status_code, 200)
        out = r.json()["results"][0]
        self.assertEqual(out["state"], "OPTIONS_SHOWN")
        self.assertEqual(out["delivery"], "QUEUED")
        r2 = self.client.post("/webhook/whatsapp", content=body, headers=h)          # Meta redelivery
        self.assertEqual(r2.json()["results"][0]["state"], "DUPLICATE")
        hist = self.app.STORE.history("+919000010013")
        self.assertEqual([x["role"] for x in hist], ["human", "ai"])

    def test_bad_signature_refused(self):
        body, h = self.signed({"entry": []})
        h["X-Hub-Signature-256"] = "sha256=00"
        self.assertEqual(self.client.post("/webhook/whatsapp", content=body, headers=h).status_code, 401)

    def test_coordinator_sign_in_and_logout(self):
        self.assertEqual(self.client.get("/console/handoffs").status_code, 401)
        tok = self.client.post("/auth/dev-login", json={"user_id": "neha.bansal@setuhaul.example"}).json()["token"]
        auth = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.client.get("/console/handoffs", headers=auth).status_code, 200)
        self.assertEqual(self.client.post("/auth/logout", headers=auth).status_code, 200)
        self.assertEqual(self.client.get("/console/handoffs", headers=auth).status_code, 401)
        self.assertEqual(self.client.post("/auth/dev-login", json={"user_id": "nobody@x.com"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
