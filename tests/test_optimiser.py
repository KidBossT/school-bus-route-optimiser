"""
Tests for the route optimiser. Standard library only:

    python -m unittest discover tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

from optimiser import coords, consolidate, demo  # noqa: E402
from optimiser.config import OptimiserConfig  # noqa: E402
from optimiser.evaluate import Evaluator  # noqa: E402
from optimiser.model import Node, Problem, Solution, Vehicle  # noqa: E402
from optimiser.solver import Solver  # noqa: E402


def line_problem(direction="pm", caps=(40,), demands=(1, 1, 1), locked=None):
    """School at km 0, stops at km 1, 2, 3 on a straight road; 1 km = 1 min."""
    n = len(demands)
    pos = np.arange(n + 1, dtype=float)
    T = np.abs(pos[:, None] - pos[None, :])
    nodes = [Node(id=f"s{i}", name=f"s{i}", lat=28.5 + i * 0.01, lon=77.3, demand=d,
                  deadline_min=455.0 if direction == "am" else None,
                  current_vehicle="B0", locked_vehicle=(locked or {}).get(i),
                  students=[f"k{i}{c}" for c in range(d)])
             for i, d in enumerate(demands, start=1)]
    vehicles = [Vehicle(f"B{v}", c) for v, c in enumerate(caps)]
    return Problem(direction, (28.5, 77.3), nodes, vehicles, T, T.copy(),
                   np.zeros_like(T, dtype=bool), np.zeros(n + 1))


def cfg_plain(**kw) -> OptimiserConfig:
    cfg = OptimiserConfig(**kw)
    cfg.time_limit_s = 1.0
    return cfg


class EvaluatorTests(unittest.TestCase):
    def test_pm_cost_and_etas(self):
        p = line_problem("pm")
        cfg = cfg_plain()
        ev = Evaluator(p, cfg)
        d = ev.route_detail([1, 2, 3], 0)
        self.assertEqual(d["duration_min"], 3.0)
        self.assertEqual(d["km"], 3.0)
        depart = cfg.dismissal_min + cfg.school_dwell_min
        self.assertEqual([s["eta_min"] for s in d["stops"]], [depart + 1, depart + 2, depart + 3])
        w = cfg.weights
        expected = w.bus_fixed + w.drive_min * 3 + w.km * 3 + w.student_ride_min * (1 + 2 + 3)
        self.assertAlmostEqual(ev.route_cost([1, 2, 3], 0), expected)

    def test_bus_carrying_only_held_children_still_counts(self):
        # A bus whose stops are all held (no usable pin) must not look "saved".
        p = line_problem("pm", caps=(40, 40))
        p.vehicles[1].reserved_load = 5
        ev = Evaluator(p, cfg_plain())
        s = ev.summary(Solution([[1, 2, 3], []]))
        self.assertEqual(s["buses_used"], 2)
        self.assertEqual(s["buses_held_only"], 1)

    def test_am_runs_towards_school(self):
        p = line_problem("am")
        ev = Evaluator(p, cfg_plain())
        d = ev.route_detail([1, 2, 3], 0)       # outward order; driving order is 3,2,1,school
        self.assertEqual([s["name"] for s in d["stops"]], ["s3", "s2", "s1"])
        etas = [s["eta_min"] for s in d["stops"]]
        self.assertEqual(etas, sorted(etas))
        self.assertEqual(d["end_min"], 455.0)
        self.assertEqual(etas[-1], 454.0)

    def test_capacity_breach_is_penalised(self):
        p = line_problem(caps=(2,))
        ev = Evaluator(p, cfg_plain())
        ok = ev.route_cost([1, 2], 0)
        over = ev.route_cost([1, 2, 3], 0)
        self.assertGreater(over - ok, cfg_plain().weights.violation * 0.99)


class SolverTests(unittest.TestCase):
    def assert_complete(self, p, sol):
        seen = sorted(j for r in sol.routes for j in r)
        self.assertEqual(seen, list(range(1, p.n + 1)), "every stop exactly once")

    def test_finds_obvious_order(self):
        p = line_problem("pm")
        ev = Evaluator(p, cfg_plain())
        s = Solver(p, ev, k=3)
        best, _ = s.solve([("bad", Solution([[3, 1, 2]]))], time_limit_s=0.5, log=lambda *_: None)
        self.assertEqual(best.routes[0], [1, 2, 3])

    def test_splits_when_capacity_forces_it(self):
        p = line_problem("pm", caps=(2, 2), demands=(1, 1, 1, 1))
        ev = Evaluator(p, cfg_plain())
        s = Solver(p, ev, k=4)
        best, _ = s.solve([("one bus", Solution([[1, 2, 3, 4], []]))], 0.5, log=lambda *_: None)
        self.assert_complete(p, best)
        self.assertTrue(all(sum(p.nodes[j - 1].demand for j in r) <= 2 for r in best.routes))

    def test_locked_stop_stays_on_its_bus(self):
        p = line_problem("pm", caps=(40, 40), locked={3: "B1"})
        ev = Evaluator(p, cfg_plain())
        s = Solver(p, ev, k=3)
        best, _ = s.solve([("start", Solution([[1, 2], [3]]))], 0.5, log=lambda *_: None)
        self.assertIn(3, best.routes[1])

    def test_demo_network_improves_and_stays_complete(self):
        cfg = cfg_plain()
        for d in ("am", "pm"):
            b = demo.make(d, cfg, n_buses=6, stops_per_bus=6)
            ev = Evaluator(b.problem, cfg)
            s = Solver(b.problem, ev, k=cfg.neighbours, seed=1)
            best, _ = s.solve([("today", b.current), ("savings", s.savings())], 2.0,
                              log=lambda *_: None)
            self.assert_complete(b.problem, best)
            self.assertLessEqual(ev.total(best), ev.total(b.current))
            self.assertEqual(ev.summary(best)["routes_over_capacity"], 0)


class ConsolidationTests(unittest.TestCase):
    def test_close_stops_are_proposed_far_ones_are_not(self):
        a = Node("a", "a", 28.5000, 77.3000, 3)
        b = Node("b", "b", 28.5009, 77.3000, 1)     # ~100 m north
        c = Node("c", "c", 28.5100, 77.3000, 1)     # ~1.1 km north
        props = consolidate.propose([a, b, c], radius_m=250, max_group_demand=40)
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["hub"], "a")       # most children walk least
        self.assertEqual([m["node"] for m in props[0]["members"]], ["b"])
        merged = consolidate.apply([a, b, c], props)
        self.assertEqual([n.id for n in merged], ["a", "c"])
        self.assertEqual(merged[0].demand, 4)


class CoordinateImportTests(unittest.TestCase):
    def test_google_maps_paste_and_swapped_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "geocode_overrides.json").write_text(json.dumps({"_README": ["x"]}))
            sheet = tmp / "filled.csv"
            sheet.write_text(
                "key,stop_text,google_maps,lat,lon,note\n"
                "Prometheus School Noida,School,\"28.51237, 77.35986\",,,\n"
                "Stop A,Stop A,,28.60,77.40,checked on site\n"
                "Stop B,Stop B,\"77.40, 28.60\",,,\n"
                "Stop C,Stop C,,,,\n", encoding="utf-8")
            st = coords.import_file(sheet, tmp)
            self.assertEqual(st["imported"], 2)
            self.assertEqual(st["skipped_blank"], 1)
            self.assertEqual([k for k, _ in st["rejected"]], ["Stop B"])
            ov = json.loads((tmp / "geocode_overrides.json").read_text(encoding="utf-8"))
            self.assertEqual(ov["Prometheus School Noida"]["lat"], 28.51237)
            self.assertEqual(ov["Stop A"]["note"], "checked on site")
            self.assertIn("_README", ov)
            self.assertTrue(list(tmp.glob("geocode_overrides.backup-*.json")))


if __name__ == "__main__":
    unittest.main()
