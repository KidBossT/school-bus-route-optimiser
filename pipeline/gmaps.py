"""
Google Maps Platform client for the transport pipeline.

Everything that talks to Google goes through here, so four rules hold in one
place rather than being re-implemented by each caller:

  1. The key comes from the environment, loaded from ./.env if present. It is
     never written to a cache, a log line or an output file.
  2. Every billable call is charged to a monthly ledger (cache/api_usage.json)
     BEFORE it is made, and refused once the SKU reaches its cap. Default caps
     sit at 80% of Google's free monthly allowance, so a normal month costs
     nothing and a runaway loop cannot produce a bill.
  3. A failure is never cached. A denied key, disabled billing or an exhausted
     quota trips a breaker: every later call in the run fails fast without
     touching the network, and callers fall back to the free providers.
  4. Google's terms allow coordinates and route results to be kept for 30
     days. Callers stamp entries with `stamp()` and treat anything older than
     `is_fresh()` allows as a cache miss.

    python pipeline/gmaps.py --check    one call to each API, prints what works
    python pipeline/gmaps.py --usage    this month's ledger against the caps
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

REQUEST_TIMEOUT = 30
MIN_INTERVAL_S = 0.05        # well under every Routes/Places/Geocoding QPM limit
RETRIES = 3

# Free monthly allowance per billing SKU (Google Maps Platform pricing since
# March 2025; the old $200 pooled credit no longer exists). Matrix SKUs are
# billed per ELEMENT (origins x destinations); everything else per request.
FREE_PER_MONTH = {
    "geocoding": 10_000,
    "places_text_search_pro": 5_000,     # displayName/formattedAddress/location/types
    "routes_essentials": 10_000,         # computeRoutes, no traffic, <= 10 intermediates
    "routes_pro": 5_000,                 # computeRoutes with traffic
    "route_matrix_essentials": 10_000,   # elements, no traffic
    "route_matrix_pro": 5_000,           # elements, with traffic
}
DEFAULT_CAP_SHARE = 0.8

# Largest request shapes the Routes API accepts.
MAX_INTERMEDIATES = 25
MAX_MATRIX_ELEMENTS_TRAFFIC_OPTIMAL = 100


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env. A variable already set in the shell wins."""
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def api_key() -> str:
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def _scrub(msg: str) -> str:
    k = api_key()
    return msg.replace(k, "***") if k else msg


# --------------------------------------------------------------------------
# Errors and the breaker
# --------------------------------------------------------------------------

class GoogleError(Exception):
    """Any Google failure. Callers must not cache the result of a failed call."""


class GoogleUnavailable(GoogleError):
    """Key missing, billing off, API not enabled, quota or budget exhausted.
    The breaker is tripped: no further calls this run."""


class GoogleRequestError(GoogleError):
    """This one request failed (bad input, server error after retries)."""


_disabled: str | None = None
session_calls: dict[str, int] = {}


def disabled_reason() -> str | None:
    return _disabled


def available() -> bool:
    return bool(api_key()) and _disabled is None


def _trip(reason: str) -> None:
    global _disabled
    if _disabled is None:
        _disabled = _scrub(reason)
        print(f"  !! Google Maps disabled for the rest of this run: {_disabled}", file=sys.stderr)


def reset_breaker() -> None:
    global _disabled
    _disabled = None


# --------------------------------------------------------------------------
# Monthly usage ledger
# --------------------------------------------------------------------------

def cap(sku: str) -> int:
    env = os.environ.get(f"GMAPS_CAP_{sku.upper()}")
    if env and env.strip().isdigit():
        return int(env)
    return int(FREE_PER_MONTH[sku] * DEFAULT_CAP_SHARE)


def _month() -> str:
    # Google bills by calendar month in Pacific time; UTC is close enough for a
    # cap set 20% below the free allowance.
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


class Budget:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def used(self, sku: str) -> int:
        return int(self.data.get(_month(), {}).get(sku, 0))

    def remaining(self, sku: str) -> int:
        return max(cap(sku) - self.used(sku), 0)

    def spend(self, sku: str, units: int = 1) -> None:
        if sku not in FREE_PER_MONTH:
            raise ValueError(f"unknown SKU {sku!r}")
        if self.used(sku) + units > cap(sku):
            raise GoogleUnavailable(
                f"monthly cap reached for {sku}: {self.used(sku)} used, cap {cap(sku)} "
                f"(set GMAPS_CAP_{sku.upper()} in .env to raise it)")
        self._add(sku, units)

    def refund(self, sku: str, units: int = 1) -> None:
        self._add(sku, -units)

    def _add(self, sku: str, units: int) -> None:
        m = self.data.setdefault(_month(), {})
        m[sku] = max(int(m.get(sku, 0)) + units, 0)
        session_calls[sku] = session_calls.get(sku, 0) + units
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data.setdefault("_README", [
            "Google Maps Platform calls charged per calendar month, per billing SKU.",
            "Written by pipeline/gmaps.py before each call; a call is refused once",
            "the SKU reaches its cap. Matrix SKUs count elements, the rest requests.",
        ])
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


_budget: Budget | None = None


def budget() -> Budget:
    global _budget
    if _budget is None:
        _budget = Budget(Path(os.environ.get("GMAPS_LEDGER", ROOT / "cache" / "api_usage.json")))
    return _budget


def usage_report() -> str:
    b = budget()
    lines = [f"  Google Maps usage, {_month()}  (this run / month to date / cap / free allowance)"]
    for sku in FREE_PER_MONTH:
        lines.append(f"    {sku:26s} {session_calls.get(sku, 0):6d} {b.used(sku):7d} "
                     f"{cap(sku):7d} {FREE_PER_MONTH[sku]:7d}")
    if _disabled:
        lines.append(f"  !! Google was unavailable during this run: {_disabled}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 30-day storage limit
# --------------------------------------------------------------------------

def ttl_days() -> int:
    v = os.environ.get("GOOGLE_CACHE_TTL_DAYS", "30").strip()
    return int(v) if v.isdigit() else 30


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def is_fresh(fetched_at: str | None) -> bool:
    if not fetched_at:
        return False
    try:
        d = dt.date.fromisoformat(str(fetched_at)[:10])
    except ValueError:
        return False
    return (dt.datetime.now(dt.timezone.utc).date() - d).days <= ttl_days()


# --------------------------------------------------------------------------
# Departure times
# --------------------------------------------------------------------------

def school_day_epoch(minute_of_day: float, weekday: int = 1) -> int:
    """
    Epoch seconds for the next representative school day (Tuesday by default)
    at `minute_of_day` IST. Always at least a day ahead: traffic-aware routing
    rejects departure times in the past, and a predicted Tuesday is a better
    stand-in for a school day than whatever today happens to be.
    """
    d = dt.datetime.now(IST).date() + dt.timedelta(days=1)
    while d.weekday() != weekday:
        d += dt.timedelta(days=1)
    m = int(round(float(minute_of_day))) % 1440
    return int(dt.datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=IST).timestamp())


def _rfc3339(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_last_call = 0.0


def _pace():
    global _last_call
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _error_status(body: str) -> tuple[str, str]:
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        return "", body[:200]
    if isinstance(d, list) and d:
        d = d[0]
    e = d.get("error", {}) if isinstance(d, dict) else {}
    reasons = [x.get("reason", "") for x in e.get("details", []) if isinstance(x, dict)]
    return (e.get("status", "") + (" " + " ".join(r for r in reasons if r) if any(reasons) else "")), \
        e.get("message", "")[:300]


def _call(sku: str, units: int, url: str, body: dict | None = None,
          headers: dict | None = None):
    """One billable call. Charges the ledger first, refunds if Google refused it."""
    if _disabled:
        raise GoogleUnavailable(_disabled)
    key = api_key()
    if not key:
        _trip("GOOGLE_MAPS_API_KEY is not set (.env or environment)")
        raise GoogleUnavailable(_disabled)
    try:
        budget().spend(sku, units)
    except GoogleUnavailable as exc:
        _trip(str(exc))
        raise
    hdr = {"Content-Type": "application/json", "User-Agent": "PrometheusTransport/2.1",
           **(headers or {})}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    delay = 1.0
    for attempt in range(RETRIES + 1):
        _pace()
        try:
            req = urllib.request.Request(url, data=data, headers=hdr)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status, msg = _error_status(e.read().decode("utf-8", "replace"))
            if e.code in (401, 403):
                budget().refund(sku, units)          # refused calls are not billed
                _trip(f"HTTP {e.code} {status}: {msg}")
                raise GoogleUnavailable(_disabled) from None
            if e.code == 429 or "RESOURCE_EXHAUSTED" in status:
                if attempt < RETRIES:
                    time.sleep(delay); delay *= 3
                    continue
                _trip(f"quota exhausted (HTTP 429 {status}): {msg}")
                raise GoogleUnavailable(_disabled) from None
            if e.code >= 500 and attempt < RETRIES:
                time.sleep(delay); delay *= 3
                continue
            raise GoogleRequestError(_scrub(f"HTTP {e.code} {status}: {msg}")) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            if attempt < RETRIES:
                time.sleep(delay); delay *= 3
                continue
            raise GoogleRequestError(_scrub(f"network: {e}")) from None


# --------------------------------------------------------------------------
# Geocoding API
# --------------------------------------------------------------------------

def geocode(address: str, bbox: dict | None = None) -> list[dict]:
    """Raw Geocoding API results for `address`, restricted to India, biased to bbox."""
    params = {"address": address, "key": api_key(), "region": "in",
              "components": "country:IN", "language": "en"}
    if bbox:
        params["bounds"] = (f"{bbox['min_lat']},{bbox['min_lon']}|"
                            f"{bbox['max_lat']},{bbox['max_lon']}")
    url = "https://maps.googleapis.com/maps/api/geocode/json?" + urllib.parse.urlencode(params)
    for attempt in range(RETRIES + 1):
        d = _call("geocoding", 1, url)
        st = d.get("status", "")
        if st in ("OK", "ZERO_RESULTS"):
            return d.get("results", []) or []
        if st in ("REQUEST_DENIED", "OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"):
            budget().refund("geocoding", 1)           # refused inside a 200: not billed
        if st == "OVER_QUERY_LIMIT" and attempt < RETRIES:
            time.sleep(2.0 * (attempt + 1))
            continue
        msg = f"geocoding {st}: {d.get('error_message', '')}"
        if st in ("REQUEST_DENIED", "OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"):
            _trip(msg)
            raise GoogleUnavailable(_disabled)
        raise GoogleRequestError(_scrub(msg))
    return []


# --------------------------------------------------------------------------
# Places API (New) - Text Search
# --------------------------------------------------------------------------

# Every field here is in the Text Search Pro SKU. Adding anything from the
# Enterprise list (rating, opening hours, phone, website) would move every call
# to a SKU with a fifth of the free allowance.
PLACES_FIELDS = ("places.id,places.displayName,places.formattedAddress,"
                 "places.location,places.types")


def places_text_search(text: str, bbox: dict | None = None,
                       circle: tuple[float, float, float] | None = None,
                       page_size: int = 10) -> list[dict]:
    """Raw Places Text Search (New) results. circle = (lat, lon, radius_m) beats bbox."""
    body: dict = {"textQuery": text, "regionCode": "IN", "languageCode": "en",
                  "pageSize": page_size}
    if circle:
        body["locationBias"] = {"circle": {
            "center": {"latitude": circle[0], "longitude": circle[1]},
            "radius": min(float(circle[2]), 50_000.0)}}
    elif bbox:
        body["locationBias"] = {"rectangle": {
            "low": {"latitude": bbox["min_lat"], "longitude": bbox["min_lon"]},
            "high": {"latitude": bbox["max_lat"], "longitude": bbox["max_lon"]}}}
    d = _call("places_text_search_pro", 1, "https://places.googleapis.com/v1/places:searchText",
              body, {"X-Goog-Api-Key": api_key(), "X-Goog-FieldMask": PLACES_FIELDS})
    return d.get("places", []) or []


# --------------------------------------------------------------------------
# Routes API
# --------------------------------------------------------------------------

def _wp(p, stopover: bool = False):
    w = {"location": {"latLng": {"latitude": float(p[0]), "longitude": float(p[1])}}}
    if stopover:
        # Pickup/drop-off point: keeps the route off roads unsuitable for
        # stopping (e.g. a society's internal lanes when the pin is at its centre).
        w["vehicleStopover"] = True
    return w


def _secs(v):
    if v in (None, ""):
        return None
    return float(str(v).rstrip("s"))


def compute_route(points: list[tuple[float, float]], depart_epoch: int | None = None,
                  traffic: bool = True, geometry: bool = True,
                  stopover: bool = False) -> dict | None:
    """
    Drive through `points` in order (first = origin, last = destination, up to 25
    in between). With traffic, uses TRAFFIC_AWARE_OPTIMAL at `depart_epoch`:
    Google propagates the clock along the route, so each leg is priced at the
    time the bus actually reaches it, not at the departure time.

    Returns per-leg and total `duration_traffic_s` (predicted, with traffic),
    `duration_s` (static, free-flow), `distance_m`, and the road geometry as
    [[lat, lon], ...]. None when Google finds no drivable route.
    """
    if len(points) < 2:
        raise ValueError("need at least two points")
    inter = points[1:-1]
    if len(inter) > MAX_INTERMEDIATES:
        raise ValueError(f"{len(inter)} intermediates; Routes API allows {MAX_INTERMEDIATES}")
    body = {"origin": _wp(points[0], stopover), "destination": _wp(points[-1], stopover),
            "travelMode": "DRIVE", "languageCode": "en", "units": "METRIC"}
    if inter:
        body["intermediates"] = [_wp(p, stopover) for p in inter]
    if traffic:
        body["routingPreference"] = "TRAFFIC_AWARE_OPTIMAL"
        if depart_epoch:
            body["departureTime"] = _rfc3339(depart_epoch)
        sku = "routes_pro"
    else:
        body["routingPreference"] = "TRAFFIC_UNAWARE"
        sku = "routes_pro" if len(inter) > 10 else "routes_essentials"
    fields = ["routes.duration", "routes.staticDuration", "routes.distanceMeters",
              "routes.legs.duration", "routes.legs.staticDuration", "routes.legs.distanceMeters"]
    if geometry:
        body["polylineEncoding"] = "GEO_JSON_LINESTRING"
        fields.append("routes.polyline")
    d = _call(sku, 1, "https://routes.googleapis.com/directions/v2:computeRoutes", body,
              {"X-Goog-Api-Key": api_key(), "X-Goog-FieldMask": ",".join(fields)})
    if not d.get("routes"):
        return None
    r = d["routes"][0]
    legs = [{"duration_traffic_s": _secs(l.get("duration")),
             "duration_s": _secs(l.get("staticDuration")) or _secs(l.get("duration")),
             "distance_m": float(l.get("distanceMeters", 0) or 0)} for l in r.get("legs", [])]
    coords = ((r.get("polyline") or {}).get("geoJsonLinestring") or {}).get("coordinates") or []
    return {
        "duration_traffic_s": _secs(r.get("duration")) if traffic else None,
        "duration_s": _secs(r.get("staticDuration")) or _secs(r.get("duration")),
        "distance_m": float(r.get("distanceMeters", 0) or 0),
        "legs": legs if traffic else [{**l, "duration_traffic_s": None} for l in legs],
        "geometry": [[round(c[1], 5), round(c[0], 5)] for c in coords],
        "provider": "google", "traffic": traffic,
        "depart": _rfc3339(depart_epoch) if (traffic and depart_epoch) else None,
        "fetched_at": stamp(),
    }


def route_matrix(origins: list[tuple[float, float]], dests: list[tuple[float, float]],
                 depart_epoch: int | None, traffic: bool = True) -> dict:
    """
    {(i, j): (duration_traffic_s, duration_static_s, metres)} for every pair
    with a drivable route. Billed per element; the caller keeps each request
    within MAX_MATRIX_ELEMENTS_TRAFFIC_OPTIMAL.
    """
    n = len(origins) * len(dests)
    body = {"origins": [{"waypoint": _wp(p)} for p in origins],
            "destinations": [{"waypoint": _wp(p)} for p in dests],
            "travelMode": "DRIVE"}
    if traffic:
        if n > MAX_MATRIX_ELEMENTS_TRAFFIC_OPTIMAL:
            raise ValueError(f"{n} elements; TRAFFIC_AWARE_OPTIMAL allows "
                             f"{MAX_MATRIX_ELEMENTS_TRAFFIC_OPTIMAL} per request")
        body["routingPreference"] = "TRAFFIC_AWARE_OPTIMAL"
        if depart_epoch:
            body["departureTime"] = _rfc3339(depart_epoch)
    else:
        body["routingPreference"] = "TRAFFIC_UNAWARE"
    rows = _call("route_matrix_pro" if traffic else "route_matrix_essentials", n,
                 "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix", body,
                 {"X-Goog-Api-Key": api_key(),
                  "X-Goog-FieldMask": "originIndex,destinationIndex,duration,staticDuration,"
                                      "distanceMeters,condition"})
    out = {}
    for e in rows or []:
        if e.get("condition") == "ROUTE_EXISTS" and e.get("duration"):
            static = _secs(e.get("staticDuration")) or _secs(e["duration"])
            out[(e.get("originIndex", 0), e.get("destinationIndex", 0))] = (
                _secs(e["duration"]), static, float(e.get("distanceMeters", 0) or 0))
    return out


# --------------------------------------------------------------------------
# Maps on the web pages
# --------------------------------------------------------------------------

def browser_map_tags(theme: str = "dark") -> str:
    """
    <script> tags for a page's maps: Google Maps JavaScript API with the
    BROWSER key, then gmap_shim.js (Leaflet-style calls drawn on a Google map).

    Google's terms do not allow Places/Geocoding/Routes content on a non-Google
    map, so the pages use Google's map. The browser key is visible to anyone who
    opens a page: it must be a separate key restricted to the pages' web
    address and to the Maps JavaScript API. The server key is never used here.
    """
    key = os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "").strip()
    if key and key == api_key():
        print("  !! GOOGLE_MAPS_BROWSER_KEY is the server key - not putting it in a web page. "
              "Create a separate browser key (see README). Maps are off.", file=sys.stderr)
        key = ""
    shim = (Path(__file__).resolve().parent / "gmap_shim.js").read_text(encoding="utf-8")
    loader = (f'<script src="https://maps.googleapis.com/maps/api/js?key={urllib.parse.quote(key)}'
              f'&v=weekly&language=en&region=IN"></script>' if key else "")
    return (f"<script>window.GMAP_THEME={json.dumps(theme)};</script>{loader}"
            f"<script>{shim}</script>")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _check() -> int:
    """One call to each API. Costs 4 requests + 1 matrix element, all free-tier."""
    print(f"key: {'set (' + api_key()[:6] + '...)' if api_key() else 'MISSING'}")
    school_area, sector_18 = (28.5700, 77.3900), (28.5708, 77.3261)
    dep = school_day_epoch(7 * 60)
    tests = [
        ("Geocoding API", lambda: f"{len(geocode('Sector 18 Noida'))} results"),
        ("Places API (New) Text Search",
         lambda: f"{len(places_text_search('DLF Mall of India Noida', page_size=1))} results"),
        ("Routes API computeRoutes (traffic)",
         lambda: "{:.1f} min with traffic, {:.1f} free-flow".format(
             *(lambda r: (r['duration_traffic_s'] / 60, r['duration_s'] / 60))(
                 compute_route([school_area, sector_18], dep, geometry=False)))),
        ("Routes API computeRouteMatrix (traffic)",
         lambda: f"{len(route_matrix([school_area], [sector_18], dep))} element"),
    ]
    ok = 0
    for name, fn in tests:
        reset_breaker()
        try:
            print(f"  OK    {name}: {fn()}")
            ok += 1
        except GoogleError as exc:
            print(f"  FAIL  {name}: {exc}")
    reset_breaker()
    print(usage_report())
    if ok < len(tests):
        print("\nIf the failure says BILLING_DISABLED or 'must enable Billing': link a billing"
              "\naccount to the project (console > Billing > Link a billing account), wait"
              "\n5 minutes, rerun. 'PERMISSION_DENIED' with billing on usually means that API"
              "\nis not enabled on the project, or the key's API restrictions leave it out.")
    return 0 if ok == len(tests) else 1


if __name__ == "__main__":
    load_env()
    if "--usage" in sys.argv:
        print(usage_report())
        raise SystemExit(0)
    raise SystemExit(_check())
