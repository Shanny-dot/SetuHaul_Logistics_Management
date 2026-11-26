"""A driver says their phone is about to die ("low battery, might be 10 min late").

Core activity: never ask an unreachable driver to choose. The engine still reads the message and records the
new ETA (nothing in its decisions changes); this layer then:
  * keeps the current booking and tells the driver until when it is held (slot start + the facility's late grace),
    with the gate instruction ("give your truck number"), instead of slot options they cannot answer;
  * closes any list just shown, so a bare "1" typed later cannot book an option the driver never saw;
  * hands the case to a coordinator (DRIVER_UNREACHABLE: HIGH when the slot no longer fits or there is none,
    MEDIUM when it still works), with the options, the hold time and the carrier dispatcher's phone;
  * marks the contact unreachable for UNREACHABLE_HOLD_H, so failed deliveries fall back to a call to the
    carrier's dispatcher, not to the dead phone, and proactive nudges are not sent.
Any later message from the driver without those words clears the mark: they are reachable again.
"""
from datetime import timedelta

from . import capacity as K
from . import config as C
from . import escalation as E
from . import timeline as T
from .clock import ts, add, ampm, iso
from .db import one, write_txn


def says_unreachable(text):
    t = (text or "").lower()
    return any(w in t for w in C.UNREACHABLE_WORDS)


def is_unreachable(con, e164, real_now):
    r = one(con, "SELECT unreachable_until FROM wa_contacts WHERE e164=?", e164)
    return bool(r and r["unreachable_until"] and ts(r["unreachable_until"]) > real_now)


def set_mark(con, e164, real_now, on):
    with write_txn(con):
        con.execute("UPDATE wa_contacts SET unreachable_until=? WHERE e164=?",
                    (iso(real_now + timedelta(hours=C.UNREACHABLE_HOLD_H)) if on else None, e164))


def carrier_phone(con, driver_id):
    r = one(con, """SELECT c.contact_phone FROM shipments s JOIN carriers c ON c.carrier_id=s.carrier_id
                    WHERE s.driver_id=? ORDER BY s.updated_at DESC LIMIT 1""", driver_id)
    return r and r["contact_phone"]


def handle(con, drv, result, as_of):
    """Called after the engine's turn. Returns (text, evidence, escalation) to send instead of / after its reply."""
    sid = result.get("shipment_id")
    cur = K.current_appointment(con, sid) if sid else None
    ship = one(con, "SELECT * FROM shipments WHERE shipment_id=?", sid) if sid else None
    lang = result.get("lang") or "en"
    from . import replies as R
    ev = {"docks": set(), "times": set(), "confirmed": False}
    if cur and ship:
        fac = one(con, "SELECT * FROM facilities WHERE facility_id=?", ship["destination_facility_id"])
        grace = int(T.facility_rules(con, fac["facility_id"]).get("NO_SHOW_GRACE_MIN", fac["checkin_grace_min"]))
        start = ts(cur["slot_start_ts"])
        until = add(start, grace)
        ev["docks"].add(cur["dock_code"])
        ev["times"] |= {ampm(start), ampm(until)}
        text = R.t("unreachable_hold", lang, dock=cur["dock_code"], start=ampm(start), until=ampm(until))
        fits = result.get("state") == "BOOKED"
        if not fits:
            text += "\n" + R.t("unreachable_move", lang)
        urgency = "MEDIUM" if fits else "HIGH"
        hold = {"appointment": cur["appointment_id"], "dock": cur["dock_code"], "slot_start": iso(start), "held_until": iso(until)}
    else:
        text, urgency, hold = R.t("unreachable_nobooking", lang), "HIGH", None
    opts = [{"dock": o.get("dock_code"), "start": o.get("start"), "slot_id": o.get("slot_id")} for o in result.get("options") or []]
    with write_txn(con):
        esc = E.escalate(con, "DRIVER_UNREACHABLE", urgency, sid, drv["driver_id"], as_of, {
            "driver_said": result.get("_text", ""), "why": "driver said their phone may switch off",
            "current_booking": hold, "options_found": opts, "carrier_dispatcher_phone": carrier_phone(con, drv["driver_id"]),
            "action": ("Keep the slot through the grace window. If the truck cannot make it, move it to the best option "
                       "and message the driver; if the driver cannot be reached, call the carrier dispatcher."),
            "driver_told": "slot held until the grace time; give the truck number at the gate"})
    return text, ev, esc
