"""
Google Maps layer: usage caps, failure handling, the 30-day storage limit, and
response parsing. Every test runs against a fake HTTP layer - no network, no
billable calls.

    python -m unittest discover tests
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import geo  # noqa: E402
import gmaps  # noqa: E402
from optimiser import matrix  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, payload):
    return urllib.error.HTTPError("https://example.invalid", code, "err", {},
                                  io.BytesIO(json.dumps(payload).encode("utf-8")))


class GoogleTestCase(unittest.TestCase):
    """Fresh ledger, breaker and environment for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "GOOGLE_MAPS_API_KEY": "test-key-123",
            "GMAPS_LEDGER": str(self.dir / "api_usage.json"),
            "GEOCODER": "google", "ROUTER": "google",
        })
        self.env.start()
        for k in [k for k in os.environ if k.startswith("GMAPS_CAP_")]:
            del os.environ[k]
        gmaps._budget = None
        gmaps.reset_breaker()
        gmaps.session_calls.clear()
        self.sleep = mock.patch("gmaps.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.env.stop()
        gmaps._budget = None
        gmaps.reset_breaker()
        self.tmp.cleanup()

    def fake(self, *responses):
        """Patch urlopen to return (or raise) each response in turn; returns the mock."""
        it = iter(responses)

        def opener(req, timeout=None):
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return _Resp(r)
        return mock.patch("gmaps.urllib.request.urlopen", side_effect=opener)


class EnvTests(unittest.TestCase):
    def test_env_file_loads_without_overriding_the_shell(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# comment\nA_TEST_KEY="quoted"\nexport B_TEST_KEY=plain\n'
                         "C_TEST_KEY=from-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"C_TEST_KEY": "from-shell"}):
                gmaps.load_env(p)
                self.assertEqual(os.environ["A_TEST_KEY"], "quoted")
                self.assertEqual(os.environ["B_TEST_KEY"], "plain")
                self.assertEqual(os.environ["C_TEST_KEY"], "from-shell")
            for k in ("A_TEST_KEY", "B_TEST_KEY"):
                os.environ.pop(k, None)


class BudgetTests(GoogleTestCase):
    def test_default_cap_is_80_percent_of_free_allowance(self):
        self.assertEqual(gmaps.cap("places_text_search_pro"), 4000)
        self.assertEqual(gmaps.cap("geocoding"), 8000)
        os.environ["GMAPS_CAP_GEOCODING"] = "12"
        self.assertEqual(gmaps.cap("geocoding"), 12)

    def test_call_refused_once_cap_reached_and_nothing_sent(self):
        os.environ["GMAPS_CAP_GEOCODING"] = "2"
        ok = {"status": "OK", "results": []}
        with self.fake(ok, ok) as m:
            gmaps.geocode("a")
            gmaps.geocode("b")
            with self.assertRaises(gmaps.GoogleUnavailable):
                gmaps.geocode("c")
            self.assertEqual(m.call_count, 2)
        self.assertEqual(gmaps.budget().used("geocoding"), 2)
        ledger = json.loads(Path(os.environ["GMAPS_LEDGER"]).read_text())
        self.assertEqual(ledger[gmaps._month()]["geocoding"], 2)

    def test_matrix_is_charged_per_element(self):
        rows = [{"originIndex": 0, "destinationIndex": j, "duration": "60s",
                 "staticDuration": "50s", "distanceMeters": 500, "condition": "ROUTE_EXISTS"}
                for j in range(3)]
        with self.fake(rows):
            out = gmaps.route_matrix([(28.5, 77.3)], [(28.6, 77.3)] * 3, 1_900_000_000)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[(0, 1)], (60.0, 50.0, 500.0))
        self.assertEqual(gmaps.budget().used("route_matrix_pro"), 3)


class FailureTests(GoogleTestCase):
    def test_billing_disabled_trips_breaker_and_is_not_billed(self):
        err = _http_error(403, {"error": {"status": "PERMISSION_DENIED", "message": "billing",
                                          "details": [{"reason": "BILLING_DISABLED"}]}})
        with self.fake(err) as m:
            with self.assertRaises(gmaps.GoogleUnavailable):
                gmaps.places_text_search("x")
            with self.assertRaises(gmaps.GoogleUnavailable):
                gmaps.places_text_search("y")
            self.assertEqual(m.call_count, 1)          # second call never left the machine
        self.assertIn("BILLING_DISABLED", gmaps.disabled_reason())
        self.assertEqual(gmaps.budget().used("places_text_search_pro"), 0)

    def test_geocoding_request_denied_in_a_200_body(self):
        with self.fake({"status": "REQUEST_DENIED", "error_message": "enable billing"}):
            with self.assertRaises(gmaps.GoogleUnavailable):
                gmaps.geocode("x")
        self.assertFalse(gmaps.available())
        self.assertEqual(gmaps.budget().used("geocoding"), 0)

    def test_server_error_is_retried(self):
        err = _http_error(503, {"error": {"status": "UNAVAILABLE"}})
        with self.fake(err, {"status": "ZERO_RESULTS", "results": []}) as m:
            self.assertEqual(gmaps.geocode("x"), [])
            self.assertEqual(m.call_count, 2)

    def test_key_never_appears_in_error_text(self):
        err = _http_error(400, {"error": {"status": "INVALID_ARGUMENT",
                                          "message": "bad key test-key-123"}})
        with self.fake(err):
            with self.assertRaises(gmaps.GoogleRequestError) as cm:
                gmaps.places_text_search("x")
        self.assertNotIn("test-key-123", str(cm.exception))


class RoutesParsingTests(GoogleTestCase):
    def test_multi_stop_route_returns_traffic_and_static_per_leg(self):
        resp = {"routes": [{
            "duration": "900s", "staticDuration": "600s", "distanceMeters": 7000,
            "legs": [{"duration": "400s", "staticDuration": "250s", "distanceMeters": 3000},
                     {"duration": "500s", "staticDuration": "350s", "distanceMeters": 4000}],
            "polyline": {"geoJsonLinestring": {"coordinates": [[77.3, 28.5], [77.31, 28.51]]}}}]}
        with self.fake(resp) as m:
            r = gmaps.compute_route([(28.5, 77.3), (28.51, 77.31), (28.52, 77.32)], 1_900_000_000)
            body = json.loads(m.call_args[0][0].data)
        self.assertEqual(body["routingPreference"], "TRAFFIC_AWARE_OPTIMAL")
        self.assertIn("departureTime", body)
        self.assertEqual(len(body["intermediates"]), 1)
        self.assertEqual([l["duration_traffic_s"] for l in r["legs"]], [400.0, 500.0])
        self.assertEqual([l["duration_s"] for l in r["legs"]], [250.0, 350.0])
        self.assertEqual(r["geometry"][0], [28.5, 77.3])           # lat, lon
        self.assertEqual(gmaps.budget().used("routes_pro"), 1)

    def test_departure_is_a_future_tuesday_at_the_printed_ist_time(self):
        import datetime as dt
        e = gmaps.school_day_epoch(7 * 60 + 5)
        t = dt.datetime.fromtimestamp(e, gmaps.IST)
        self.assertEqual((t.weekday(), t.hour, t.minute), (1, 7, 5))
        self.assertGreater(e, dt.datetime.now(dt.timezone.utc).timestamp())


class StorageLimitTests(unittest.TestCase):
    def test_fresh_and_stale(self):
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).date()
        self.assertTrue(gmaps.is_fresh(today.isoformat()))
        self.assertTrue(gmaps.is_fresh((today - dt.timedelta(days=30)).isoformat()))
        self.assertFalse(gmaps.is_fresh((today - dt.timedelta(days=31)).isoformat()))
        self.assertFalse(gmaps.is_fresh(None))


class GeocoderTests(GoogleTestCase):
    PLACE = {"places": [{"id": "ChIJ-abc", "displayName": {"text": "Ace Aspire"},
                         "formattedAddress": "Techzone 4, Greater Noida West, Uttar Pradesh",
                         "location": {"latitude": 28.5901, "longitude": 77.4302},
                         "types": ["apartment_complex", "establishment"]}]}
    GEO = {"status": "OK", "results": [{
        "formatted_address": "Ace Aspire, Techzone 4, Greater Noida", "place_id": "ChIJ-def",
        "geometry": {"location": {"lat": 28.5905, "lng": 77.4300},
                     "location_type": "GEOMETRIC_CENTER"},
        "types": ["premise"]}]}

    def gc(self, **kw):
        return geo.Geocoder(self.dir, corpus=["Ace Aspire", "Sector 18", "Gaur City"], **kw)

    def test_places_and_geocoding_agree_and_are_cached_with_a_date(self):
        g = self.gc()
        with self.fake(self.PLACE, self.GEO):
            rec = g.resolve("Ace Aspire")
        self.assertEqual(rec["provider"], "google_places")
        self.assertEqual(rec["place_id"], "ChIJ-abc")
        self.assertEqual(rec["confidence"], "high")
        self.assertEqual(rec["agree"], ["Google Geocoding", "Google Places"])
        entry = g.cache.data["google_places::Ace Aspire"]
        self.assertTrue(gmaps.is_fresh(entry["fetched_at"]))
        with self.fake() as m:                        # warm: zero calls
            g.resolve("Ace Aspire")
            self.assertEqual(m.call_count, 0)

    def test_google_failure_is_not_cached_and_falls_back_to_free_stack(self):
        err = _http_error(403, {"error": {"status": "PERMISSION_DENIED"}})
        g = self.gc()
        photon = [geo.GeoHit(28.59, 77.43, "Ace Aspire, Greater Noida", "high", "photon", "Ace Aspire")]
        with self.fake(err), mock.patch("geo.geocode_photon", return_value=photon), \
                mock.patch("geo.geocode_nominatim", return_value=[]):
            rec = g.resolve("Ace Aspire")
        self.assertEqual(rec["provider"], "photon")
        self.assertFalse(any(k.startswith("google") for k in g.cache.data))

    def test_expired_google_entries_are_deleted_on_load(self):
        (self.dir / "geocode_cache.json").write_text(json.dumps({
            "google::old": {"fetched_at": "2020-01-01", "hits": []},
            "google::new": {"fetched_at": gmaps.stamp(), "hits": []},
            "photon::kept": []}), encoding="utf-8")
        g = self.gc()
        self.assertEqual(g.expired, 1)
        self.assertEqual(sorted(g.cache.data), ["google::new", "photon::kept"])

    def test_distant_second_source_is_reported_as_disagreeing(self):
        best = geo.GeoHit(28.59, 77.43, "Ace Aspire", "high", "google_places", "q")
        far = geo.GeoHit(28.63, 77.43, "Ace Aspire", "high", "photon", "q")      # ~4.4 km
        near = geo.GeoHit(28.5905, 77.4302, "Ace Aspire", "high", "google", "q")
        agree, disagree = geo._agreement(best, [(50, best), (40, far), (45, near)])
        self.assertEqual(agree, ["Google Geocoding", "Google Places"])
        self.assertEqual(disagree[0]["source"], "OpenStreetMap")
        self.assertGreater(disagree[0]["km"], 4)


class RouterTests(GoogleTestCase):
    def test_leg_sends_departure_time_and_keeps_traffic_and_static(self):
        resp = {"routes": [{"duration": "420s", "staticDuration": "300s", "distanceMeters": 2500,
                            "legs": [{"duration": "420s", "staticDuration": "300s",
                                      "distanceMeters": 2500}]}]}
        rt = geo.Router(self.dir)
        with self.fake(resp) as m:
            leg = rt.leg((28.5, 77.3), (28.52, 77.31), depart_min=7 * 60 + 10)
            body = json.loads(m.call_args[0][0].data)
        self.assertIn("departureTime", body)
        self.assertEqual((leg["duration_s"], leg["duration_traffic_s"]), (300.0, 420.0))
        self.assertEqual(rt.google_calls, 1)
        self.assertIn("google|28.50000,77.30000|28.52000,77.31000|h7", rt.cache.data)

    def test_offline_google_build_uses_osrm_entry_it_already_has(self):
        rt = geo.Router(self.dir, offline=True)
        rt.cache.data["osrm|28.50000,77.30000|28.52000,77.31000|h7"] = {
            "duration_s": 280.0, "distance_m": 2400.0, "provider": "osrm"}
        self.assertEqual(rt.leg((28.5, 77.3), (28.52, 77.31), depart_hour=7)["provider"], "osrm")


class MatrixChunkingTests(unittest.TestCase):
    def test_every_billed_element_is_wanted(self):
        todo = {0: {1, 2}, 1: set(range(2, 160)), 5: {0}}
        blocks = matrix.google_requests(todo)
        billed = sum(len(o) * len(d) for o, d in blocks)
        self.assertEqual(billed, sum(len(v) for v in todo.values()))
        self.assertTrue(all(len(o) * len(d) <= 100 for o, d in blocks))


if __name__ == "__main__":
    unittest.main()
