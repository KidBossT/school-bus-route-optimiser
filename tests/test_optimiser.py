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


class CampaignInputTests(unittest.TestCase):
    """What the GPS campaign changed: staff ride, long runs stay legal, traffic is put back, moves listed."""

    def test_a_bus_longer_than_the_limit_today_may_keep_its_length(self):
        p = line_problem("pm", demands=(1, 1, 1))
        cfg = cfg_plain(max_ride_min=2.0)
        ev = Evaluator(p, cfg)
        breach = ev.route_cost([1, 2, 3], 0)                # 3 min run, 2 min limit
        p.vehicles[0].max_run_min = 3.0                      # ... but it runs 3 min today
        ok = Evaluator(p, cfg).route_cost([1, 2, 3], 0)
        self.assertGreater(breach - ok, cfg.weights.violation * 0.99)
        self.assertEqual(Evaluator(p, cfg).route_detail([1, 2, 3], 0)["over_time_min"], 0.0)

    def test_staff_rows_are_riders_when_asked(self):
        import pandas as pd
        import transport_spine as ts
        rows = [["ROUTE # 1"], ["Bus Reporting Time in School : 7:35 AM"],
                ["S.No.", "Admission No", "Student Name", "Grade", "Bus Stop", "New Pickup Time", "Drop Off Time"],
                [1, "PRO20220028", "A Child", "PYP 2 (Grade 2) - B", "Stop X", "6:30am", "4:10pm"],
                [2, "90281", "A Teacher", "Primary", "Stop X", "6:30am", "4:10pm"]]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "roster.xlsx"
            pd.DataFrame(rows).to_excel(f, header=False, index=False)
            self.assertEqual(len(ts.parse_roster(f)), 1)
            both = ts.parse_roster(f, id_pattern=ts.STAFF_OR_STUDENT_ID)
        self.assertEqual(sorted(both.admission_no), ["90281", "PRO20220028"])

    def test_traffic_ratio_from_the_planners_pairs(self):
        from optimiser import build_problem
        a, b = "28.50000,77.30000", "28.51000,77.31000"
        static = {f"google_static|h7|{a}|{b}": [600.0, 5000.0, "2099-01-01"]}       # 10 min, no traffic
        planner = {f"google-matrix|{420 + i}|{a}|{b}": {"min": 13.0, "fetched_at": "2099-01-01"} for i in range(40)}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / build_problem.PLANNER_CACHE).write_text(json.dumps(planner))
            old = build_problem.gmaps.is_fresh
            build_problem.gmaps.is_fresh = lambda d: True
            try:
                r, n = build_problem.static_traffic_ratio(Path(tmp), static, am=True)
                r_pm, _ = build_problem.static_traffic_ratio(Path(tmp), static, am=False)
            finally:
                build_problem.gmaps.is_fresh = old
        self.assertAlmostEqual(r, 1.3)
        self.assertEqual(n, 40)
        self.assertIsNone(r_pm)                               # morning pairs say nothing about afternoons

    def test_a_stop_whose_pin_is_off_the_path_is_timed_on_it(self):
        from optimiser import build_problem
        track = [[28.50 + i * 0.001, 77.30, 400.0 + i] for i in range(20)]       # north, 1 point a minute
        keep, off = build_problem.on_track(track, 28.5052, 77.3001, 405.0)       # pin ~10 m from the path
        self.assertIsNone(keep)
        self.assertLess(off, 50)
        pt, off = build_problem.on_track(track, 28.60, 77.40, 410.0)            # pin ~14 km away
        self.assertEqual(pt, (28.51, 77.30))                                     # where the bus is at 410
        self.assertGreater(off, 10_000)
        self.assertEqual(build_problem.on_track(None, 28.6, 77.4, 410.0), (None, None))

    def test_a_closed_bus_takes_nobody_new(self):
        p = line_problem("pm", caps=(40, 40), demands=(1, 1, 1))
        p.nodes[2].current_vehicle = "B1"
        p.vehicles[1].closed = True
        ev = Evaluator(p, cfg_plain())
        self.assertGreater(ev.route_cost([1, 3], 1) - ev.route_cost([3], 1), 1e5)   # stop 1 lives on B0

    def test_moves_list_only_stops_that_change_bus(self):
        from optimiser import report
        cur = [{"vehicle": "R01", "duration_min": 30.0,
                "stops": [{"node": "a@R01", "name": "A", "children": 2, "eta_min": 400.0, "ride_min": 20.0,
                           "was_on": "R01", "lat": 1, "lon": 2},
                          {"node": "b@R01", "name": "B", "children": 1, "eta_min": 410.0, "ride_min": 10.0,
                           "was_on": "R01", "lat": 1, "lon": 2}]}]
        opt = [{"vehicle": "R01", "duration_min": 20.0, "stops": [dict(cur[0]["stops"][0])]},
               {"vehicle": "R02", "duration_min": 25.0,
                "stops": [dict(cur[0]["stops"][1], eta_min=405.0, ride_min=15.0)]}]
        mv = report.stop_moves("am", cur, opt)
        self.assertEqual([(m["stop"], m["bus_today"], m["bus_proposed"]) for m in mv], [("B", "R01", "R02")])
        self.assertEqual((mv[0]["from_run_today_min"], mv[0]["from_run_proposed_min"]), (30.0, 20.0))


if __name__ == "__main__":
    unittest.main()
