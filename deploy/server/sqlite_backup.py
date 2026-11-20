"""Consistent online copy of the app database (sqlite3 backup API), then an integrity check.
Runs inside the app container:  python -I - /data/backups/<file>.db < sqlite_backup.py"""
import os, sqlite3, sys
src, dst = os.environ.get("SETUHAUL_DB", "/data/demo_world.db"), sys.argv[1]
a = sqlite3.connect(src, timeout=30)          # the backup API reads a consistent snapshot; WAL-safe
b = sqlite3.connect(dst)
a.backup(b)
ok = b.execute("PRAGMA integrity_check").fetchone()[0]
n = b.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
b.close(); a.close()
print(f"backup {dst}: integrity={ok} appointments={n}")
sys.exit(0 if ok == "ok" else 1)
