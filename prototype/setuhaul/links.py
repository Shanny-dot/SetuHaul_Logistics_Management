"""Lost phone: a coordinator links a driver's NEW number, after the carrier confirms it is really them.

Identity still comes only from the verified sender number. A message from an unregistered number is never
believed on its own (no WhatsApp quiz: names, shipment and licence numbers are known to too many people).
Instead a coordinator, after checking with the carrier's dispatcher by phone or in person:
  * links the new number for a limited time (default TEMP_NUMBER_LINK_H = 24 h), or
  * makes it the driver's number permanently.
Both are audited, the link can be revoked, and an expired link stops working by itself. While a link is live,
the driver's replies and coordinator notices go to the new number.
"""
from datetime import timedelta

from . import capacity as K
from . import config as C
from .clock import iso, ts
from .db import one, rows, write_txn
from .session_store import normalise_phone

SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_phone_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id TEXT NOT NULL, e164 TEXT NOT NULL,
    created_real TEXT NOT NULL, expires_real TEXT NOT NULL, linked_by TEXT NOT NULL, reason TEXT,
    revoked_real TEXT, revoked_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_driver_phone_links ON driver_phone_links(e164, revoked_real);
"""


class LinkError(ValueError):
    pass


def _valid(phone):
    e = normalise_phone(phone)
    if not (e.startswith("+") and 10 <= len(e) <= 16):
        raise LinkError("not a valid phone number (use +<country code><number>)")
    return e


def active_driver_for(con, e164, real_now):
    """The driver a live temporary link points this number at, shaped like auth.driver_for_phone(), or None."""
    r = one(con, """SELECT d.driver_id, d.phone, d.driver_status FROM driver_phone_links l JOIN drivers d USING(driver_id)
                    WHERE l.e164=? AND l.revoked_real IS NULL AND l.expires_real>? ORDER BY l.id DESC LIMIT 1""",
            normalise_phone(e164), iso(real_now))
    return dict(r) if r else None


def active_number_for(con, driver_id, real_now):
    r = one(con, """SELECT e164 FROM driver_phone_links WHERE driver_id=? AND revoked_real IS NULL AND expires_real>?
                    ORDER BY id DESC LIMIT 1""", driver_id, iso(real_now))
    return r and r["e164"]


def _taken(con, e164, driver_id):
    for d in rows(con, "SELECT driver_id, phone FROM drivers"):
        if normalise_phone(d["phone"]) == e164 and d["driver_id"] != driver_id:
            return f"that number belongs to {d['driver_id']}"
    for c in rows(con, "SELECT contact_id, phone FROM facility_contacts WHERE phone IS NOT NULL"):
        if normalise_phone(c["phone"]) == e164:
            return "that number belongs to a facility contact"
    return None


def link(con, driver_id, phone, who, reason, real_now, as_of, hours=None):
    e164 = _valid(phone)
    hours = int(hours or C.TEMP_NUMBER_LINK_H)
    if not 1 <= hours <= 72:
        raise LinkError("hours must be between 1 and 72")
    if not one(con, "SELECT 1 FROM drivers WHERE driver_id=?", driver_id):
        raise LinkError("no such driver")
    clash = _taken(con, e164, driver_id)
    if clash:
        raise LinkError(clash)
    with write_txn(con):
        con.execute("UPDATE driver_phone_links SET revoked_real=?, revoked_by=? WHERE driver_id=? AND revoked_real IS NULL",
                    (iso(real_now), who + " (replaced)", driver_id))
        con.execute("""INSERT INTO driver_phone_links(driver_id, e164, created_real, expires_real, linked_by, reason)
                       VALUES (?,?,?,?,?,?)""", (driver_id, e164, iso(real_now), iso(real_now + timedelta(hours=hours)), who, reason))
        K._audit(con, as_of, who, "DRIVER_NUMBER_LINKED", {"driver": driver_id, "hours": hours, "reason": reason,
                                                            "number_last4": e164[-4:]})
    return {"driver_id": driver_id, "expires_real": iso(real_now + timedelta(hours=hours))}


def revoke(con, driver_id, who, real_now, as_of):
    with write_txn(con):
        n = con.execute("UPDATE driver_phone_links SET revoked_real=?, revoked_by=? WHERE driver_id=? AND revoked_real IS NULL",
                        (iso(real_now), who, driver_id)).rowcount
        if n:
            K._audit(con, as_of, who, "DRIVER_NUMBER_UNLINKED", {"driver": driver_id})
    return n


def set_permanent(con, driver_id, phone, who, reason, real_now, as_of):
    e164 = _valid(phone)
    if not one(con, "SELECT 1 FROM drivers WHERE driver_id=?", driver_id):
        raise LinkError("no such driver")
    clash = _taken(con, e164, driver_id)
    if clash:
        raise LinkError(clash)
    with write_txn(con):
        con.execute("UPDATE drivers SET phone=? WHERE driver_id=?", (e164, driver_id))
        con.execute("UPDATE driver_phone_links SET revoked_real=?, revoked_by=? WHERE driver_id=? AND revoked_real IS NULL",
                    (iso(real_now), who + " (made permanent)", driver_id))
        K._audit(con, as_of, who, "DRIVER_NUMBER_CHANGED", {"driver": driver_id, "reason": reason, "number_last4": e164[-4:]})
    return {"driver_id": driver_id, "permanent": True}
