#!/bin/sh
# Container start: make sure the demo database exists (a COPY of the supplied snapshot, never the snapshot),
# then run ONE uvicorn worker (SQLite has one writer; the engine uses BEGIN IMMEDIATE).
set -e
DB="${SETUHAUL_DB:-/data/demo_world.db}"
if [ ! -s "$DB" ]; then
  if [ -n "${SEED_PHONES:-}" ]; then
    python -I scripts/seed_demo_phones.py --out "$DB" > /dev/null
    echo "setuhaul: created $DB with handsets mapped"
  else
    python -I -c 'import sys; sys.path.insert(0, "."); from setuhaul import db, chan_db, demo_world
p = db.fresh_copy(sys.argv[1]); c = db.connect(p); chan_db.ensure(c); demo_world.start(c); c.close()' "$DB"
    echo "setuhaul: created $DB (no handsets mapped: set DEMO_PHONES or DEMO_PHONE and run reset-demo.sh)"
  fi
fi
# --proxy-headers: trust X-Forwarded-Proto/For from Caddy (the only thing that can reach port 8000 besides localhost).
exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips '*' --log-level warning
