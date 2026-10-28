"""
Route planner: how a proposed stop order is judged against the printed one.
Road times are faked - no network, no billable calls.

    python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import build_routes as br  # noqa: E402

SCHOOL = (28.5000, 77.3000)
STOPS = [   # afternoon drops, printed order A, B, C
    {"name": "A", "lat": 28.5100, "lon": 77.3000, "printed": 900.0},
    {"name": "B", "lat": 28.5105, "lon": 77.3005, "printed": 905.0},
    {"name": "C", "lat": 28.5400, "lon": 77.3000, "printed": 915.0},
]
# The order search's matrix prefers school -> B -> A -> C.
D = np.full((4, 4), 10.0)
D[0][2] = D[2][1] = D[1][3] = 1.0


def fake_roads(runs):
    """run_legs stand-in: Google's legs and km for each stop order, by name."""
    def run_legs(seq, school, morning, cache, start=None):
        legs, km = runs[tuple(s["name"] for s in seq)]
        return {"legs_min": legs, "legs_free_min": legs, "km": km,
                "geometry": [], "provider": "google"}
    return run_legs


def plan(runs, scale):
    with mock.patch.object(br, "table", return_value=D), \
            mock.patch.object(br, "run_legs", side_effect=fake_roads(runs)):
        return br.plan(STOPS, SCHOOL, False, cache=None, scale=scale)


class ProposalJudging(unittest.TestCase):
    # Short legs get a deep discount and long ones a light one, like the fitted
    # bands: re-pairing stops can then look faster on paper alone.
    BANDED = staticmethod(lambda km, verified: 0.5 if km < 2 else 0.9)

    def test_order_google_says_is_slower_is_not_proposed(self):
        # Google: 25 min printed vs 26 proposed. Leg by leg through the bands
        # the proposal would read 16.2 vs 16.5 - a saving that does not exist.
        p = plan({("A", "B", "C"): ([5.0, 10.0, 10.0], 20.0),
                  ("B", "A", "C"): ([16.0, 2.0, 8.0], 19.0)}, self.BANDED)
        self.assertEqual([s["name"] for s in p["optimised"]["stops"]], ["A", "B", "C"])
        self.assertEqual(p["saved_min"], 0.0)
        self.assertIn("printed order kept", p["note"])

    def test_order_that_drives_further_is_not_proposed(self):
        p = plan({("A", "B", "C"): ([10.0, 10.0, 10.0], 20.0),
                  ("B", "A", "C"): ([8.0, 8.0, 8.0], 21.0)}, self.BANDED)
        self.assertEqual(p["saved_min"], 0.0)
        self.assertIsNotNone(p["note"])

    def test_saving_is_googles_saving_at_the_runs_own_scale(self):
        p = plan({("A", "B", "C"): ([10.0, 10.0, 10.0], 20.0),
                  ("B", "A", "C"): ([8.0, 6.0, 6.0], 18.0)}, self.BANDED)
        self.assertEqual([s["name"] for s in p["optimised"]["stops"]], ["B", "A", "C"])
        cur = p["current"]
        factor = cur["drive_min"] / cur["drive_model_min"]
        self.assertAlmostEqual(p["saved_min"], round((30.0 - 20.0) * factor, 1), places=1)
        self.assertIsNone(p["note"])


if __name__ == "__main__":
    unittest.main()
