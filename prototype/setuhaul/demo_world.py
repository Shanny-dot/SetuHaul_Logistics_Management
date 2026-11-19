"""The 4 August world for live snapshot mode (CLOCK_MODE=snapshot).

The same fixed world events as run_scenarios.py (copied, so the scenario runner stays untouched),
applied when the snapshot clock passes their time. Driver messages are NOT scripted here: they
come from real handsets or the simulator. Each event is applied once (demo_world_applied).
"""
from . import world as Wd, capacity as K
from .clock import ts, iso
from .db import write_txn, one

REWIND_TO = "08:20"


def D(hm):
    return ts(f"2026-08-04T{hm}:00+05:30")


EVENTS = [
    ("08:30", "ETA003", lambda c: Wd.eta_update(c, "ETA003", "SHP1004", "DRIVER_DECLARED", "DRV004", D("09:20"), "HIGH", "TRAFFIC", "Traffic at Tonk Road.", D("08:30"))),
    ("08:42", "unload SHP1001", lambda c: Wd.unload_done(c, "SHP1001", D("08:42"))),
    ("08:50", "ETA005", lambda c: Wd.eta_update(c, "ETA005", "SHP1006", "DRIVER_DECLARED", "DRV006", D("10:50"), "MEDIUM", "TRAFFIC", "Traffic after Shahpura.", D("08:50"))),
    ("09:05", "gate SHP1005", lambda c: Wd.gate_in(c, "SHP1005", D("09:05"), "LATE")),
    ("09:15", "DEVT001", lambda c: Wd.dock_event(c, "DEVT001", "DOCK-JAI-D3", "BREAKDOWN", D("09:15"), D("13:00"), "Hydraulic dock leveller failure", D("09:16"))),
    ("09:25", "gate SHP1004", lambda c: Wd.gate_in(c, "SHP1004", D("09:25"), "LATE")),
    ("09:28", "unload SHP1002", lambda c: Wd.unload_done(c, "SHP1002", D("09:28"))),
    ("09:38", "dock SHP1004", lambda c: Wd.dock_in(c, "SHP1004", "DOCK-JAI-D2", D("09:38"))),
    ("09:55", "ETA012", lambda c: Wd.eta_update(c, "ETA012", "SHP1017", "DRIVER_DECLARED", "DRV001", D("12:45"), "LOW", "TRAFFIC",
                                                "Driver expects arrival between 12:30 and 13:00.", D("09:55"), window=(D("12:30"), D("13:00")))),
    ("10:10", "cancel SHP1019", lambda c: K.cancel_shipment(c, "SHP1019", D("10:10"), "Shipment cancelled")),
    ("12:20", "depart SHP1015", lambda c: Wd.depart(c, "SHP1015", D("12:20"))),
]


def start(con):
    """Rewind a fresh copy to 08:20 (as the scenario run does)."""
    return Wd.rewind(con, D(REWIND_TO))


def apply_due(con, as_of):
    """Apply every fixed event whose time has come. Returns the keys applied now."""
    done = []
    if iso(as_of)[:10] != "2026-08-04":
        return done
    for hm, key, fn in EVENTS:
        if D(hm) > as_of:
            break
        if one(con, "SELECT 1 FROM demo_world_applied WHERE event_key=?", key):
            continue
        fn(con)
        with write_txn(con):
            con.execute("INSERT OR IGNORE INTO demo_world_applied VALUES (?,?)", (key, iso(as_of)))
        done.append(key)
    return done
