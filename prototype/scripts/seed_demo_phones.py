"""Map selected drivers and facility contacts to the team's sandbox-joined handsets, on a COPY of the snapshot.

The supplied snapshot (data/setuhaul_freight_operations.db) is never opened for writing. The copy is
rewound to 08:20 like the scenario run; the app replays the fixed world events as its snapshot clock passes them.

Mapping comes from a CSV (id,phone) or SEED_PHONES="DRV013=+9198xxxxxxxx,CON003=+9197xxxxxxxx".
ids are driver ids (DRV...) or facility contact ids (CON...). drivers.phone is UNIQUE, and a number that
belongs to a driver is always treated as that driver, so use a different handset for every role.

    .venv/bin/python -I scripts/seed_demo_phones.py --csv my_handsets.csv --out out/demo_world.db
    SETUHAUL_DB=out/demo_world.db CHANNEL=twilio CLOCK_MODE=snapshot SNAPSHOT_ANCHOR=2026-08-04T09:15:00+05:30 ...
"""
import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import db, chan_db, demo_world                    # noqa: E402
from setuhaul.session_store import normalise_phone           # noqa: E402
from setuhaul.tracing import mask                            # noqa: E402


def load(args):
    pairs = []
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row and not row[0].startswith("#") and row[0].strip().lower() != "id":
                    pairs.append((row[0].strip(), row[1].strip()))
    for part in filter(None, (os.environ.get("SEED_PHONES") or "").split(",")):
        k, _, v = part.partition("=")
        pairs.append((k.strip(), v.strip()))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--out", default=os.path.join(HERE, "out", "demo_world.db"))
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    if os.path.samefile(os.path.dirname(out), os.path.dirname(db.SNAPSHOT)) and os.path.basename(out) == os.path.basename(db.SNAPSHOT):
        sys.exit("refusing to write the supplied snapshot; choose another --out")
    pairs = load(args)
    if not pairs:
        sys.exit("no mapping: pass --csv or set SEED_PHONES")
    nums = [normalise_phone(p) for _, p in pairs]
    if len(set(nums)) != len(nums):
        sys.exit("each handset may be mapped to one id only (drivers.phone is UNIQUE; a driver number always wins)")
    path = db.fresh_copy(out)
    con = db.connect(path)
    chan_db.ensure(con)
    demo_world.start(con)
    taken = {normalise_phone(r[0]): r[1] for r in con.execute("SELECT phone, driver_id FROM drivers")}
    for ident, phone in pairs:
        e = normalise_phone(phone)
        if ident.startswith("DRV"):
            if e in taken and taken[e] != ident:
                sys.exit(f"{mask(e)} already belongs to {taken[e]}")
            n = con.execute("UPDATE drivers SET phone=? WHERE driver_id=?", (e, ident)).rowcount
        elif ident.startswith("CON"):
            n = con.execute("UPDATE facility_contacts SET phone=? WHERE contact_id=?", (e, ident)).rowcount
        else:
            sys.exit(f"unknown id {ident} (use DRV... or CON...)")
        if n != 1:
            sys.exit(f"no row for {ident}")
        print(f"{ident:7s} -> {mask(e)}")
    con.close()
    print(f"\nDemo database: {path}\nStart the app with SETUHAUL_DB={os.path.relpath(path, HERE)}")


if __name__ == "__main__":
    main()
