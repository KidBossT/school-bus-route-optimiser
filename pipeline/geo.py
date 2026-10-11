"""
Geocoding and road-time layer for the Prometheus School transport dashboard.

Two provider families, one interface, so the free prototype stack can be
swapped for the paid production stack by setting environment variables and
re-running. Caches are keyed by the exact query string, so a provider switch
re-uses nothing stale and a warm cache makes zero network calls.

    GEOCODER  = photon | nominatim | google      (default: photon)
    ROUTER    = osrm | google                    (default: osrm)
    GOOGLE_MAPS_API_KEY = <key>                  (required only for google)

All three are read from ./.env if present (see gmaps.py, which also enforces
the monthly usage caps). GEOCODER=google queries BOTH Places Text Search (New)
and the Geocoding API for each stop, and scores their candidates together with
any free-provider answers already in the cache, which cost nothing to re-read.

FREE STACK (this prototype)
  Geocoding : Photon (photon.komoot.io) - OpenStreetMap data behind an
              Elasticsearch fuzzy matcher. No key. Handles the informal,
              misspelt, locally-phrased stop names in this dataset far better
              than plain Nominatim, which needs near-exact strings.
              Nominatim is queried as a cross-check when Photon is unsure.
  Routing   : OSRM public demo server. No key. FREE-FLOW durations only -
              there is no traffic model, so the congestion ratio computed
              against it is (observed / free-flow), which is exactly the
              locally-calibrated factor section 5 asks for.

GOOGLE STACK (GEOCODER=google / ROUTER=google in .env)
  Geocoding : Google Geocoding API. Materially better on Indian informal
              addresses, returns a location_type and a partial_match flag that
              map straight onto the confidence tiers used here.
  Routing   : Google Routes API computeRoutes with TRAFFIC_AWARE_OPTIMAL and a
              departureTime. This is the only option among these that returns
              a traffic-adjusted duration for a specific departure time, which
              lets the free-flow ratio be decomposed into "model traffic" and
              "residual local effect".

Nothing in this module writes an API key to disk. Caches hold coordinates and
durations only.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import gmaps

gmaps.load_env()

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Delhi NCR bounding box. Stops outside this are almost certainly geocoding
# errors: the fleet does not leave the NCR.
BBOX = {"min_lat": 28.20, "max_lat": 29.10, "min_lon": 76.80, "max_lon": 77.85}

# Bias point for the geocoder. Sector 77 Noida, near the school.
BIAS_LAT, BIAS_LON = 28.5700, 77.3900

# Consecutive stops further apart than this are flagged for review.
FAR_FROM_NEIGHBOUR_KM = 3.0

USER_AGENT = (
    "PrometheusSchoolTransportDashboard/2.0 "
    "(school transport reliability analysis; contact via school office)"
)

# Politeness delay between live calls, seconds. Nominatim's usage policy is a
# hard 1 req/s; Photon and the OSRM demo server ask for restraint.
SLEEP_PHOTON = 0.35
SLEEP_NOMINATIM = 1.1
SLEEP_OSRM = 0.35

REQUEST_TIMEOUT = 25


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _norm(s) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def in_bbox(lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    return (
        BBOX["min_lat"] <= lat <= BBOX["max_lat"]
        and BBOX["min_lon"] <= lon <= BBOX["max_lon"]
    )


def haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    """Great-circle distance in km."""
    r = 6371.0088
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _get_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


class JsonCache:
    """Dict-backed JSON file cache. Written atomically; safe to commit."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0

    def get(self, key):
        if key in self.data:
            self.hits += 1
            return self.data[key]
        self.misses += 1
        return None

    def put(self, key, value):
        self.data[key] = value
        self._dirty = True

    def save(self, force: bool = False):
        if not (self._dirty or force):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False


# --------------------------------------------------------------------------
# Stop-name handling
# --------------------------------------------------------------------------

# 24 stop strings pack the pickup point and the drop point into one cell:
#   "P - Gate 1, Yojna Vihar D - Gitanjali Apartments, Karkardooma"
#   "P- Kaveri Gate, Jaypee Greens D- Narmada Gate, Jaypee Greens"
#   "Laxmi Nagar Metro p-Gate3,D-#1"
# Geocoding the whole string puts one marker halfway between two real places.
# The morning layer must use the P half and the afternoon layer the D half.
_PD_SPLIT = re.compile(
    r"^(?P<pre>.*?)(?:^|[\s,])[Pp]\s*-\s*(?P<p>.+?)[\s,]+[Dd]\s*-\s*(?P<d>.+)$"
)
_LEAD_P = re.compile(r"^\s*[Pp]\s*-\s*")


def split_pickup_drop(stop_name: str) -> tuple[str, str]:
    """
    Return (pickup_text, drop_text) for a stop string.

    For the ordinary case both halves are the same string. For the 24 combined
    strings they are the two distinct locations. Text sitting before the "P-"
    marker is shared context ("Laxmi Nagar Metro p-Gate3,D-#1") and is carried
    onto both halves, without which neither half is geocodable.
    """
    s = _norm(stop_name)
    m = _PD_SPLIT.search(s)
    if m:
        pre = _norm(m.group("pre")).rstrip(" ,-")
        p = _norm(m.group("p")).rstrip(" ,")
        d = _norm(m.group("d")).rstrip(" ,")
        if p and d:
            join = lambda half: _norm(f"{pre} {half}") if pre else half
            return join(p), join(d)
    # "P - Main Road opp vaid van park, Assotech" - pickup marker, no drop half
    s2 = _norm(_LEAD_P.sub("", s))
    return (s2 or s), (s2 or s)


# Tokens that describe a doorway rather than a place. Dropped in the fallback
# query so the geocoder can find the estate itself when the gate is unknown.
_DOORWAY = re.compile(
    r"^\s*(?:main\s+gate|back\s*gate|entry\s+gate|exit\s+gate|commercial\s+gate|"
    r"gate\s*(?:no\.?|#)?\s*\d*[a-z]?|opposite\s+roadside|opposite\s+road|"
    r"opposite|opp\.?|near|behind|infront\s+of|in\s+front\s+of|inside|under)\b"
    r"[\s,\-\.#]*",
    re.I,
)

_NOISE = re.compile(r"\b(?:t\s*point|red\s*light|gol\s*chakkar|golchakkar|round\s*about)\b", re.I)


def core_text(text: str) -> str:
    """The stop string minus its leading doorway phrase."""
    s = _norm(text).rstrip(" ,.-")
    stripped = _DOORWAY.sub("", s).strip(" ,.-")
    return stripped or s


def query_variants(text: str) -> list[str]:
    """
    Candidate geocoder queries for one stop, most specific first.

    Deliberately NO region suffix. Photon AND-matches its tokens, so appending
    ", Delhi NCR, India" turns a working query into zero results - "Ace Aspire"
    resolves correctly, "Ace Aspire, Greater Noida West" returns nothing at all.
    Region is supplied instead by the bias coordinate and the bounding box,
    which constrain without excluding. A bare "Noida" hint is tried last, as a
    fallback for names too generic to place on their own.
    """
    base = _norm(text).rstrip(" ,.-")
    if not base:
        return []
    out: list[str] = []

    def add(q: str):
        q = _norm(q).rstrip(" ,.-")
        if q and len(q) > 2 and q.lower() not in {o.lower() for o in out}:
            out.append(q)

    add(base)

    stripped = core_text(base)
    add(stripped)

    # Longest comma-separated fragment, usually the estate or locality name.
    parts = [p.strip() for p in re.split(r"[,;]", stripped) if len(p.strip()) > 3]
    parts = [p for p in parts if not _DOORWAY.match(p)]
    longest = ""
    if parts:
        longest = _NOISE.sub("", max(parts, key=len)).strip(" ,.-")
        if len(longest) > 3:
            add(longest)

    add(f"{longest or stripped} Noida")

    return out[:4]


# --------------------------------------------------------------------------
# Geocoding providers
# --------------------------------------------------------------------------

@dataclass
class GeoHit:
    lat: float
    lon: float
    label: str          # formatted address the service returned
    confidence: str     # high | medium | low
    provider: str
    query: str
    raw_type: str = ""
    place_id: str = ""  # Google only; storable indefinitely, unlike the coordinate

    def as_dict(self) -> dict:
        d = {
            "lat": self.lat, "lon": self.lon, "label": self.label,
            "confidence": self.confidence, "provider": self.provider,
            "query": self.query, "raw_type": self.raw_type,
        }
        if self.place_id:
            d["place_id"] = self.place_id
        return d


# Photon / OSM place types, mapped to confidence. A result typed "city" for a
# query naming a housing society means the geocoder gave up and returned the
# containing city - a confident, wrong answer, and exactly what section 5 warns
# about.
_PHOTON_HIGH = {"house", "street", "building", "amenity", "shop", "office", "railway"}
_PHOTON_MED = {"locality", "district", "neighbourhood", "suburb", "quarter", "residential"}
_PHOTON_LOW = {"city", "state", "country", "county", "region", "postcode"}


def _photon_confidence(props: dict) -> str:
    t = _norm(props.get("type", "")).lower()
    osm_key = _norm(props.get("osm_key", "")).lower()
    if t in _PHOTON_LOW:
        return "low"
    if t in _PHOTON_HIGH or osm_key in {"amenity", "shop", "building", "office", "highway"}:
        return "high"
    if t in _PHOTON_MED or osm_key in {"place", "landuse"}:
        return "medium"
    return "medium"


def _photon_label(props: dict) -> str:
    bits = [props.get(k) for k in
            ("name", "street", "district", "city", "county", "state", "postcode", "country")]
    return ", ".join(_norm(b) for b in bits if _norm(b))


def geocode_photon(query: str) -> list[GeoHit]:
    url = "https://photon.komoot.io/api?" + urllib.parse.urlencode({
        "q": query, "limit": 8, "lat": BIAS_LAT, "lon": BIAS_LON, "lang": "en",
        "bbox": f"{BBOX['min_lon']},{BBOX['min_lat']},{BBOX['max_lon']},{BBOX['max_lat']}",
    })
    data = _get_json(url)
    hits = []
    for f in data.get("features", []):
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
        except (KeyError, IndexError, TypeError):
            continue
        p = f.get("properties", {}) or {}
        hits.append(GeoHit(
            lat=float(lat), lon=float(lon), label=_photon_label(p),
            confidence=_photon_confidence(p), provider="photon", query=query,
            raw_type=_norm(p.get("type", "")),
        ))
    return hits


_NOMINATIM_HIGH = {"house", "building", "amenity", "shop", "office", "road", "residential"}
_NOMINATIM_LOW = {"city", "state", "country", "county", "postcode", "region"}


def geocode_nominatim(query: str) -> list[GeoHit]:
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": query, "format": "jsonv2", "limit": 5, "countrycodes": "in",
        "addressdetails": 1,
        "viewbox": f"{BBOX['min_lon']},{BBOX['max_lat']},{BBOX['max_lon']},{BBOX['min_lat']}",
        "bounded": 1,
    })
    data = _get_json(url)
    hits = []
    for f in data or []:
        try:
            lat, lon = float(f["lat"]), float(f["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        at = _norm(f.get("addresstype", "")).lower()
        conf = "high" if at in _NOMINATIM_HIGH else "low" if at in _NOMINATIM_LOW else "medium"
        hits.append(GeoHit(
            lat=lat, lon=lon, label=_norm(f.get("display_name", "")),
            confidence=conf, provider="nominatim", query=query, raw_type=at,
        ))
    return hits


# Google place types that name an area, not a point. A result carrying only
# these is the service giving up and returning the containing sector or city.
_G_AREA = {"locality", "sublocality", "sublocality_level_1", "sublocality_level_2",
           "sublocality_level_3", "administrative_area_level_1",
           "administrative_area_level_2", "administrative_area_level_3", "country",
           "postal_code", "political", "colloquial_area", "geocode"}
_G_STREETISH = {"route", "neighborhood", "sublocality_level_4", "sublocality_level_5",
                "natural_feature", "plus_code"}


def _google_type_confidence(types: list[str]) -> str:
    t = set(types or [])
    if not t or t <= _G_AREA:
        return "low"
    if t - _G_AREA <= _G_STREETISH:
        return "medium"
    return "high"


def geocode_google(query: str) -> list[GeoHit]:
    """Google Geocoding API. Raises gmaps.GoogleError on failure (never cached)."""
    tier = {"ROOFTOP": "high", "RANGE_INTERPOLATED": "high",
            "GEOMETRIC_CENTER": "medium", "APPROXIMATE": "low"}
    hits = []
    for r in gmaps.geocode(query, BBOX):
        loc = r.get("geometry", {}).get("location", {})
        conf = tier.get(r.get("geometry", {}).get("location_type", ""), "medium")
        # An establishment or premise at its geometric centre is a point, not an area.
        if conf == "medium" and _google_type_confidence(r.get("types", [])) == "high":
            conf = "high"
        if r.get("partial_match"):
            conf = "low" if conf == "medium" else "medium"
        hits.append(GeoHit(
            lat=float(loc.get("lat")), lon=float(loc.get("lng")),
            label=_norm(r.get("formatted_address", "")), confidence=conf,
            provider="google", query=query,
            raw_type=",".join(r.get("types", [])[:2]),
            place_id=r.get("place_id", ""),
        ))
    return hits


def geocode_google_places(query: str, circle: tuple | None = None) -> list[GeoHit]:
    """
    Places API (New) Text Search. Finds a stop described by a landmark ("near
    Kwality Store", "Gate 4 Ace Aspire") as a business or building lookup, which
    the address geocoder cannot. The label is the place name followed by its
    address, so the name-match gate sees the words a parent would recognise.
    `circle` = (lat, lon, radius_m) biases to a route instead of the whole NCR.
    """
    hits = []
    for p in gmaps.places_text_search(query, bbox=None if circle else BBOX, circle=circle):
        loc = p.get("location") or {}
        if "latitude" not in loc:
            continue
        name = _norm((p.get("displayName") or {}).get("text", ""))
        addr = _norm(p.get("formattedAddress", ""))
        label = name if addr.lower().startswith(name.lower()) else f"{name}, {addr}".strip(", ")
        types = p.get("types", []) or []
        hits.append(GeoHit(
            lat=float(loc["latitude"]), lon=float(loc["longitude"]), label=label,
            confidence=_google_type_confidence(types), provider="google_places",
            query=query, raw_type=",".join(types[:2]), place_id=p.get("id", ""),
        ))
    return hits


def geocode_arcgis(query: str) -> list[GeoHit]:
    """
    Esri World Geocoder, free without a key for non-stored lookups. Better POI
    coverage in Indian cities than OSM, and a Score + Addr_type that map onto
    the confidence tiers. Locality-level answers are area centres, not points,
    and are kept only as approximate.
    """
    url = ("https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
           "findAddressCandidates?" + urllib.parse.urlencode({
               "singleLine": query, "f": "json", "maxLocations": 6,
               "outFields": "Addr_type,Score", "countryCode": "IND",
               "location": f"{BIAS_LON},{BIAS_LAT}",
               "searchExtent": f"{BBOX['min_lon']},{BBOX['min_lat']},{BBOX['max_lon']},{BBOX['max_lat']}",
           }))
    data = _get_json(url)
    hits = []
    for c in data.get("candidates", []) or []:
        at = _norm((c.get("attributes") or {}).get("Addr_type", ""))
        score = float(c.get("score", 0) or 0)
        if score < 75:
            continue
        conf = ("high" if at in {"POI", "PointAddress", "StreetAddress", "Subaddress"}
                else "medium" if at in {"StreetName", "StreetInt", "DistanceMarker"}
                else "low")
        loc = c.get("location") or {}
        hits.append(GeoHit(lat=float(loc.get("y")), lon=float(loc.get("x")),
                           label=_norm(c.get("address", "")), confidence=conf,
                           provider="arcgis", query=query, raw_type=at))
    return hits


_CONF_RANK = {"high": 3, "medium": 2, "low": 1}
REGION_HINTS = ["Noida", "Greater Noida", "Delhi", "Ghaziabad"]


# Words that carry no locating power here: every second stop is a "Main Gate"
# on a "Road" in "Noida". Numbers are kept - sector and gate numbers are the
# most discriminating tokens in this dataset.
_GENERIC = {
    "gate", "main", "back", "near", "opposite", "opp", "road", "raod", "the",
    "delhi", "ncr", "india", "noida", "block", "and", "of", "no", "entry",
    "exit", "front", "infront", "inside", "under", "roadside", "uttar",
    "pradesh", "dadri", "gautam", "buddha", "nagar", "new",
}


def _distinctive_tokens(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in toks
            if (t.isdigit() or len(t) >= 3) and t not in _GENERIC}


def build_idf(corpus: list[str]) -> dict[str, float]:
    """
    Inverse document frequency of each token across the school's own stop list.

    A flat token count cannot tell "metro station" from "Vishwavidyalaya", so
    Photon's answer of "Karkarduma Metro Station" scores 2 matches out of 3 and
    wins - a confidently wrong point 10 km from the real stop. Weighting by
    rarity inside this corpus fixes it without any hand-maintained word list:
    "metro", "sector" and "apartments" recur across dozens of stops and sink to
    near zero, while "vishwavidyalaya", "kalkaji" and "gitanjali" appear once
    and carry the match.
    """
    n = max(len(corpus), 1)
    df: dict[str, int] = {}
    for text in corpus:
        for t in set(_distinctive_tokens(core_text(text))):
            df[t] = df.get(t, 0) + 1
    return {t: math.log(n / (1.0 + c)) + 0.1 for t, c in df.items()}


MIN_TOKEN_OVERLAP = 0.34     # weighted share of the query that must be matched


def score_hit(hit: GeoHit, original: str, idf: dict[str, float] | None = None,
              bias: tuple | None = None, max_km: float | None = None) -> float:
    """
    Rank candidates. Returns -1 for anything that must not be used.

    Token overlap is a GATE, not a bonus. Photon answers "Klay School Noida"
    with four other schools in Noida and types them all "house", so a scorer
    led by result type picks a confident wrong answer every time.
    """
    if not in_bbox(hit.lat, hit.lon):
        return -1.0
    if bias is not None and max_km is not None:
        if haversine_km(hit.lat, hit.lon, bias[0], bias[1]) > max_km:
            return -1.0
    want = _distinctive_tokens(core_text(original))
    got = _distinctive_tokens(hit.label)
    if want:
        w = idf or {}
        weight = lambda t: w.get(t, 1.0)
        total = sum(weight(t) for t in want)
        matched = sum(weight(t) for t in (want & got))
        overlap = matched / total if total else 0.0
        if overlap < MIN_TOKEN_OVERLAP:
            return -1.0
        s = 45.0 * overlap + 6.0 * _CONF_RANK.get(hit.confidence, 1)
    else:
        # Nothing distinctive to match on ("D-1", "T Point"). Never trusted;
        # scored low so it always lands in the review file.
        s = 4.0 * _CONF_RANK.get(hit.confidence, 1)
    # Mild pull toward the fleet's centre of gravity: breaks ties between
    # identically-named localities without overriding a strong text match.
    if bias is not None:
        # Route context: among candidates that pass the name gate, the one near
        # the rest of the route wins. This is what separates Delhi's Model Town
        # from Ghaziabad's when both match the text equally well.
        s -= min(haversine_km(hit.lat, hit.lon, bias[0], bias[1]), 40.0) * 0.9
    else:
        s -= min(haversine_km(hit.lat, hit.lon, BIAS_LAT, BIAS_LON), 60.0) * 0.06
    return s


# --------------------------------------------------------------------------
# Geocoder with cache + human overrides
# --------------------------------------------------------------------------

GOOGLE_PROVIDERS = {"google", "google_places"}


def _agreement(best: GeoHit, passing: list[tuple[float, GeoHit]]) -> tuple[list, list]:
    """
    Each independent source's own best name-matching answer, compared with the
    chosen point: (families within AGREE_KM, [{source, km}] beyond DISAGREE_KM).
    """
    top: dict[str, tuple[float, GeoHit]] = {}
    for sc, h in passing:
        fam = SOURCE_FAMILY.get(h.provider, h.provider)
        if fam not in top or sc > top[fam][0]:
            top[fam] = (sc, h)
    agree, disagree = [], []
    for fam, (_, h) in sorted(top.items()):
        km = haversine_km(h.lat, h.lon, best.lat, best.lon)
        if km <= AGREE_KM:
            agree.append(fam)
        elif km > DISAGREE_KM:
            disagree.append({"source": fam, "km": round(km, 1)})
    own = SOURCE_FAMILY.get(best.provider, best.provider)
    if own not in agree:
        agree.insert(0, own)
    return agree, disagree

# Independent data sources. Photon and Nominatim both serve OpenStreetMap, so
# their agreeing proves nothing; Google's two services are separate databases
# (a business index and an address geocoder) and do count twice.
SOURCE_FAMILY = {"google_places": "Google Places", "google": "Google Geocoding",
                 "photon": "OpenStreetMap", "nominatim": "OpenStreetMap", "arcgis": "Esri"}

# Added to a candidate's score once it has passed the name gate. At equal name
# match, prefer Google, which is materially better on Indian informal addresses.
PROVIDER_PRIOR = {"google_places": 4.0, "google": 3.0}

AGREE_KM = 0.3        # two sources within this of each other corroborate a point
DISAGREE_KM = 1.0     # a source whose own best answer is further than this disagrees


class Geocoder:
    """
    Resolves a location string to a coordinate.

    Precedence, highest first:
      1. geocode_overrides.json  - hand-edited, keyed by the exact stop string.
         These are the only points treated as verified.
      2. geocode_cache.json      - every query ever issued, keyed by query text.
         Google entries expire after 30 days (Google's storage limit).
      3. a live provider call.
    """

    def __init__(self, cache_dir: Path, provider: str | None = None,
                 offline: bool = False, corpus: list[str] | None = None):
        cache_dir = Path(cache_dir)
        self.idf = build_idf(corpus or [])
        self.cache = JsonCache(cache_dir / "geocode_cache.json")
        self.overrides_path = cache_dir / "geocode_overrides.json"
        self.overrides = self._load_overrides()
        self.provider = (provider or os.environ.get("GEOCODER", "photon")).lower()
        self.offline = offline
        self.calls = 0
        self.google_calls = 0
        self.expired = self._purge_expired()

    def _purge_expired(self) -> int:
        """Delete Google results past the storage limit, whether or not re-queried."""
        stale = [k for k, v in self.cache.data.items()
                 if isinstance(v, dict) and "fetched_at" in v and not gmaps.is_fresh(v["fetched_at"])]
        for k in stale:
            del self.cache.data[k]
        if stale:
            self.cache._dirty = True
        return len(stale)

    @property
    def primary(self) -> list[str]:
        if self.provider == "google":
            # Degrade to the free stack if Google refused us earlier in this run.
            return ["google_places", "google"] if (gmaps.available() or self.offline) else ["photon"]
        return [self.provider]

    @property
    def fallback(self) -> list[str]:
        return ["photon", "nominatim"] if self.provider == "google" else ["nominatim"]

    def _load_overrides(self) -> dict:
        if not self.overrides_path.exists():
            self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
            self.overrides_path.write_text(json.dumps({
                "_README": [
                    "Hand-edited corrections. These always win over the cache and are",
                    "the ONLY points the dashboard shows as verified.",
                    "Key on the exact stop string as it appears in the source files.",
                    "For a stop whose cell packs a pickup and a drop location, suffix",
                    "the key with '||P' or '||D' to correct one half only.",
                    "Delete the leading underscore keys; they are ignored.",
                    "Example:",
                    "  \"K-42 near kwality store bazar\": {",
                    "     \"lat\": 28.6412, \"lon\": 77.2003,",
                    "     \"note\": \"confirmed against street view, 2026-09\" }",
                ]
            }, indent=1), encoding="utf-8")
        try:
            raw = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return {k: v for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, dict)
                and "lat" in v and "lon" in v}

    def _live(self, query: str, provider: str, circle: tuple | None = None) -> list[dict] | None:
        """Live call. None means a Google failure, which must not be cached."""
        if provider in GOOGLE_PROVIDERS:
            try:
                if circle:
                    hits = geocode_google_places(query, circle=circle)
                else:
                    hits = (geocode_google_places if provider == "google_places"
                            else geocode_google)(query)
            except gmaps.GoogleError:
                return None
            self.calls += 1
            self.google_calls += 1
            return [h.as_dict() for h in hits]
        fn, sleep = {
            "photon": (geocode_photon, SLEEP_PHOTON),
            "nominatim": (geocode_nominatim, SLEEP_NOMINATIM),
            "arcgis": (geocode_arcgis, 0.3),
        }[provider]
        try:
            hits = fn(query)
            self.calls += 1
            time.sleep(sleep)
            return [h.as_dict() for h in hits]
        except Exception as exc:                      # network, quota, malformed
            self.calls += 1
            time.sleep(sleep)
            return [{"_error": f"{type(exc).__name__}: {exc}"}]

    def _cached_query(self, query: str, provider: str, live: bool = True,
                      circle: tuple | None = None) -> list[dict]:
        tag = provider + (f"@{circle[0]:.3f},{circle[1]:.3f},{int(circle[2])}" if circle else "")
        key = f"{tag}::{query}"
        got = self.cache.get(key)
        if got is not None:
            if not isinstance(got, dict):
                return got
            if gmaps.is_fresh(got.get("fetched_at")):
                return got.get("hits", [])
            del self.cache.data[key]                  # past Google's storage limit
            self.cache._dirty = True
        if self.offline or not live:
            return []
        google = provider in GOOGLE_PROVIDERS
        if google and not gmaps.available():
            return []
        hits = self._live(query, provider, circle)
        if hits is None:
            return []
        self.cache.put(key, {"fetched_at": gmaps.stamp(), "hits": hits} if google else hits)
        return hits

    def _hits(self, query: str, provider: str, live: bool = True,
              circle: tuple | None = None) -> list[GeoHit]:
        raw = self._cached_query(query, provider, live=live, circle=circle)
        return [GeoHit(**{k: v for k, v in h.items() if not k.startswith("_")})
                for h in raw if "_error" not in h]

    def resolve(self, stop_text: str, override_key: str | None = None,
                bias: tuple | None = None, max_km: float | None = None,
                deep: bool = False) -> dict:
        """
        Resolve one location string. Returns a record ready for the review CSV.

        `override_key` is the exact source stop string (optionally suffixed
        ||P / ||D), so a human can correct one half of a combined cell.
        `bias` / `max_km` give route context: candidates further than max_km
        from bias are rejected and nearer ones preferred.
        `deep` also asks Nominatim and ArcGIS with every query variant and with
        region hints appended - slower, used for stops the fast pass missed.
        With Google on, `deep` plus `bias` also runs a Places search centred on
        the route, which separates Delhi's Model Town from Ghaziabad's.

        The record also says which independent sources put the point within
        AGREE_KM of the answer (`agree`) and which put it more than DISAGREE_KM
        away (`disagree`) - the fastest guide to which pins a human must check.
        """
        text = _norm(stop_text)
        rec = {
            "query_text": text, "lat": None, "lon": None, "label": "",
            "confidence": "none", "provider": "", "matched_query": "",
            "raw_type": "", "source": "unresolved", "verified": False,
            "note": "", "place_id": "", "agree": [], "disagree": [],
        }
        if not text:
            rec["source"] = "empty"
            return rec

        for key in filter(None, [override_key, text]):
            ov = self.overrides.get(key)
            if ov:
                rec.update({
                    "lat": float(ov["lat"]), "lon": float(ov["lon"]),
                    "label": _norm(ov.get("label", "")) or "(human override)",
                    "confidence": "high", "provider": "override",
                    "matched_query": key, "source": "override",
                    "verified": True, "note": _norm(ov.get("note", "")),
                })
                return rec

        best, best_score = None, 0.0
        passing: list[tuple[float, GeoHit]] = []

        def consider(hits, eligible: bool = True):
            nonlocal best, best_score
            for h in hits:
                sc = score_hit(h, text, self.idf, bias, max_km)
                if sc <= 0:
                    continue
                sc += PROVIDER_PRIOR.get(h.provider, 0.0)
                passing.append((sc, h))
                if eligible and sc > best_score:
                    best, best_score = h, sc

        variants = query_variants(text)
        for v in variants:
            for p in self.primary:
                consider(self._hits(v, p))
            if best is not None and best_score >= 45 and not deep:
                break

        if deep and bias is not None and "google_places" in self.primary:
            radius = (max_km or 12.0) * 1000.0
            for v in variants[:2]:
                consider(self._hits(v, "google_places", circle=(bias[0], bias[1], radius)))

        # Free fallback, in exactly the order the free stack uses on its own:
        # Photon over every variant until a strong match, then Nominatim.
        if (best is None or best_score < 30 or deep) and "photon" not in self.primary \
                and "photon" in self.fallback:
            for v in variants:
                consider(self._hits(v, "photon"))
                if best is not None and best_score >= 45 and not deep:
                    break
        if best is None or best_score < 30 or deep:
            for v in variants[:1] if not deep else variants:
                consider(self._hits(v, "nominatim"))

        if deep and (best is None or best_score < 45):
            hinted = [f"{v}, {h}" for v in variants[:2] for h in REGION_HINTS]
            for v in variants + hinted:
                consider(self._hits(v, "arcgis"))
            for v in hinted[:4]:
                consider(self._hits(v, "nominatim"))

        # Corroboration from answers the free providers already gave for these
        # exact queries. Cache reads only - never a live call, never the winner.
        if self.provider == "google":
            for p in ("photon", "nominatim", "arcgis"):
                for v in variants:
                    consider(self._hits(v, p, live=False), eligible=False)

        if best is not None:
            rec.update({
                "lat": best.lat, "lon": best.lon, "label": best.label,
                "confidence": best.confidence, "provider": best.provider,
                "matched_query": best.query, "raw_type": best.raw_type,
                "source": "geocoded", "verified": False, "place_id": best.place_id,
            })
            rec["agree"], rec["disagree"] = _agreement(best, passing)
        return rec

    def save(self):
        self.cache.save()


# --------------------------------------------------------------------------
# Routing providers
# --------------------------------------------------------------------------

def route_osrm(a: tuple[float, float], b: tuple[float, float]) -> dict:
    """Free-flow driving duration and geometry between two coordinates."""
    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}"
        f"?overview=simplified&geometries=geojson&alternatives=false&steps=false"
    )
    data = _get_json(url)
    if data.get("code") != "Ok" or not data.get("routes"):
        return {"_error": f"osrm: {data.get('code', 'no route')}"}
    r = data["routes"][0]
    coords = (r.get("geometry", {}) or {}).get("coordinates", []) or []
    return {
        "duration_s": float(r["duration"]),
        "distance_m": float(r["distance"]),
        "duration_traffic_s": None,          # OSRM has no traffic model
        "geometry": [[round(c[1], 5), round(c[0], 5)] for c in coords],
        "provider": "osrm",
    }


def route_google(a, b, departure_epoch: int | None = None) -> dict:
    """
    Traffic-aware driving time for one leg: `duration_traffic_s` is Google's
    prediction for that departure time on a typical day like it, `duration_s`
    the free-flow (static) time. Raises gmaps.GoogleError on failure.
    """
    r = gmaps.compute_route([a, b], departure_epoch, traffic=True)
    if r is None:
        return {"_error": "google: no drivable route"}
    r.pop("legs", None)
    return r


class Router:
    """
    Road times for consecutive stop pairs, cached by coordinate + hour band.

    With ROUTER=google each leg is requested at its printed time on the next
    representative school day, and carries both the predicted duration with
    traffic and the free-flow one. If Google refuses us mid-run the remaining
    legs fall back to OSRM (free-flow), labelled as such in `provider`.
    """

    def __init__(self, cache_dir: Path, provider: str | None = None, offline: bool = False):
        self.cache = JsonCache(Path(cache_dir) / "route_times_cache.json")
        self.provider = (provider or os.environ.get("ROUTER", "osrm")).lower()
        self.offline = offline
        self.calls = 0
        self.google_calls = 0
        self.failures = 0
        stale = [k for k, v in self.cache.data.items()
                 if isinstance(v, dict) and v.get("provider") == "google"
                 and not gmaps.is_fresh(v.get("fetched_at"))]
        for k in stale:
            del self.cache.data[k]
        self.cache._dirty = self.cache._dirty or bool(stale)
        self.expired = len(stale)

    @staticmethod
    def _key(a, b, provider, hour) -> str:
        # Hour band is part of the key so a traffic-aware provider stores one
        # entry per departure hour without colliding with OSRM's.
        return f"{provider}|{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}|h{hour}"

    def leg(self, a, b, depart_hour: int | None = None, depart_epoch: int | None = None,
            depart_min: float | None = None) -> dict:
        """
        Driving time from a to b, departing at `depart_min` (minutes after
        midnight, IST) or `depart_hour`. Returns {} when unavailable - callers
        must render that as "no road time available", never as zero.
        """
        if not a or not b or a[0] is None or b[0] is None:
            return {}
        if depart_min is not None and depart_min == depart_min:      # not NaN
            depart_hour = int(depart_min // 60)
        else:
            depart_min = depart_hour * 60 if depart_hour is not None else None
        hour = depart_hour if depart_hour is not None else "na"
        provider = self.provider
        if provider == "google" and not self.offline and not gmaps.available():
            provider = "osrm"
        key = self._key(a, b, provider, hour)
        got = self.cache.get(key)
        if got is not None:
            return {} if "_error" in got else got
        if self.offline:
            # A Google build viewed offline still shows OSRM times it already has.
            if provider == "google":
                got = self.cache.data.get(self._key(a, b, "osrm", hour))
                return got if got and "_error" not in got else {}
            return {}
        try:
            if provider == "google":
                if depart_epoch is None and depart_min is not None:
                    depart_epoch = gmaps.school_day_epoch(depart_min)
                res = route_google(a, b, depart_epoch)
                self.google_calls += 1
            else:
                res = route_osrm(a, b)
                time.sleep(SLEEP_OSRM)
            self.calls += 1
        except gmaps.GoogleUnavailable:
            self.failures += 1
            return self.leg(a, b, depart_hour, None, depart_min)       # now osrm
        except gmaps.GoogleError:
            self.failures += 1                                          # not cached
            return {}
        except Exception as exc:
            res = {"_error": f"{type(exc).__name__}: {exc}"}
            self.calls += 1
            time.sleep(SLEEP_OSRM)
        if "_error" in res:
            self.failures += 1
        self.cache.put(key, res)
        return {} if "_error" in res else res

    def save(self):
        self.cache.save()
