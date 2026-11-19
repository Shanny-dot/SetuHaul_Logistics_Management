"""The scripted scenario run is reproducible; the free race still keeps every invariant.

run_scenarios.py releases the 09:36 two-driver race in a fixed order (DRV006, then DRV013), so its
transcript is byte-identical on every run and equal to the published out/scenario_console.txt.
SCENARIO_RACE=free releases both picks together (any winner); the integrity proof relies on that mode.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(extra_env=None):
    d = tempfile.mkdtemp(prefix="sh-scn-")
    env = dict(os.environ, **(extra_env or {}))
    p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "run_scenarios.py"), d], capture_output=True, text=True,
                       env=env, timeout=300, cwd=HERE)
    if p.returncode:
        raise AssertionError(p.stderr[-2000:])
    files = {f: open(os.path.join(d, f), encoding="utf-8").read() for f in ("scenario_run.json", "scenario_metrics.json")}
    shutil.rmtree(d, ignore_errors=True)
    return p.stdout, files


class ScenarioDeterminism(unittest.TestCase):
    def test_two_runs_identical_and_equal_to_published(self):
        out1, f1 = run()
        out2, f2 = run()
        self.assertEqual(out1, out2)
        self.assertEqual(f1, f2)
        with open(os.path.join(HERE, "out", "scenario_console.txt"), encoding="utf-8") as f:
            self.assertEqual(out1, f.read())

    def test_free_race_keeps_invariants(self):
        _, files = run({"SCENARIO_RACE": "free"})
        log = json.loads(files["scenario_run.json"])["log"]
        race = [e for e in log if e.get("race")]
        self.assertEqual(sorted(e["state"] for e in race), ["BOOKED", "OPTIONS_SHOWN"])     # exactly one winner
        loser = next(e for e in race if e["state"] == "OPTIONS_SHOWN")
        self.assertTrue(any(t.startswith("book:OPTION_GONE") for t in loser["tools"]))
        inv = json.loads(files["scenario_metrics.json"])["invariants"]
        self.assertEqual(sum(inv.values()), 0, inv)


if __name__ == "__main__":
    unittest.main()
