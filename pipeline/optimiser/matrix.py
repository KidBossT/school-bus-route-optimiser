"""
Stop-to-stop travel-time matrices for the optimiser.

Re-assigning stops between buses needs times between stops that no bus links
today, so the consecutive-leg road times in geo.Router are not enough. A full
N x N matrix of real road times is also not needed and, on Google, not cheap
(~250 stops -> 62,500 elements). The compromise used here:

  * real road times for each stop's k nearest neighbours, and to/from school
    (a bus only ever drives between nearby stops - that is what the search
    uses), roughly 250 x 2k elements instead of 250^2;
  * every other pair estimated from straight-line distance, scaled by the
    median road/straight-line ratio measured on the real pairs.

Estimated cells are flagged, and every report says what share of a plan's
legs rest on an estimate.

Providers: `haversine` (offline, no calls - use until coordinates are in),
`osrm` (free, free-flow, public demo server), `google` (Routes API
computeRouteMatrix, traffic-aware at a given departure hour; key read from
GOOGLE_MAPS_API_KEY via .env), `google_static` (the same, without traffic:
the Essentials SKU, twice the free allowance and 625 elements per request). Every element is cached, keyed by coordinates,
provider and hour, so a warm rerun makes no calls.

Google bills the matrix per ELEMENT. Requests are built one origin at a time
so that every billed element is one the optimiser asked for, and the whole
fetch is checked against the monthly cap (gmaps.py) BEFORE the first request:
a fetch that does not fit is not started, rather than stopping half-way with
the month's allowance spent on a partial matrix.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

import geo
import gmaps

CACHE_FILE = "optimiser_matrix_cache.json"
OSRM = "https://router.project-osrm.org"
OSRM_CHUNK = 25          # origins per table request
GOOGLE_TRAFFIC_ELEMENTS = gmaps.MAX_MATRIX_ELEMENTS_TRAFFIC_OPTIMAL
GOOGLE_EPM = 2_500       # elements per minute; Google's limit is 3,000
ESTIMATE_BANDS_KM = [(0, 1), (1, 2), (2, 4), (4, 8), (8, 1e9)]   # straight-line km, for estimated pairs


def haversine_matrix(points: list[tuple[float, float]]) -> np.ndarray:
    lat = np.radians([p[0] for p in points])[:, None]
    lon = np.radians([p[1] for p in points])[:, None]
    dlat, dlon = lat.T - lat, lon.T - lon
    h = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def _key(provider, hour, a, b) -> str:
    return f"{provider}|h{hour}|{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}"


# --------------------------------------------------------------------------
# provider block fetchers: origins x destinations -> {(i, j): (sec, metres)}
# --------------------------------------------------------------------------

def _osrm_block(origins, dests) -> dict:
    pts = origins + dests
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in pts)
    src = ";".join(str(i) for i in range(len(origins)))
    dst = ";".join(str(len(origins) + j) for j in range(len(dests)))
    url = (f"{OSRM}/table/v1/driving/{coords}?sources={src}&destinations={dst}"
           f"&annotations=duration,distance")
    data = geo._get_json(url)
    time.sleep(geo.SLEEP_OSRM)
    if data.get("code") != "Ok":
        raise RuntimeError(f"osrm table: {data.get('code')}")
    out = {}
    for i, (drow, krow) in enumerate(zip(data["durations"], data["distances"])):
        for j, (d, k) in enumerate(zip(drow, krow)):
            if d is not None and k is not None:
                out[(i, j)] = (float(d), float(k))
    return out


def _google_block(origins, dests, hour: int, traffic: bool = True) -> dict:
    """Seconds and metres at `hour`:00 on a representative school day (traffic or static)."""
    res = gmaps.route_matrix(origins, dests, gmaps.school_day_epoch(hour * 60), traffic=traffic)
    return {ij: ((v[0] if traffic else v[1]), v[2]) for ij, v in res.items()}


def google_requests(todo: dict[int, set[int]], per_request: int | None = None
                    ) -> list[tuple[list[int], list[int]]]:
    """
    One origin per request, destinations in blocks of the per-request cap.
    Every billed element is a wanted pair. (Grouping origins would share
    requests but bill each origin for every other origin's destinations -
    about six times the elements for the same matrix.)
    """
    out = []
    for i in sorted(todo):
        ds = sorted(todo[i])
        step = per_request or GOOGLE_TRAFFIC_ELEMENTS
        for t in range(0, len(ds), step):
            out.append(([i], ds[t:t + step]))
    return out


# --------------------------------------------------------------------------

class MatrixBuilder:
    def __init__(self, cache_dir: Path, provider: str = "haversine", offline: bool = False,
                 detour: float = 1.35, speed_kmh: float = 22.0, neighbours: int = 15,
                 log=print):
        self.provider = provider.lower()
        self.offline = offline
        self.detour, self.speed = detour, speed_kmh
        self.k = neighbours
        self.cache = geo.JsonCache(Path(cache_dir) / CACHE_FILE)
        self.log = log
        self.calls = 0

    def _estimate(self, H):
        km = H * self.detour
        return km / self.speed * 60.0, km

    def _wanted(self, H) -> dict[int, set[int]]:
        """origin -> destinations worth a real road time."""
        n = len(H)
        want = {i: set() for i in range(n)}
        for i in range(1, n):
            want[0].add(i); want[i].add(0)
            for j in np.argsort(H[i])[1:self.k + 1]:
                j = int(j)
                if j:
                    want[i].add(j); want[j].add(i)
        return want

    def _fetch(self, points, want, hour) -> dict:
        got, todo = {}, {}
        google = self.provider in ("google", "google_static")
        traffic = self.provider == "google"
        sku = "route_matrix_pro" if traffic else "route_matrix_essentials"
        for i, js in want.items():
            for j in js:
                c = self.cache.get(_key(self.provider, hour, points[i], points[j]))
                # Google entries end with their fetch date and expire after the
                # 30-day storage limit: [sec, metres, date], or [date] for no route.
                if c is not None and google and not (c and isinstance(c[-1], str)
                                                     and gmaps.is_fresh(c[-1])):
                    c = None
                if c is not None:
                    if len(c) >= 2:
                        got[(i, j)] = (c[0], c[1])
                else:
                    todo.setdefault(i, set()).add(j)
        if not todo or self.offline:
            return got
        if google:
            blocks = google_requests(todo, None if traffic else 625)
            need = sum(len(o) * len(d) for o, d in blocks)
            left = gmaps.budget().remaining(sku)
            if not gmaps.available():
                self.log(f"    Google unavailable ({gmaps.disabled_reason() or 'no key'}); "
                         f"{need} pairs fall back to estimates")
                return got
            if need > left:
                self.log(f"    NOT FETCHED: this matrix needs {need} billable elements, "
                         f"{left} remain under this month's cap ({sku}). "
                         f"Lower `neighbours`, raise GMAPS_CAP_{sku.upper()} in .env, or "
                         f"use matrix_provider \"osrm\". Estimates used instead.")
                return got
            self.log(f"    fetching {need} {'traffic-aware' if traffic else 'static'} elements in {len(blocks)} requests "
                     f"(~{need / GOOGLE_EPM:.1f} min at the rate limit)")
        else:
            origins = sorted(todo)
            blocks = []
            for s in range(0, len(origins), OSRM_CHUNK):
                og = origins[s:s + OSRM_CHUNK]
                blocks.append((og, sorted(set().union(*(todo[i] for i in og)))))
        window: list[tuple[float, int]] = []
        for n, (og, dg) in enumerate(blocks):
            if google:
                # stay under the elements-per-minute limit
                now = time.monotonic()
                window = [(t, e) for t, e in window if now - t < 60]
                size = len(og) * len(dg)
                if sum(e for _, e in window) + size > GOOGLE_EPM:
                    time.sleep(max(60 - (now - window[0][0]), 0) + 0.5)
                    window = []
                window.append((time.monotonic(), size))
            try:
                res = (_osrm_block if self.provider == "osrm" else
                       lambda o, d: _google_block(o, d, hour, traffic))(
                    [points[i] for i in og], [points[j] for j in dg])
            except gmaps.GoogleUnavailable as exc:
                self.log(f"    Google stopped the fetch ({exc}); the remaining pairs "
                         f"fall back to estimates")
                break
            except Exception as exc:
                self.log(f"    matrix request failed ({type(exc).__name__}: {exc}); "
                         f"those pairs fall back to estimates")
                continue
            finally:
                self.calls += 1
            for a, i in enumerate(og):
                for b, j in enumerate(dg):
                    if j not in todo[i]:
                        continue
                    v = res.get((a, b))
                    self.cache.put(_key(self.provider, hour, points[i], points[j]),
                                   (list(v) if v else []) + ([gmaps.stamp()] if google else []))
                    if v:
                        got[(i, j)] = v
            if n % 20 == 19:
                self.cache.save()
        self.cache.save()
        return got

    def build(self, points: list[tuple[float, float]], hour: int, multiplier: float = 1.0):
        """
        points[0] is the school. Returns (minutes, km, estimated) matrices.
        """
        H = haversine_matrix(points)
        T, K = self._estimate(H)
        est = np.ones_like(T, dtype=bool)
        np.fill_diagonal(est, False)
        if self.provider != "haversine" and len(points) > 1:
            real = self._fetch(points, self._wanted(H), hour)
            if real:
                idx = tuple(np.array(list(real)).T)
                rt = np.array([v[0] / 60.0 for v in real.values()])
                rk = np.array([v[1] / 1000.0 for v in real.values()])
                ok = T[idx] > 0.05
                # calibrate the estimate on this network's real pairs, per straight-line distance
                # band: one factor fits the many short neighbour hops and leaves long legs (a 16 km
                # run to school) at the 22 km/h of a lane, twice their real time
                ft = float(np.median(rt[ok] / T[idx][ok])) if ok.any() else 1.0
                fk = float(np.median(rk[ok] / K[idx][ok])) if ok.any() else 1.0
                F = np.full_like(T, ft)
                hr, bands = H[idx], []
                for lo, hi in ESTIMATE_BANDS_KM:
                    sel = ok & (hr >= lo) & (hr < hi)
                    if sel.sum() >= 20:
                        f = float(np.median(rt[sel] / T[idx][sel]))
                        F[(H >= lo) & (H < hi)] = f
                        bands.append(f"{lo}-{hi if hi < 1e9 else ''} km x{f:.2f}")
                T, K = T * F, K * fk
                T[idx], K[idx] = rt, rk
                est[idx] = False
                self.log(f"    {len(real)} real road times, {int(est.sum())} estimated "
                         f"(time {', '.join(bands) or f'x{ft:.2f}'}; km x{fk:.2f} vs straight-line model)")
        np.fill_diagonal(T, 0.0)
        np.fill_diagonal(K, 0.0)
        return T * multiplier, K, est
