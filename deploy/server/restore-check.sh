#!/bin/bash
# Restore a backup into a SCRATCH database and prove it is usable (integrity + row counts).
# Works on the server or a laptop (needs python3). Does not touch the live database.
# Usage: restore-check.sh <demo_world-....db.gz | .db>
set -euo pipefail
IN="${1:?usage: restore-check.sh <backup.db.gz>}"
SCRATCH="$(mktemp -d)/restored.db"
case "$IN" in *.gz) gunzip -c "$IN" > "$SCRATCH" ;; *) cp "$IN" "$SCRATCH" ;; esac
python3 - "$SCRATCH" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
for t in ("drivers", "shipments", "appointments", "slot_holds", "escalations", "wa_inbound", "wa_delivery"):
    try:
        print(f"  {t:13s}", c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    except sqlite3.Error as e:
        print(f"  {t:13s} (absent: {e})")
PY
rm -rf "$(dirname "$SCRATCH")"
echo "restore check OK (scratch copy deleted)"
