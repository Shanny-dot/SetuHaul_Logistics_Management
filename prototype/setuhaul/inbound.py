"""The WhatsApp channel service: one inbound message -> zero or more outbound messages in the outbox.

    webhook (signature already verified) -> accept(): dedupe by MessageSid (Redis + SQLite), log
    background -> process():
        identity (verified sender number only) -> rate limit -> per-driver lock
        -> 24 h window opens -> STOP/START -> first-contact privacy notice
        -> SAFETY freeze? -> unsupported types -> offer binding -> graph.run_turn (unchanged engine)
        -> SAFETY freeze on a new safety escalation -> render (guard body + option titles)
        -> outbox rows in ONE transaction -> worker sends after COMMIT

The engine never sees the provider. Nothing here decides a booking.
"""
import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta

from . import auth as A
from . import config as C
from . import nlu
from . import offers as OF
from . import links as LK
from . import unreachable as U
from . import voice as V
from . import replies as R
from . import safety as S
from . import escalation as E
from .chan_db import alarm
from .channels.base import OutboundMessage, Option
from .clock import iso, ts, ampm, add
from .db import write_txn, one
from .graph import run_turn
from .outbox import enqueue
from .session_store import normalise_phone
from .tracing import mask, traced

log = logging.getLogger("setuhaul.inbound")
STOP_WORDS = {"STOP", "STOPALL", "STOP ALL", "UNSUBSCRIBE", "OPT OUT", "OPTOUT"}
START_WORDS = {"START", "UNSTOP", "OPT IN"}


def _hash(phone):
    return hashlib.sha256(normalise_phone(phone).encode()).hexdigest()[:32]


class ChannelService:
    def __init__(self, connect, store, channel, settings, clock, llm=None, demo_world=False):
        self.connect, self.store, self.channel, self.s, self.clock, self.llm = connect, store, channel, settings, clock, llm
        self.demo_world = demo_world
        self.on_enqueued = None                 # app sets this to wake the outbox worker
        self.voice = None                       # voice.ElevenLabs when ELEVENLABS_API_KEY is set (app.py)
        self.media_dir = None                   # where reply audio is written; served at <base>/media/<name>
        self.fetch_media = None                 # (url) -> (bytes, content type): Twilio media download

    # ------------------------------------------------------------ fast path (inside the webhook)
    def accept(self, msgs):
        """Dedupe by provider message id. Returns the messages that are new. Fast: no engine work."""
        fresh = []
        con = self.connect()
        try:
            for m in msgs:
                if not self.store.first_time(f"{m.provider}:{m.message_id}"):
                    continue
                with write_txn(con):
                    cur = con.execute("""INSERT OR IGNORE INTO wa_inbound(message_sid, provider, e164, msg_type, body, choice_id,
                                         received_real, status) VALUES (?,?,?,?,?,?,?, 'RECEIVED')""",
                                      (m.message_id, m.provider, _hash(m.from_e164), m.type, m.text, m.choice_id, iso(self.clock.real_now())))
                if cur.rowcount == 1:                 # SQLite is the durable fallback when Redis was flushed
                    fresh.append(m)
        finally:
            con.close()
        return fresh

    # ------------------------------------------------------------ slow path (background)
    @traced("whatsapp_turn")
    def process(self, msg):
        as_of, real = self.clock.now(), self.clock.real_now()
        trace = {"trace_id": uuid.uuid4().hex, "message_sid": msg.message_id, "from": mask(msg.from_e164), "type": msg.type,
                 "provider": msg.provider, "clock_mode": self.clock.mode, "as_of": iso(as_of), "outbox_ids": [], "status": None}
        con = self.connect()
        try:
            if self.demo_world:
                from . import demo_world
                applied = demo_world.apply_due(con, as_of)
                if applied:
                    trace["world_events"] = applied
            self._process(con, msg, as_of, real, trace)
        except Exception as e:                        # never leak; never leave the driver without an answer
            log.exception("turn failed %s", msg.message_id)
            trace["status"] = "ERROR"
            trace["error"] = type(e).__name__
            try:
                with write_txn(con):
                    alarm(con, "TURN_ERROR", msg.message_id, {"error": type(e).__name__, "detail": mask(str(e))[:200]}, real, as_of)
            except Exception:
                log.exception("could not raise alarm")
        finally:
            try:
                with write_txn(con):
                    con.execute("UPDATE wa_inbound SET status=?, as_of=?, trace_id=? WHERE message_sid=?",
                                (trace["status"] or "PROCESSED", iso(as_of), trace["trace_id"], msg.message_id))
                    con.execute("INSERT INTO channel_traces VALUES (?,?,?,?)",
                                (trace["trace_id"], iso(real), "inbound", json.dumps(trace, default=str, ensure_ascii=False)))
            finally:
                con.close()
        log.info("turn %s", json.dumps({k: trace.get(k) for k in ("trace_id", "message_sid", "from", "type", "status", "state",
                                                                  "window_open", "outbox_ids", "clock_mode", "as_of")}, default=str))
        if trace["outbox_ids"] and self.on_enqueued:
            self.on_enqueued()
        return trace

    def _process(self, con, msg, as_of, real, trace):
        drv = A.driver_for_phone(con, msg.from_e164)
        if not drv:                                   # lost phone: a coordinator-linked temporary number (links.py)
            drv = LK.active_driver_for(con, msg.from_e164, real)
            if drv:
                trace["linked_number"] = True
        staff = None if drv else self._staff_for(con, msg.from_e164)
        if not drv and not staff:
            return self._unknown(con, msg, as_of, real, trace)
        if not self.store.allow(msg.from_e164):
            trace["status"] = "RATE_LIMITED"
            return
        tok = self._lock(msg.from_e164)
        if not tok:
            trace["status"] = "BUSY"
            with write_txn(con):
                alarm(con, "LOCK_TIMEOUT", msg.message_id, {"from": mask(msg.from_e164)}, real, as_of)
            self.store.r.delete(f"dedupe:{msg.provider}:{msg.message_id}")     # let a provider retry through
            con.execute("DELETE FROM wa_inbound WHERE message_sid=?", (msg.message_id,))
            return
        try:
            role = "DRIVER" if drv else "STAFF"
            contact = self._touch_contact(con, msg, role, drv, staff, real)
            trace["window_open"] = True                                   # an inbound message always (re)opens it
            if role == "STAFF":
                return self._staff(con, msg, staff, as_of, real, trace)
            return self._driver(con, msg, drv, contact, as_of, real, trace)
        finally:
            self.store.unlock(msg.from_e164, tok)

    # ------------------------------------------------------------ helpers
    def _lock(self, phone):
        deadline = time.monotonic() + C.LOCK_WAIT_S
        while True:
            tok = self.store.lock(phone)
            if tok or time.monotonic() >= deadline:
                return tok
            time.sleep(0.2)

    def _staff_for(self, con, phone):
        want = normalise_phone(phone)
        for c in con.execute("SELECT * FROM facility_contacts WHERE active_flag=1 AND phone IS NOT NULL"):
            if normalise_phone(c["phone"]) == want:
                return dict(c)
        return None

    def _touch_contact(self, con, msg, role, drv, staff, real):
        e = normalise_phone(msg.from_e164)
        with write_txn(con):
            con.execute("""INSERT INTO wa_contacts(e164, role, driver_id, contact_id, last_inbound_real) VALUES (?,?,?,?,?)
                           ON CONFLICT(e164) DO UPDATE SET last_inbound_real=excluded.last_inbound_real, role=excluded.role,
                           driver_id=excluded.driver_id, contact_id=excluded.contact_id""",
                        (e, role, drv and drv["driver_id"], staff and staff["contact_id"], iso(real)))
        return one(con, "SELECT * FROM wa_contacts WHERE e164=?", e)

    def _lang(self, con, driver_id):
        c = one(con, "SELECT lang FROM conversations WHERE driver_id=?", driver_id)
        return (c and c["lang"]) or "en"

    def _queue(self, con, as_of, real, trace, msgs, role="DRIVER"):
        """All outbound messages of one turn, in one transaction. Each body passes the guard first."""
        with write_txn(con):
            for m, kind, ev, extra in msgs:
                bad = R.guard(m.body, ev) if m.body else []
                if bad:                                       # a channel-layer text must never state an unbacked fact
                    trace.setdefault("guard", []).extend(bad)
                    m.body, m.options, m.kind = R.t("unknown", m.lang), [], "text"
                trace["outbox_ids"].append(enqueue(con, as_of, real, m, role=role, kind=kind, trace_id=trace["trace_id"], **extra))

    def _text(self, to, lang, body, kind="text"):
        return OutboundMessage(to_e164=normalise_phone(to), kind=kind, body=body, lang=lang)

    # ------------------------------------------------------------ unknown senders
    def _unknown(self, con, msg, as_of, real, trace):
        trace["status"] = "UNKNOWN_SENDER"
        h = _hash(msg.from_e164)
        r = one(con, "SELECT * FROM wa_unknown_senders WHERE e164_hash=?", h)
        log.warning("unknown sender %s", mask(msg.from_e164))
        if r and real - ts(r["last_refused_real"]) < timedelta(hours=C.UNKNOWN_SENDER_REFUSAL_EVERY_H):
            with write_txn(con):
                con.execute("UPDATE wa_unknown_senders SET count=count+1 WHERE e164_hash=?", (h,))
            return
        with write_txn(con):
            con.execute("""INSERT INTO wa_unknown_senders VALUES (?,?,1) ON CONFLICT(e164_hash) DO UPDATE SET
                           last_refused_real=excluded.last_refused_real, count=count+1""", (h, iso(real)))
            con.execute("""INSERT INTO wa_contacts(e164, role, last_inbound_real) VALUES (?, 'UNKNOWN', ?)
                           ON CONFLICT(e164) DO UPDATE SET last_inbound_real=excluded.last_inbound_real""",
                        (normalise_phone(msg.from_e164), iso(real)))
        with write_txn(con):                          # a coordinator sees it: maybe a driver who lost their phone
            esc = E.escalate(con, "UNKNOWN_NUMBER", "LOW", None, None, as_of, {
                "from_number": normalise_phone(msg.from_e164), "message": (msg.text or f"[{msg.type}]")[:300],
                "told": "not registered; booking not affected; ask the dispatcher, or truck number and licence at the gate",
                "action": ("If this is a registered driver who lost their phone, confirm with their carrier by phone, then "
                           "link the number in the console (temporary) or change it permanently. Never trust the message alone.")})
        trace["escalation"] = esc["escalation_id"]
        self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, "en", R.t("unknown_sender", "en")), "UNKNOWN_SENDER_REFUSAL",
                                               {}, {})], role="UNKNOWN")

    # ------------------------------------------------------------ staff
    def _staff(self, con, msg, staff, as_of, real, trace):
        ref = S.staff_ack(con, staff, msg.text, as_of)
        trace["status"] = "STAFF_ACK" if ref else "STAFF_HELP"
        body = R.t("staff_ack", "en", name=staff["contact_name"], ref=ref) if ref else R.t("staff_help", "en")
        self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, "en", body), "STAFF_REPLY", {}, {})], role="STAFF")

    # ------------------------------------------------------------ drivers
    def _driver(self, con, msg, drv, contact, as_of, real, trace):
        did, lang = drv["driver_id"], self._lang(con, drv["driver_id"])
        word = (msg.text or "").strip().upper()
        out = []
        if word in STOP_WORDS:
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET opted_out_at=? WHERE e164=?", (iso(real), contact["e164"]))
            trace["status"] = "OPTED_OUT"
            return self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, lang, R.t("opted_out", lang)), "OPT_OUT", {},
                                                          {"ignore_opt_out": True})])
        if contact["opted_out_at"]:
            if word in START_WORDS:
                with write_txn(con):
                    con.execute("UPDATE wa_contacts SET opted_out_at=NULL WHERE e164=?", (contact["e164"],))
                trace["status"] = "OPTED_IN"
                return self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, lang, R.t("opted_in", lang)), "OPT_IN", {}, {})])
            trace["status"] = "IGNORED_OPTED_OUT"
            return
        if not contact["notice_sent_at"]:
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET notice_sent_at=? WHERE e164=?", (iso(real), contact["e164"]))
            nl = nlu.detect_lang(msg.text or "", lang)            # read-only: the notice in the language they wrote in
            out.append((self._text(msg.from_e164, nl, R.t("privacy_notice", nl)), "PRIVACY_NOTICE", {}, {}))

        # ---- SAFETY freeze: no scheduling talk until a person clears it
        frozen = S.is_frozen(con, did)
        if frozen and not nlu.extract(msg.text or "", as_of).safety:
            S.add_followup(con, frozen, msg.text or f"[{msg.type}]", as_of)
            trace["status"] = "SAFETY_HOLD"
            out.append((self._text(msg.from_e164, lang, R.t("safety_hold", lang, ref=frozen)), "SAFETY_HOLD", {}, {}))
            return self._queue(con, as_of, real, trace, out)

        # ---- a voice note: transcribe it (ElevenLabs) and handle the words like a typed message
        voice_in = False
        if (msg.type == "media" and self.voice and self.fetch_media and msg.media_urls
                and any((t or "").startswith("audio/") for t in msg.media_types)):
            try:
                audio, ctype = self.fetch_media(msg.media_urls[0])
                text, vlang = self.voice.transcribe(audio, ctype)
                msg.text, msg.type, voice_in = text, "text", True
                trace["voice_in"] = {"chars": len(text), "lang": vlang}
                with write_txn(con):
                    con.execute("UPDATE wa_inbound SET body=?, msg_type='voice' WHERE message_sid=?", (text, msg.message_id))
            except V.VoiceError as e:
                trace["voice_error"] = str(e)[:160]     # falls through to "please type" below

        # ---- types the engine cannot read
        if msg.type in ("media", "other", "location"):
            return self._non_text(con, msg, drv, contact, lang, as_of, real, trace, out)
        if contact["unsupported_streak"]:
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET unsupported_streak=0 WHERE e164=?", (contact["e164"],))

        # ---- offer binding
        b = OF.bind(con, did, msg, as_of)
        trace["binding"] = {k: b.get(k) for k in ("action", "why", "offer_id", "expired")}
        if b["action"] == "stale":
            trace["status"] = "STALE_CHOICE"
            out.append(self._resend_live(con, msg, did, lang, b, as_of, real))
            return self._queue(con, as_of, real, trace, out)

        # ---- "low battery / phone may switch off": remembered for UNREACHABLE_HOLD_H; any other message clears it
        unreachable = U.says_unreachable(msg.text)
        U.set_mark(con, contact["e164"], real, unreachable)

        # ---- the engine (unchanged)
        cap = {}
        history = self.store.history(msg.from_e164)
        self.store.append_history(msg.from_e164, "human", b["text"], ts=iso(as_of), wa_id=msg.message_id)
        try:
            res = run_turn(con, drv["phone"], b["text"], as_of, msg.message_id, history=history, llm_fallback=self.llm, capture=cap)
        except Exception as e:
            return self._turn_failed(con, msg, drv, lang, e, as_of, real, trace, out)
        trace.update(state=res.get("state"), tools=res.get("tools"), lang=res.get("lang"))
        trace["status"] = "DUPLICATE_TEXT" if res.get("reply") is None else "REPLIED"
        lang = res.get("lang") or lang

        # ---- SAFETY: freeze on a new safety escalation
        esc = res.get("escalation") or {}
        if any(t.startswith("escalate:SAFETY") for t in res.get("tools") or []):
            loc = contact["last_lat"] is not None and {"lat": contact["last_lat"], "lng": contact["last_lng"], "at": contact["last_location_at"]}
            trace["frozen"] = S.freeze(con, did, esc["escalation_id"], as_of, loc or None)

        # ---- unreachable driver: never ask them to choose; hold the slot and hand it to a coordinator (unreachable.py)
        if unreachable and res.get("reply") and res.get("state") not in ("ESCALATED", "CLOSED"):
            text, ev, uesc = U.handle(con, drv, dict(res, _text=msg.text), as_of)
            with write_txn(con):
                OF.close_all(con, did, as_of)             # a "1" typed later must not book an option never shown
            trace["status"] = "UNREACHABLE_HOLD"
            trace["escalation"] = uesc["escalation_id"]
            out.append((self._text(msg.from_e164, lang, text), "DRIVER_REPLY", ev, {}))
            self.store.append_history(msg.from_e164, "ai", text, ts=iso(as_of), state=res.get("state"), lang=lang, tools=res.get("tools"))
            if voice_in:
                self._voice_reply(out, msg, lang, text, ev, trace)
            return self._queue(con, as_of, real, trace, out)

        # ---- offers follow the conversation
        used = (msg.message_id, b.get("choice") or (int(b["text"]) if b.get("offer_id") and b["text"].strip().isdigit() else None))
        oid, opts = OF.after_turn(con, did, res, as_of, real, used=used)
        if res.get("reply"):
            m = self._render(msg.from_e164, lang, res["reply"], oid, opts, cap.get("evidence") or {}, trace)
            out.append((m, "DRIVER_REPLY", cap.get("evidence") or {}, {}))
            self.store.append_history(msg.from_e164, "ai", res["reply"], ts=iso(as_of), state=res.get("state"), lang=lang, tools=res.get("tools"))
            if voice_in:
                self._voice_reply(out, msg, lang, res["reply"], cap.get("evidence") or {}, trace)
        self.store.save_driver(msg.from_e164, {"driver_id": did, "shipment_id": res.get("shipment_id"), "state": res.get("state"),
                                               "lang": lang, "offer_id": oid, "last_seen": iso(as_of)})
        if out:
            self._queue(con, as_of, real, trace, out)

    def _voice_reply(self, out, msg, lang, text, evidence, trace):
        """The same (already guarded) reply, spoken, sent after the text. On any failure the text alone goes out."""
        base = self.s.twilio_webhook_base_url
        if not (self.voice and self.media_dir and base):
            return
        try:
            audio, ext, _ = self.voice.speak(text, lang)
            name = V.save_reply_audio(self.media_dir, audio, ext)
        except (V.VoiceError, OSError) as e:
            trace["voice_out_error"] = str(e)[:160]
            return
        trace["voice_out"] = ext
        m = OutboundMessage(to_e164=normalise_phone(msg.from_e164), kind="voice", body=text, lang=lang,
                            media_url=f"{base}/media/{name}")
        out.append((m, "DRIVER_VOICE_REPLY", evidence, {}))

    def _render(self, to, lang, body, offer_id, opts, evidence, trace):
        """Reply -> OutboundMessage. Options become buttons (<=3) or a list (4-10), ids bound to the offer."""
        m = OutboundMessage(to_e164=normalise_phone(to), kind="text", body=body, lang=lang, offer_id=offer_id)
        if not opts:
            return m
        options = [Option(id=o["id"], title=o["title"][:C.TWILIO_QR_TITLE_MAX if len(opts) <= C.TWILIO_QR_MAX_BUTTONS else C.TWILIO_LIST_ITEM_MAX])
                   for o in opts]
        bad = R.guard_options([o.__dict__ for o in options], evidence)
        if bad:                                             # numbered text only; the body was already guarded by the engine
            trace.setdefault("guard", []).extend(bad)
            return m
        m.options = options
        m.kind = "choices" if len(options) <= C.TWILIO_QR_MAX_BUTTONS else "list" if len(options) <= C.TWILIO_LIST_MAX_ITEMS else "text"
        return m

    def _resend_live(self, con, msg, did, lang, b, as_of, real):
        lv = b.get("live")
        if lv:
            with write_txn(con):                            # several live lists: keep only the newest
                con.execute("UPDATE wa_offers SET status='SUPERSEDED', closed_at=? WHERE driver_id=? AND status='LIVE' AND offer_id<>?",
                            (iso(as_of), did, lv["offer_id"]))
            body = R.t("options_changed", lang) + lv["body"]
            opts = json.loads(lv["options_json"])
            ev = R.evidence_from_text(lv["body"])
            m = self._render(msg.from_e164, lang, body, lv["offer_id"], opts, ev, {})
            return (m, "STALE_CHOICE_RESEND", ev, {})
        return (self._text(msg.from_e164, lang, R.t("offer_closed", lang)), "STALE_CHOICE_CLOSED", {}, {})

    def _non_text(self, con, msg, drv, contact, lang, as_of, real, trace, out):
        if msg.type == "location":
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET last_lat=?, last_lng=?, last_location_at=?, unsupported_streak=0 WHERE e164=?",
                            (msg.lat, msg.lng, iso(as_of), contact["e164"]))
            trace["status"] = "LOCATION_NOTED"
            out.append((self._text(msg.from_e164, lang, R.t("location_noted", lang)), "LOCATION_NOTED", {}, {}))
            return self._queue(con, as_of, real, trace, out)
        streak = contact["unsupported_streak"] + 1
        with write_txn(con):
            con.execute("UPDATE wa_contacts SET unsupported_streak=? WHERE e164=?", (streak, contact["e164"]))
        if streak < C.UNSUPPORTED_BEFORE_HANDOFF:
            trace["status"] = "UNSUPPORTED_TYPE"
            out.append((self._text(msg.from_e164, lang, R.t("please_type", lang)), "PLEASE_TYPE", {}, {}))
            return self._queue(con, as_of, real, trace, out)
        conv = one(con, "SELECT shipment_id FROM conversations WHERE driver_id=?", drv["driver_id"])
        with write_txn(con):
            esc = E.escalate(con, "HUMAN_REQUESTED", "MEDIUM", conv and conv["shipment_id"], drv["driver_id"], as_of,
                             {"driver_sent": f"{streak} {msg.type} messages in a row ({', '.join(msg.media_types) or msg.type})",
                              "why": "the assistant reads typed text only"})
            con.execute("UPDATE wa_contacts SET unsupported_streak=0 WHERE e164=?", (contact["e164"],))
        trace["status"] = "UNSUPPORTED_HANDOFF"
        by = ts(esc["respond_by"])
        out.append((self._text(msg.from_e164, lang, R.t("human", lang, who=esc["routed_to"]["name"], by=ampm(by))), "HANDOFF",
                    {"times": {ampm(by)}, "docks": set(), "confirmed": False}, {}))
        return self._queue(con, as_of, real, trace, out)

    def _turn_failed(self, con, msg, drv, lang, e, as_of, real, trace, out):
        trace["status"] = "TURN_FAILED"
        trace["error"] = type(e).__name__
        frozen_guard = "SAFETY_FREEZE" in str(e)
        conv = one(con, "SELECT shipment_id FROM conversations WHERE driver_id=?", drv["driver_id"])
        with write_txn(con):
            alarm(con, "TURN_FAILED", msg.message_id, {"error": type(e).__name__, "safety_freeze": frozen_guard}, real, as_of)
            E.escalate(con, "HUMAN_REQUESTED", "HIGH", conv and conv["shipment_id"], drv["driver_id"], as_of,
                       {"driver_said": msg.text, "why": "the turn could not finish" + (" (a frozen safety booking blocked it)" if frozen_guard else "")})
        out.append((self._text(msg.from_e164, lang, R.t("turn_error", lang)), "TURN_ERROR", {}, {}))
        self._queue(con, as_of, real, trace, out)

    # ------------------------------------------------------------ messages not triggered by the driver
    def notify_driver(self, con, driver_id, text, evidence, as_of, kind="DRIVER_NOTICE", order=None):
        """Coordinator decisions and swaps. Free-form inside the 24 h window, else the booking-update template."""
        real = self.clock.real_now()
        d = one(con, "SELECT phone FROM drivers WHERE driver_id=?", driver_id)
        lang = self._lang(con, driver_id)
        to = LK.active_number_for(con, driver_id, real) or d["phone"]          # a linked new number wins while it lasts
        m = OutboundMessage(to_e164=normalise_phone(to), kind="text", body=text, lang=lang,
                            template_ref="driver_booking_update", template_vars={"order": order or "-"})
        trace = {"trace_id": uuid.uuid4().hex, "outbox_ids": [], "kind": kind, "clock_mode": self.clock.mode, "as_of": iso(as_of)}
        self._queue(con, as_of, real, trace, [(m, kind, evidence, {})])
        with write_txn(con):
            con.execute("INSERT INTO channel_traces VALUES (?,?,?,?)", (trace["trace_id"], iso(real), "notify", json.dumps(trace, default=str)))
        with write_txn(con):
            OF.close_all(con, driver_id, as_of)
        if self.on_enqueued:
            self.on_enqueued()
        return trace

    def proactive_checkin(self, con, shipment_id, as_of):
        """Health monitor 'are you on track?'. Always a template (business-initiated). Frozen drivers are not nudged."""
        real = self.clock.real_now()
        s = one(con, """SELECT s.*, d.phone, d.driver_name, d.driver_id AS did FROM shipments s JOIN drivers d ON d.driver_id=s.driver_id
                        WHERE s.shipment_id=?""", shipment_id)
        if not s:
            return {"status": "NO_SHIPMENT"}
        if S.frozen_shipment(con, shipment_id):
            return {"status": "FROZEN"}
        to = LK.active_number_for(con, s["did"], real) or s["phone"]
        if U.is_unreachable(con, normalise_phone(to), real):
            return {"status": "UNREACHABLE"}                    # the coordinator already has the case; no nudge to a dead phone
        from .capacity import current_appointment
        from .workflow import facility_short
        cur = current_appointment(con, shipment_id)
        slot = ampm(ts(cur["slot_start_ts"])) if cur else "-"
        values = {"name": s["driver_name"].split()[0], "order": s["order_reference"],
                  "facility": facility_short(con, s["destination_facility_id"]), "slot": slot, "slot_day": "today"}
        from . import templates as TPL
        lang = self._lang(con, s["did"])
        m = OutboundMessage(to_e164=normalise_phone(to), kind="template", template_ref="driver_checkin", template_vars=values,
                            body=TPL.preview("driver_checkin", values, lang), lang=lang, proactive=True)
        trace = {"trace_id": uuid.uuid4().hex, "outbox_ids": [], "kind": "proactive", "clock_mode": self.clock.mode, "as_of": iso(as_of)}
        self._queue(con, as_of, real, trace, [(m, "PROACTIVE_CHECKIN", {"times": {slot}, "docks": set(), "confirmed": False}, {})])
        if self.on_enqueued:
            self.on_enqueued()
        return {"status": "QUEUED", "outbox_ids": trace["outbox_ids"]}
