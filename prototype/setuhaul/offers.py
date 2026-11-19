"""Offer binding: a tap or a typed number always means one specific list the driver was shown.

Every list we send (slot options, "which trip?", early-arrival choice) is an offer with an id.
Button / list ids carry it: "opt:{offer_id}:{n}". A new list for a driver supersedes the old one;
a turn that leaves the choosing states closes it.

    tap on the LIVE offer, n in range      -> the engine gets "n" (commit re-checks feasibility;
                                              after OFFER_VALID_MIN the re-check is all that changes)
    tap on a SUPERSEDED or CLOSED offer    -> nothing is committed; "those options changed" + the live
                                              offer re-sent, or "those options are closed"
    typed bare digit, exactly one LIVE     -> the engine gets it
    typed bare digit, zero or several LIVE -> same as a stale tap
    same MessageSid twice                  -> dropped earlier by dedupe
"""
import json
import re
import secrets

from . import config as C
from .clock import iso, ts, add
from .db import write_txn, one, rows

CHOOSING = ("OPTIONS_SHOWN", "NEED_SHIPMENT")
OPT_ID = re.compile(r"^opt:([0-9a-f]{8}):(\d{1,2})$")
BARE_DIGIT = re.compile(r"^\s*([1-9०-९])\s*[.)]?\s*$")


def option_id(offer_id, n):
    return f"opt:{offer_id}:{n}"


def live(con, driver_id):
    return rows(con, "SELECT * FROM wa_offers WHERE driver_id=? AND status='LIVE' ORDER BY shown_at DESC, rowid DESC", driver_id)


def kind_for(result):
    if result.get("state") == "NEED_SHIPMENT":
        return "SHIPMENT"
    return "EARLY" if any(b.startswith(("Take ", "Keep ")) for b in result.get("buttons") or []) else "SLOTS"


def publish(con, driver_id, result, as_of, real_now):
    """New list shown: supersede the driver's live offers and record this one. Inside the caller's transaction."""
    oid = secrets.token_hex(4)
    con.execute("UPDATE wa_offers SET status='SUPERSEDED', closed_at=? WHERE driver_id=? AND status='LIVE'", (iso(as_of), driver_id))
    opts = [{"n": i, "id": option_id(oid, i), "title": b} for i, b in enumerate(result.get("buttons") or [], 1)]
    con.execute("""INSERT INTO wa_offers(offer_id, driver_id, kind, conv_state, options_json, body, shown_at, expires_at, shown_real, status)
                   VALUES (?,?,?,?,?,?,?,?,?, 'LIVE')""",
                (oid, driver_id, kind_for(result), result["state"], json.dumps(opts, ensure_ascii=False), result.get("reply") or "",
                 iso(as_of), iso(add(as_of, C.OFFER_VALID_MIN)), iso(real_now)))
    return oid, opts


def close_all(con, driver_id, as_of, used_by=None, choice=None):
    con.execute("""UPDATE wa_offers SET status='CLOSED', closed_at=?, used_by_sid=COALESCE(used_by_sid, ?), used_choice=COALESCE(used_choice, ?)
                   WHERE driver_id=? AND status='LIVE'""", (iso(as_of), used_by, choice, driver_id))


def after_turn(con, driver_id, result, as_of, real_now, used=None):
    """Keep offers in step with the conversation. Returns (offer_id, options) if a new list was shown."""
    if not result or result.get("reply") is None:
        return None, []
    with write_txn(con):
        if result.get("state") in CHOOSING and result.get("buttons"):
            return publish(con, driver_id, result, as_of, real_now)
        if result.get("state") not in CHOOSING:
            close_all(con, driver_id, as_of, *(used or (None, None)))
    return None, []


def bind(con, driver_id, msg, as_of):
    """Decide what the engine sees for this inbound message.

    Returns {"action": "run", "text": ..., "offer_id": ..., "expired": bool}
         or {"action": "stale", "why": ..., "live": <offer row or None>}
    """
    lv = live(con, driver_id)
    if msg.choice_id:
        m = OPT_ID.match(msg.choice_id.strip())
        if not m:                                          # a button we did not make (e.g. a template's quick reply)
            return {"action": "run", "text": msg.text, "offer_id": None, "expired": False}
        oid, n = m.group(1), int(m.group(2))
        o = one(con, "SELECT * FROM wa_offers WHERE offer_id=? AND driver_id=?", oid, driver_id)
        if not o:
            return {"action": "stale", "why": "UNKNOWN_OFFER", "live": lv[0] if len(lv) == 1 else None}
        if o["status"] != "LIVE":
            return {"action": "stale", "why": "OFFER_" + o["status"], "live": lv[0] if lv else None}
        if not 1 <= n <= len(json.loads(o["options_json"])):
            return {"action": "stale", "why": "OPTION_OUT_OF_RANGE", "live": o}
        return {"action": "run", "text": str(n), "offer_id": oid, "expired": as_of > ts(o["expires_at"]), "choice": n}
    if BARE_DIGIT.match(msg.text or ""):
        if len(lv) == 1:
            return {"action": "run", "text": msg.text, "offer_id": lv[0]["offer_id"], "expired": as_of > ts(lv[0]["expires_at"])}
        return {"action": "stale", "why": "NO_SINGLE_LIVE_OFFER" if lv else "NO_LIVE_OFFER", "live": lv[0] if lv else None}
    return {"action": "run", "text": msg.text, "offer_id": None, "expired": False}
