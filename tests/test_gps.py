import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import calibration as cal
import gps_runs

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
{wpts}
<trk><trkseg>
{trkpts}
</trkseg></trk></gpx>"""


def _pt(tag, lat, lon, t):
    return f'<{tag} lat="{lat}" lon="{lon}"><time>{t:%Y-%m-%dT%H:%M:%SZ}</time></{tag}>'


class TestGpsRuns(unittest.TestCase):
    def test_stop_events_and_lag_free_dwell(self):
        # stand 40 s at A, drive ~1.1 km north for 120 s, stand 30 s at B
        t0 = dt.datetime(2026, 10, 5, 1, 0, 0)          # 06:30 IST
        trk, lat = [], 28.50
        for s in range(40):
            trk.append((28.50, 77.30, t0 + dt.timedelta(seconds=s)))
        for s in range(1, 121):
            lat = 28.50 + 0.01 * s / 120
            trk.append((lat, 77.30, t0 + dt.timedelta(seconds=40 + s)))
        for s in range(1, 31):
            trk.append((28.51, 77.30, t0 + dt.timedelta(seconds=160 + s)))
        taps = [(28.50, 77.30, t0 + dt.timedelta(seconds=2)), (28.51, 77.30, t0 + dt.timedelta(seconds=163))]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "run.gpx"
            p.write_text(GPX.format(wpts="\n".join(_pt("wpt", *w) for w in taps),
                                    trkpts="\n".join(_pt("trkpt", *x) for x in trk)), encoding="utf-8")
            track, w, _ = gps_runs.read_gpx(p)
        self.assertEqual(len(w), 2)
        self.assertEqual(track[0][2].hour, 6)                # converted to IST
        ev = gps_runs.stop_events(track, w)
        self.assertIsNone(ev[0]["arr"])                      # logging began at the stop
        self.assertAlmostEqual((ev[0]["dep"] - track[0][2]).total_seconds(), 40, delta=4)
        self.assertAlmostEqual((ev[1]["arr"] - track[0][2]).total_seconds(), 158, delta=4)
        self.assertIsNone(ev[1]["dep"])                      # logging ended at the stop

    def test_align_skips_stops_and_taps(self):
        pins = [{"lat": 28.50 + 0.01 * i, "lon": 77.30} for i in range(5)]
        ev = [{"lat": 28.5101, "lon": 77.30}, {"lat": 28.5301, "lon": 77.30},
              {"lat": 28.70, "lon": 77.50},                  # nowhere near the route
              {"lat": 28.5401, "lon": 77.30}]
        self.assertEqual(gps_runs.align(ev, pins), [1, 3, None, 4])


class TestVerifiedFactor(unittest.TestCase):
    def _runs(self, n_gps):
        printed = [cal.Leg(0.5 + 0.1 * i, 6.0, 3.0) for i in range(30)]       # x0.5 at guessed pins
        gps = [cal.Leg(0.6, 4.0, gps=4.0, verified=True) for _ in range(n_gps)]  # x1.0 at real pins
        return [cal.Run("R1", "am", printed, 30.0), cal.Run("R2", "am", gps, 0.0)]

    def test_gps_legs_set_the_verified_factor_only(self):
        m = cal.fit(self._runs(6))
        self.assertAlmostEqual(m.factor("am", 0.6, verified=True), 1.0)
        self.assertAlmostEqual(m.factor("am", 0.6), 0.5)
        self.assertEqual(m.verified_factor["pm"], 1.0)       # borrowed from the morning

    def test_too_few_gps_legs_fall_back_to_bands(self):
        m = cal.fit(self._runs(cal.MIN_GPS_LEGS - 1))
        self.assertIsNone(m.verified_factor["am"])
        self.assertAlmostEqual(m.factor("am", 0.6, verified=True), 0.5)


if __name__ == "__main__":
    unittest.main()
