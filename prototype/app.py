"""SetuHaul HTTP entry point (FastAPI).

Core activity: receive an HTTP request, prove who sent it, load the session, run one
turn through the graph, send the reply, save the session. No booking logic lives here.

Driver side (WhatsApp Business Cloud API)
    GET  /webhook/whatsapp          Meta's subscription check (hub.challenge)
    POST /webhook/whatsapp          inbound messages; body signed by Meta (X-Hub-Signature-256)
Coordinator side (console)
    POST /auth/dev-login            offline demo sign-in by email or mobile -> token
    POST /auth/session              register a Cognito token (production)
    POST /auth/logout
    GET  /console/handoffs          open escalations for my facility
    GET  /console/holds             holds waiting for my approval
    POST /console/holds/{id}/decide approve or reject; the driver is told at once
    GET  /console/board             booking health for my facility
Demo and ops
    POST /dev/driver-message        send a driver message without Meta (SETUHAUL_DEV=1 only)
    GET  /health

Run:  pip install -r requirements-prod.txt
      SETUHAUL_DEV=1 uvicorn app:app --reload            (from the prototype folder)

Environment
    SETUHAUL_DB             database path (default: a fresh copy of the snapshot in out/app_world.db)
    SETUHAUL_REWIND_TO      e.g. 2026-08-04T08:20:00+05:30, to replay the snapshot day (demo)
    REDIS_URL               sessions in Redis; in-memory store if unset
    ANTHROPIC_API_KEY       turns on the LLM fallback reader (llm.py); rules only if unset
    WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN, WHATSAPP_TOKEN, WHATSAPP_PHONE_ID
    COGNITO_REGION, COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID   (production coordinator sign-in)
    SETUHAUL_DEV_SECRET     signing key for demo tokens
    LANGSMITH_TRACING=true, LANGSMITH_API_KEY                 (tracing)
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from setuhaul import db, capacity, planner, world
from setuhaul import auth as A
from setuhaul.clock import IST, ts, iso
from setuhaul.graph import run_turn
from setuhaul.llm import make_fallback
from setuhaul.session_store import SessionStore, normalise_phone
from setuhaul.workflow import notify_decision

HERE = os.path.dirname(os.path.abspath(__file__))
DEV = os.environ.get("SETUHAUL_DEV") == "1"


def _init_db():
    path = os.environ.get("SETUHAUL_DB")
    if not path:
        path = db.fresh_copy(os.path.join(HERE, "out", "app_world.db"))
        rewind = os.environ.get("SETUHAUL_REWIND_TO")
        if rewind:
            con = db.connect(path)
            world.rewind(con, ts(rewind))
            con.close()
    return path


DB_PATH = _init_db()
STORE = SessionStore.from_env()
LLM = make_fallback()                      # None unless ANTHROPIC_API_KEY is set
app = FastAPI(title="SetuHaul driver-exception service")


def _con():
    return db.connect(DB_PATH)             # one connection per request; SQLite WAL / Postgres pool in production


def _as_of(override=None, epoch=None):
    if override:
        return ts(override)
    if epoch:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(IST)
    return datetime.now(IST).replace(microsecond=0)


# ---------------------------------------------------------------- outbound WhatsApp
def send_whatsapp(con, to_phone, text, as_of):
    """Record in the outbox (always), then deliver through the Cloud API when configured."""
    capacity.outbox(con, as_of, "WHATSAPP", to_phone, "DRIVER_REPLY", text)
    token, phone_id = os.environ.get("WHATSAPP_TOKEN"), os.environ.get("WHATSAPP_PHONE_ID")
    if not (token and phone_id):
        return "QUEUED"
    req = urllib.request.Request(
        f"https://graph.facebook.com/v21.0/{phone_id}/messages",
        data=json.dumps({"messaging_product": "whatsapp", "to": normalise_phone(to_phone).lstrip("+"),
                         "type": "text", "text": {"body": text}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return "SENT"
    except Exception:
        return "FAILED"                    # the outbox row stays QUEUED for a retry worker


# ---------------------------------------------------------------- one driver turn
def driver_turn(phone, text, as_of, external_id=None):
    """Identity -> rate limit -> dedupe -> lock -> history -> graph -> reply -> session."""
    if not STORE.allow(phone):
        return {"state": "RATE_LIMITED", "reply": None, "tools": ["rate_limit:BLOCKED"]}
    if not STORE.first_time(external_id):
        return {"state": "DUPLICATE", "reply": None, "tools": [f"dedupe:SUPPRESSED ({external_id})"]}
    con = _con()
    try:
        drv = A.driver_for_phone(con, phone)
        if not drv:
            return {"state": "UNKNOWN_SENDER", "reply": None, "tools": ["identify:REJECTED"]}
        tok = STORE.lock(phone)
        if not tok:
            return {"state": "BUSY", "reply": None, "tools": ["lock:BUSY (previous turn still running)"]}
        try:
            history = STORE.history(phone)
            STORE.append_history(phone, "human", text, ts=iso(as_of), wa_id=external_id)
            out = run_turn(con, drv["phone"], text, as_of, external_id, history=history, llm_fallback=LLM)
            if out.get("reply"):
                out["delivery"] = send_whatsapp(con, drv["phone"], out["reply"], as_of)
                STORE.append_history(phone, "ai", out["reply"], ts=iso(as_of), state=out.get("state"),
                                     lang=out.get("lang"), tools=out.get("tools"))
            STORE.save_driver(phone, {"driver_id": drv["driver_id"], "shipment_id": out.get("shipment_id"),
                                      "state": out.get("state"), "lang": out.get("lang"), "last_seen": iso(as_of)})
            return out
        finally:
            STORE.unlock(phone, tok)
    finally:
        con.close()


@app.get("/webhook/whatsapp", response_class=PlainTextResponse)
def wa_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == os.environ.get("WHATSAPP_VERIFY_TOKEN"):
        return q.get("hub.challenge", "")
    raise HTTPException(403, "verification failed")


@app.post("/webhook/whatsapp")
async def wa_inbound(request: Request, x_hub_signature_256: str = Header(default=""),
                     x_setuhaul_as_of: str = Header(default="")):
    body = await request.body()
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if secret and not A.verify_whatsapp_signature(body, x_hub_signature_256, secret):
        raise HTTPException(401, "bad signature")
    if not secret and not DEV:
        raise HTTPException(500, "WHATSAPP_APP_SECRET not configured")
    payload = json.loads(body or b"{}")
    results = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                if m.get("type") != "text":
                    continue                # voice notes and images: a later release
                as_of = _as_of(x_setuhaul_as_of if DEV else None, m.get("timestamp"))
                results.append(driver_turn(m["from"], m["text"]["body"], as_of, m.get("id")))
    return {"processed": len(results), "results": results}


@app.post("/dev/driver-message")
def dev_driver_message(msg: dict):
    if not DEV:
        raise HTTPException(404)
    return driver_turn(msg["phone"], msg["text"], _as_of(msg.get("as_of")), msg.get("external_id"))


# ---------------------------------------------------------------- coordinator auth
def _coordinator(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "sign in first")
    try:
        return A.verify_coordinator(authorization[7:], STORE)
    except A.AuthError as e:
        raise HTTPException(401, str(e))


@app.post("/auth/dev-login")
def dev_login(body: dict):
    if not DEV:
        raise HTTPException(404)
    con = _con()
    try:
        who = A.coordinator_for(con, body.get("user_id", ""))
    finally:
        con.close()
    if not who:
        raise HTTPException(403, "not a coordinator")
    token, claims = A.issue_dev_token(who, os.environ.get("SETUHAUL_DEV_SECRET", "dev-only-secret"))
    STORE.start_coordinator(who["user_id"], claims, A.TOKEN_TTL_S)
    return {"token": token, "expires_at": claims["exp"], "role": who["role"], "facility_id": who["facility_id"]}


@app.post("/auth/session")
def cognito_session(authorization: str = Header(default="")):
    """Production: the console signs in with Cognito, then registers the token here."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401)
    try:
        claims = A.verify_cognito(authorization[7:])
    except (A.AuthError, KeyError) as e:
        raise HTTPException(401, str(e))
    con = _con()
    try:
        who = A.coordinator_for(con, claims.get("email") or claims.get("phone_number", ""))
    finally:
        con.close()
    if not who:
        raise HTTPException(403, "not a coordinator")
    STORE.start_coordinator(who["user_id"], dict(who, jti=claims["jti"]), max(60, claims["exp"] - int(datetime.now().timestamp())))
    return who


@app.post("/auth/logout")
def logout(authorization: str = Header(default="")):
    c = _coordinator(authorization)
    STORE.logout(c["jti"])
    return {"ok": True}


# ---------------------------------------------------------------- coordinator console
@app.get("/console/handoffs")
def handoffs(authorization: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        rs = con.execute("""SELECT e.* FROM escalations e LEFT JOIN shipments s USING(shipment_id)
                            WHERE e.status='OPEN' AND COALESCE(s.destination_facility_id, ?)=? ORDER BY e.sla_due_at""",
                         (c["facility_id"], c["facility_id"])).fetchall()
        return [dict(r, handoff=json.loads(r["handoff_json"])) for r in rs]
    finally:
        con.close()


@app.get("/console/holds")
def holds(authorization: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        rs = con.execute("""SELECT h.* FROM slot_holds h JOIN appointment_slots s USING(slot_id)
                            WHERE h.hold_status='PENDING_APPROVAL' AND s.facility_id=?""", (c["facility_id"],)).fetchall()
        return [dict(r) for r in rs]
    finally:
        con.close()


@app.post("/console/holds/{hold_id}/decide")
def decide(hold_id: str, body: dict, authorization: str = Header(default=""), x_setuhaul_as_of: str = Header(default="")):
    c = _coordinator(authorization)
    as_of = _as_of(x_setuhaul_as_of if DEV else None)
    con = _con()
    try:
        h = con.execute("""SELECT h.*, s.facility_id FROM slot_holds h JOIN appointment_slots s USING(slot_id)
                           WHERE hold_id=?""", (hold_id,)).fetchone()
        if not h or h["facility_id"] != c["facility_id"]:
            raise HTTPException(404, "no such hold at your facility")
        res = capacity.decide(con, hold_id, bool(body.get("approve")), c["name"], as_of, body.get("note", ""))
        if res.get("code") == "HOLD_NOT_LIVE":
            return res
        note = notify_decision(con, h["driver_id"], res, as_of)
        phone = con.execute("SELECT phone FROM drivers WHERE driver_id=?", (h["driver_id"],)).fetchone()["phone"]
        note["delivery"] = send_whatsapp(con, phone, note["reply"], as_of)
        STORE.append_history(phone, "ai", note["reply"], ts=iso(as_of), state=note["state"], tools=note["tools"])
        return {"decision": res.get("code"), "driver_told": note}
    finally:
        con.close()


@app.get("/console/board")
def board(authorization: str = Header(default=""), x_setuhaul_as_of: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        return planner.health(con, c["facility_id"], _as_of(x_setuhaul_as_of if DEV else None))
    finally:
        con.close()


@app.get("/health")
def health():
    return {"ok": True, "sessions": STORE.backend, "llm_fallback": LLM is not None,
            "db": os.path.basename(DB_PATH)}
