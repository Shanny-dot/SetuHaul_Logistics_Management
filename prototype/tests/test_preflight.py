"""scripts/preflight.py, offline: no network beyond a refused localhost port, no message sent."""
import contextlib
import importlib.util
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))
has = lambda m: importlib.util.find_spec(m) is not None


@unittest.skipUnless(has("twilio"), "twilio not installed")
class Preflight(unittest.TestCase):
    def setUp(self):
        import preflight
        self.P = preflight
        self.dir = tempfile.mkdtemp(prefix="sh-pre-")
        self.db = os.path.join(self.dir, "demo.db")
        env = dict(os.environ, SEED_PHONES="DRV013=+919800000013,DRV004=+919800000004,CON003=+919800000003")
        p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scripts", "seed_demo_phones.py"), "--out", self.db],
                           env=env, capture_output=True, text=True, cwd=HERE)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"CHANNEL": "sim", "TWILIO_AUTH_TOKEN": "tok-" + "x" * 28, "CLOCK_MODE": "snapshot",
                    "SNAPSHOT_ANCHOR": "2026-08-04T09:15:00+05:30", "SETUHAUL_DB": self.db}

    def lines(self, rep, area=None, level=None):
        return [t for a, l, t in rep.rows if (area is None or a == area) and (level is None or l == level)]

    def test_ready_sim_setup_and_playable_scenarios(self):
        rep = self.P.run(self.env, do_probe=False)
        self.assertFalse(rep.failed, rep.rows)
        play = self.P.check_phones(self.env, self.P.Report())
        self.assertEqual({k for k, v in play.items() if v}, {"C03", "C06", "C12 staff page + ACK"})
        self.assertTrue(any("signed request accepted" in t for t in self.lines(rep, "signature", "PASS")))

    def test_bad_phone_and_shared_handset_fail(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE drivers SET phone='98 0000 0006' WHERE driver_id='DRV006'")
        con.execute("UPDATE facility_contacts SET phone='+919800000013' WHERE contact_id='CON001'")
        con.commit()
        con.close()
        rep = self.P.run(self.env, do_probe=False)
        fails = " | ".join(self.lines(rep, "phones", "FAIL"))
        self.assertIn("DRV006 is mapped to a number that is not E.164", fails)
        self.assertIn("share one handset", fails)

    def test_twilio_env_url_and_snapshot_checks(self):
        env = dict(self.env, CHANNEL="twilio", TWILIO_ACCOUNT_SID="ACxyz", TWILIO_WHATSAPP_FROM="+14155238886",
                   TWILIO_WEBHOOK_BASE_URL="http://127.0.0.1:9", SETUHAUL_DB=self.P.SNAPSHOT)
        rep = self.P.run(env, do_probe=False)
        fails = " | ".join(t for _, l, t in rep.rows if l == "FAIL")
        for want in ("Account SID", "whatsapp:+<E.164>", "must be https", "points at this machine", "unreachable",
                     "is the supplied snapshot"):
            self.assertIn(want, fails)

    def test_never_prints_secret_values(self):
        env = dict(self.env, TWILIO_AUTH_TOKEN="SECRET-TOKEN-VALUE-123")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.P.run(env, do_probe=False).print()
        out = buf.getvalue()
        self.assertNotIn("SECRET-TOKEN-VALUE-123", out)
        self.assertNotIn("9800000013", out)                       # numbers masked


if __name__ == "__main__":
    unittest.main()
