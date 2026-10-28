import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import gps_campaign as gc

SCHOOL = (28.5127, 77.3602)
M = 1 / 111320                     # degrees of latitude per metre


def _gpx(route, label, pts, taps=()):
    w = "\n".join(f'<wpt lat="{a}" lon="{b}"><time>{(t - gc.IST):%Y-%m-%dT%H:%M:%SZ}</time></wpt>' for a, b, t in taps)
    k = "\n".join(f'<trkpt lat="{a}" lon="{b}"><time>{(t - gc.IST):%Y-%m-%dT%H:%M:%SZ}</time></trkpt>' for a, b, t in pts)
    return (f'<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">'
            f'<metadata><desc>route={route} session={label}</desc></metadata>{w}<trk><trkseg>{k}</trkseg></trk></gpx>')


def _drive(t0, lat0, seconds, metres_per_s=8.0):
    """A bus driving north from lat0 for `seconds`."""
    return [(lat0 + i * metres_per_s * M, 77.30, t0 + dt.timedelta(seconds=i)) for i in range(seconds)]


class TestRuns(unittest.TestCase):
    def test_runs_rebuilt_by_clock_time(self):
        # one morning run saved under "pm", and a file resumed days later holding an afternoon run
        am = _drive(dt.datetime(2026, 10, 21, 6, 40), 28.60, 600)
        pm = _drive(dt.datetime(2026, 10, 23, 15, 5), 28.60, 600)
        test = [(SCHOOL[0], SCHOOL[1], dt.datetime(2026, 10, 17, 10, 20) + dt.timedelta(seconds=i)) for i in range(400)]
        with tempfile.TemporaryDirectory() as d:
            Path(d, "R4_pm_2026-10-21_0640.gpx").write_text(_gpx(4, "pm", am), encoding="utf-8")
            Path(d, "R4_am_2026-10-17_1020.gpx").write_text(_gpx(4, "am", test + pm), encoding="utf-8")
            runs, notes = gc.collect_runs(Path(d), SCHOOL)
        self.assertEqual(sorted(runs), [(4, "2026-10-21", "am"), (4, "2026-10-23", "pm")])
        self.assertTrue(any("labelled pm" in n for n in notes))
        self.assertTrue(any("never left school" in n for n in notes))      # the 17 Oct test at school

    def test_spells_taps_and_repeat_presses(self):
        t0 = dt.datetime(2026, 10, 21, 6, 40)
        pts = [(28.60, 77.30, t0 + dt.timedelta(seconds=i)) for i in range(40)]           # stands 40 s at A
        pts += _drive(t0 + dt.timedelta(seconds=40), 28.60, 150)                            # ~1.2 km
        b = pts[-1][0]
        pts += [(b, 77.30, t0 + dt.timedelta(seconds=190 + i)) for i in range(30)]          # stands 30 s at B
        pts += _drive(t0 + dt.timedelta(seconds=220), b, 300)
        taps = [(28.60, 77.30, t0 + dt.timedelta(seconds=10))]                              # tapped at A
        taps += [(pts[100 + k][0], 77.30, pts[100 + k][2]) for k in range(0, 25, 5)]       # 5 presses while driving
        ev, _, _ = gc.run_events(dict(pts=pts, taps=taps), "am", SCHOOL)
        spells = [e for e in ev if e["kind"] == "spell"]
        moving = [e for e in ev if e["kind"] == "movetap"]
        self.assertEqual(len(spells), 2)
        self.assertEqual([s["ntap"] for s in spells], [1, 0])
        self.assertAlmostEqual(spells[0]["dur"], 40, delta=5)
        self.assertEqual(len(moving), 1)                     # the repeated presses count once


class TestNames(unittest.TestCase):
    def _stop(self, name, ses):
        p, d = gc.geo.split_pickup_drop(name)
        return dict(name=name, place=p if ses == "am" else d, key=name, printed=400.0, cards=None, pin="none",
                    lat=None, lon=None, junior=1, senior=0, staff=0, unknown=0)

    def test_pickup_drop_stops_share_a_place(self):
        names = ["Gate 1, Alpha Vihar", "P - Gate 1, Alpha Vihar D - Beta Apartments, Gamma Nagar"]
        am = gc.group_places([self._stop(n, "am") for n in names])
        pm = gc.group_places([self._stop(n, "pm") for n in names])
        self.assertEqual(len(am), 1)                         # one pickup place in the morning
        self.assertEqual(am[0]["junior"], 2)
        self.assertEqual(len(pm), 2)                         # two drop places in the afternoon

    def test_name_check_needs_the_stops_own_words(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d, "nc.json")
            lat, lon = 28.5100, 77.4090
            cache.write_text(json.dumps({
                f"Delta Town 1, Red Light|{lat:.4f},{lon:.4f}": [{"name": "Delta Town", "addr": "", "lat": lat, "lon": lon}],
                f"Main Gate, Omega Vivace|{lat:.4f},{lon:.4f}": [{"name": "Omega Vivace Tower B", "addr": "",
                                                                 "lat": lat + 50 * M, "lon": lon}],
            }), encoding="utf-8")
            nc = gc.NameCheck(cache, live=False)
            area = {"delta", "town", "omega"}                  # words several stops share: localities, builders
            self.assertNotEqual(nc.verdict("Delta Town 1, Red Light", lat, lon, area)["v"], "confirmed")
            self.assertEqual(nc.verdict("Main Gate, Omega Vivace", lat, lon, area)["v"], "confirmed")
            self.assertIsNone(nc.verdict("Somewhere Else", lat, lon, area))     # not cached, not live: no call


if __name__ == "__main__":
    unittest.main()
