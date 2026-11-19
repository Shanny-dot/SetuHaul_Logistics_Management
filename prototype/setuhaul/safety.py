"""SAFETY freeze and the missed-SLA chain for escalations.

Freeze: when a driver reports an accident or injury, every live shipment of that driver is frozen
until a person clears it: no scheduling talk with the driver, no auto-release of the booking and no
NO_SHOW marking (database trigger in chan_db.py), no proactive nudges (outbox callers check
is_frozen). The escalation record gets the last known shipment and location.

Missed-SLA chain (every escalation that pages someone by phone/WhatsApp):
    level 0  the contact escalation.route() chose (SAFETY: shift manager)     due = escalation SLA
    level 1+ the next contact in escalation.ROUTES order with a usable phone    due = +SLA again
    last     the duty head (DUTY_HEAD_PHONE)                                    due = +SLA again
    then     ops alarm SLA_CHAIN_EXHAUSTED (once)
An ACK (console, or the staff member replying "ACK ESC-0007" on WhatsApp) stops the chain.
Each page goes out as the staff_page template (proactive) with a phone-call stub alongside.
"""
import json

from . import escalation as E
from . import capacity as K
from . import templates as TPL
from . import replies as R
from .chan_db import alarm
from .channels.base import OutboundMessage
from .clock import iso, ts, add, ampm
from .db import write_txn, one, rows
from .outbox import enqueue
from .session_store import normalise_phone
from .tracing import mask


# ---------------------------------------------------------------- freeze
def is_frozen(con, driver_id):
    r = one(con, "SELECT escalation_id FROM safety_freezes WHERE driver_id=? AND cleared_at IS NULL ORDER BY frozen_at DESC LIMIT 1", driver_id)
    return r["escalation_id"] if r else None


def frozen_shipment(con, shipment_id):
    return one(con, "SELECT 1 FROM safety_freezes WHERE shipment_id=? AND cleared_at IS NULL", shipment_id) is not None


def freeze(con, driver_id, escalation_id, as_of, location=None):
    """Freeze the driver's live shipments and complete the escalation record."""
    with write_txn(con):
        conv = one(con, "SELECT shipment_id FROM conversations WHERE driver_id=?", driver_id)
        live = rows(con, """SELECT shipment_id, order_reference, current_status, destination_facility_id FROM shipments
                            WHERE driver_id=? AND current_status NOT IN ('COMPLETED','CANCELLED') ORDER BY original_eta_ts""", driver_id)
        last = (conv and conv["shipment_id"]) or next((s["shipment_id"] for s in live if s["current_status"] == "IN_TRANSIT"), None) \
            or (live[0]["shipment_id"] if live else None)
        ids = sorted({s["shipment_id"] for s in live} | ({last} if last else set()))
        for sid in ids:
            con.execute("INSERT OR IGNORE INTO safety_freezes VALUES (?,?,?,?,NULL,NULL)", (escalation_id, driver_id, sid, iso(as_of)))
        e = one(con, "SELECT * FROM escalations WHERE escalation_id=?", escalation_id)
        h = json.loads(e["handoff_json"])
        h.update({"last_known_shipment": last, "frozen_shipments": ids, "last_location": location,
                  "frozen": "auto-release, no-show marking and nudges paused until a person clears this case"})
        con.execute("UPDATE escalations SET handoff_json=?, shipment_id=COALESCE(shipment_id, ?) WHERE escalation_id=?",
                    (json.dumps(h, default=str, ensure_ascii=False), last, escalation_id))
        K._audit(con, as_of, "channel:safety", "SAFETY_FREEZE_SET", {"escalation": escalation_id, "driver": driver_id, "shipments": ids})
    return ids


def add_followup(con, escalation_id, text, as_of):
    with write_txn(con):
        e = one(con, "SELECT handoff_json FROM escalations WHERE escalation_id=?", escalation_id)
        h = json.loads(e["handoff_json"])
        h.setdefault("driver_followups", []).append({"at": iso(as_of), "text": text[:500]})
        con.execute("UPDATE escalations SET handoff_json=? WHERE escalation_id=?", (json.dumps(h, default=str, ensure_ascii=False), escalation_id))


def clear(con, escalation_id, who, as_of, note=""):
    """A person clears the freeze (coordinator console). Audited; scheduling, release and nudges resume."""
    with write_txn(con):
        ships = [r["shipment_id"] for r in rows(con, "SELECT shipment_id FROM safety_freezes WHERE escalation_id=? AND cleared_at IS NULL",
                                                escalation_id)]
        n = con.execute("UPDATE safety_freezes SET cleared_at=?, cleared_by=? WHERE escalation_id=? AND cleared_at IS NULL",
                        (iso(as_of), who, escalation_id)).rowcount
        if n:
            con.execute("UPDATE escalations SET status='CLOSED' WHERE escalation_id=? AND reason_code='SAFETY'", (escalation_id,))
            K._audit(con, as_of, who, "SAFETY_FREEZE_CLEARED", {"escalation": escalation_id, "shipments": ships, "note": note})
    return n


# ---------------------------------------------------------------- pages and the chain
def _chain(con, facility_id, reason, as_of):
    """Every contact, in escalation order, that can be paged by phone (vacant roles skipped, as in route())."""
    order = list(E.ROUTES.get(reason, ["WAREHOUSE_COORDINATOR"]))
    if as_of.hour >= E.NIGHT_FROM_HOUR:
        order.insert(0, "NIGHT_SHIFT_COORDINATOR")
    out = []
    for role in order:
        for c in rows(con, "SELECT * FROM facility_contacts WHERE facility_id=? AND contact_role=? AND active_flag=1", facility_id, role):
            if c["contact_name"].lower().startswith("vacant") or not c["phone"]:
                continue
            out.append({"contact_id": c["contact_id"], "name": c["contact_name"], "role": role, "phone": c["phone"]})
    return out


def _facility(con, esc):
    s = esc["shipment_id"] and one(con, "SELECT destination_facility_id AS f FROM shipments WHERE shipment_id=?", esc["shipment_id"])
    return s["f"] if s else "FAC-JAI-01"


def _page(con, esc, level, who, as_of, real_now, settings):
    """Write the page row, queue the staff_page template and a phone-call stub. Inside a transaction."""
    due = add(as_of, E.URGENCY_SLA_MIN[esc["urgency"]])
    summary = {"SAFETY": "driver reported an accident or injury"}.get(esc["reason_code"], esc["reason_code"].replace("_", " ").lower())
    values = {"urgency": esc["urgency"], "ref": esc["escalation_id"], "reason": summary, "by": ampm(due),
              "slot": ampm(due), "slot_day": "today"}
    msg_id = None
    body = TPL.preview("staff_page", values)
    bad = R.guard(body, {"docks": set(), "times": {ampm(due)}, "confirmed": False})
    if bad:                                                   # never expected: the page states only the due time
        raise ValueError("staff page failed the output guard: " + "; ".join(bad))
    if who.get("phone"):
        msg = OutboundMessage(to_e164=normalise_phone(who["phone"]), kind="template", template_ref="staff_page", template_vars=values,
                              body=body, proactive=True)
        msg_id = enqueue(con, as_of, real_now, msg, role="STAFF", kind=f"STAFF_PAGE_L{level}")
        con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
                    (K._next_id(con, "outbox", "msg_id", "OUT-"), "PHONE_CALL", who["phone"], f"STAFF_CALL_L{level}",
                     f"Call stub: {esc['escalation_id']} {esc['reason_code']} ({esc['urgency']})", "STUB", iso(as_of)))
    con.execute("INSERT INTO escalation_pages VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
                (esc["escalation_id"], level, who.get("contact_id"), who.get("name"), who.get("role"),
                 "WHATSAPP_TEMPLATE+PHONE_CALL" if who.get("phone") else "DASHBOARD_ALARM", mask(who.get("phone") or ""),
                 iso(as_of), iso(due), msg_id))
    return msg_id


def page_new(con, as_of, real_now, settings):
    """Level-0 page for every open escalation routed to a phone that has not been paged yet."""
    out = []
    with write_txn(con):
        for esc in rows(con, """SELECT * FROM escalations e WHERE status='OPEN' AND channel LIKE '%PHONE%'
                                AND NOT EXISTS (SELECT 1 FROM escalation_pages p WHERE p.escalation_id=e.escalation_id)"""):
            routed = json.loads(esc["handoff_json"]).get("routed_to", {})
            who = {"contact_id": routed.get("contact_id"), "name": routed.get("name"), "role": routed.get("role"), "phone": routed.get("address")}
            out.append((esc["escalation_id"], _page(con, esc, 0, who, as_of, real_now, settings)))
    return out


def sla_sweep(con, as_of, real_now, settings):
    """Page the next person for every escalation whose current page passed its due time without an ACK."""
    out = []
    with write_txn(con):
        for esc in rows(con, "SELECT * FROM escalations WHERE status='OPEN'"):
            pages = rows(con, "SELECT * FROM escalation_pages WHERE escalation_id=? ORDER BY level", esc["escalation_id"])
            if not pages or any(p["acked_at"] for p in pages):
                continue
            top = pages[-1]
            if ts(top["due_at"]) > as_of or top["role"] == "CHAIN_EXHAUSTED":
                continue
            paged = {p["contact_id"] for p in pages if p["contact_id"]}
            nxt = next((c for c in _chain(con, _facility(con, esc), esc["reason_code"], as_of) if c["contact_id"] not in paged), None)
            if nxt is None and not any(p["role"] == "OPS_DUTY_HEAD" for p in pages):
                nxt = {"contact_id": "DUTY_HEAD", "name": "Duty head", "role": "OPS_DUTY_HEAD", "phone": settings.duty_head_phone or None}
            if nxt is None:
                alarm(con, "SLA_CHAIN_EXHAUSTED", esc["escalation_id"], {"pages": len(pages)}, real_now, as_of)
                con.execute("INSERT INTO escalation_pages VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                            (esc["escalation_id"], top["level"] + 1, None, "Ops dashboard", "CHAIN_EXHAUSTED", "DASHBOARD_ALARM", "",
                             iso(as_of), iso(as_of)))
                out.append((esc["escalation_id"], "EXHAUSTED"))
                continue
            alarm(con, "SLA_MISSED", esc["escalation_id"], {"missed_by": top["name"], "level": top["level"], "next": nxt["name"]}, real_now, as_of)
            _page(con, esc, top["level"] + 1, nxt, as_of, real_now, settings)
            out.append((esc["escalation_id"], nxt["role"]))
    return out


def ack(con, escalation_id, who, as_of):
    with write_txn(con):
        p = one(con, "SELECT * FROM escalation_pages WHERE escalation_id=? ORDER BY level DESC LIMIT 1", escalation_id)
        if not p:
            return False
        con.execute("UPDATE escalation_pages SET acked_at=?, acked_by=? WHERE escalation_id=? AND level=?",
                    (iso(as_of), who, escalation_id, p["level"]))
    return True


def staff_ack(con, contact, text, as_of):
    """Staff replied on WhatsApp. 'ACK ESC-0007' (or plain 'ACK' = their latest open page)."""
    words = (text or "").strip().upper().split()
    if not words or words[0] not in ("ACK", "OK"):
        return None
    ref = words[1] if len(words) > 1 else None
    if not ref:
        p = one(con, """SELECT escalation_id FROM escalation_pages WHERE contact_id=? AND acked_at IS NULL ORDER BY paged_at DESC LIMIT 1""",
                contact["contact_id"])
        ref = p and p["escalation_id"]
    if not ref or not one(con, "SELECT 1 FROM escalations WHERE escalation_id=?", ref):
        return None
    ack(con, ref, contact["contact_name"], as_of)
    return ref
