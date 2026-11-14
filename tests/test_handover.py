import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import build_handover as bh  # noqa: E402


def run(session, rows):
    """rows: (stop, printed, gps typical, new time, changed, runs seen)"""
    df = pd.DataFrame([{"route": 1, "session": session, "stop": s, "printed": p, "GPS typical": g,
                        "new time": n, "changed": c, "runs seen": r, "note": None,
                        "sess": "am" if session == "morning" else "pm", "is_changed": c == "yes"}
                       for s, p, g, n, c, r in rows])
    return bh.phase1_times(df).set_index("stop")


class Phase1Times(unittest.TestCase):
    def test_late_afternoon_bus_shifts_whole_run_in_order(self):
        out = run("afternoon", [
            ("A", "15:30", "15:44", "15:30", None, 3),
            ("B", "15:40", "15:54", "15:54", "yes", 3),   # confirmed: its GPS time
            ("C", "15:50", "16:04", "15:50", None, 3),
            ("D", "15:55", None, "15:55", None, 0),       # never seen: kept in order
        ])
        self.assertEqual(out.loc["A", "final"], 15 * 60 + 44)
        self.assertEqual(out.loc["B", "final"], 15 * 60 + 54)
        self.assertEqual(out.loc["C", "final"], 16 * 60 + 4)
        self.assertGreaterEqual(out.loc["D", "final"], out.loc["C", "final"])
        self.assertTrue(out["final"].is_monotonic_increasing)

    def test_morning_never_moves_an_unconfirmed_pickup_later(self):
        out = run("morning", [
            ("A", "06:30", "06:36", "06:30", None, 3),
            ("B", "06:40", "06:46", "06:40", None, 3),
            ("C", "06:50", "06:56", "06:50", None, 3),
        ])
        self.assertTrue((out["final"] <= out["pr"]).all())

    def test_morning_early_bus_moves_pickups_earlier(self):
        out = run("morning", [
            ("A", "06:30", "06:24", "06:30", None, 3),
            ("B", "06:40", "06:34", "06:40", None, 3),
            ("C", "06:50", "06:44", "06:50", None, 3),
        ])
        self.assertEqual(list(out["final"]), [6 * 60 + 24, 6 * 60 + 34, 6 * 60 + 44])


if __name__ == "__main__":
    unittest.main()
