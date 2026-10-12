"""
Route planner: current route vs optimised route, per bus, on one map.

    python pipeline/build_routes.py            -> outputs/routes.html

For every route:
  CURRENT   the stops in the printed order, with an estimated time at each stop.
  OPTIMISED the same stops re-ordered to minimise driving time, ending at school
            in the morning and starting from school in the afternoon.

Road times (ROUTER in .env):
  google    Google Maps is the model that gets optimised. The stop ORDER is
            searched on Google's traffic-aware travel times (Route Matrix):
            each stop to its PLAN_NEIGHBOURS nearest stops and to/from school,
            each origin departing at its own printed time on the next
            representative school day. Pairs outside that neighbourhood are
            estimated from straight-line distance at the run's own measured
            Google pace. Both the printed order and the proposal are then TIMED
            with one traffic-aware computeRoutes request each; a proposal
            Google says is not faster is dropped. The whole matrix is costed
            BEFORE any request: if it does not fit this month's cap, the search
            falls back to OSRM and the log says so.
  osrm      free-flow throughout, no traffic.

Calibration (calibration.py; on by default, --raw-google turns it off):
Google's car times run about twice the printed timetable on short hops and
close to it on long legs, so every Google leg is scaled by a factor that
depends on the leg's length, fitted 80% on the printed timetable and 20% on
whole-run tap times, and checked route by route before it is used. The same
scaled times drive the order search, so the optimisation and the displayed
times agree. The fitted model is saved to cache/calibration.json for the
network optimiser.

Only stops with a usable coordinate can be routed. The rest are listed, not
hidden. Coordinates come from the geocoders and are unverified; a wrong
coordinate produces a wrong plan, so treat the optimised order as a proposal to
check on the ground, not an instruction.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibration as cal
import geo
import gmaps
import transport_spine as ts
from optimiser import consolidate
from optimiser.model import Node

MERGE_RADIUS_M = 250     # stops closer than this (walking) are suggested for merging
MOVED_KM = 0.05          # a Google pin this far from the old free pin counts as moved

DWELL_MIN = 1.0          # minutes the bus stands at each stop for boarding
PLAN_NEIGHBOURS = 7      # real Google times from each stop to this many nearest stops (3,812 elements/build, fits the cap)
MATRIX_BAND_MIN = 15     # departure times are rounded to this for requests and the cache
OSRM = "https://router.project-osrm.org"
SLEEP = 0.4


OFFLINE = False
ROUTER = "osrm"
USE_GOOGLE_MATRIX = False     # decided per build, after costing the whole matrix


def osrm(path: str, cache: geo.JsonCache):
    got = cache.get(path)
    if got is not None:
        return got
    if OFFLINE:
        return {"code": "offline"}
    req = urllib.request.Request(OSRM + path, headers={"User-Agent": geo.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        data = {"code": f"error: {exc}"}
    time.sleep(SLEEP)
    cache.put(path, data)
    return data


def coords_str(pts):
    return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in pts)


def table(pts, cache):
    d = osrm(f"/table/v1/driving/{coords_str(pts)}?annotations=duration,distance", cache)
    if d.get("code") != "Ok":
        return None
    return np.array(d["durations"], dtype=float) / 60.0


def _band(minute: float) -> int:
    return int(round(minute / MATRIX_BAND_MIN) * MATRIX_BAND_MIN) % 1440


def wanted_pairs(pts, morning: bool, k: int = PLAN_NEIGHBOURS) -> set[tuple[int, int]]:
    """
    Node 0 is the school. A morning path ends at school, so it needs stop->stop
    and stop->school; an afternoon path starts there, so stop->stop and
    school->stop. Stop->stop only to each stop's k nearest, both directions.
    """
    n = len(pts)
    want = set()
    for i in range(1, n):
        near = sorted((j for j in range(1, n) if j != i),
                      key=lambda j: geo.haversine_km(*pts[i], *pts[j]))[:k]
        for j in near:
            want.add((i, j)); want.add((j, i))
        want.add((i, 0) if morning else (0, i))
    return want


def _gm_key(depart: int, a, b) -> str:
    return f"google-matrix|{depart}|{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}"


def matrix_requests(pts, departs, morning, cache) -> dict[int, list[int]]:
    """origin -> destinations still missing from the cache (the billable part)."""
    todo: dict[int, list[int]] = {}
    for i, j in sorted(wanted_pairs(pts, morning)):
        got = cache.data.get(_gm_key(departs[i], pts[i], pts[j]))
        if not (got and gmaps.is_fresh(got.get("fetched_at"))):
            todo.setdefault(i, []).append(j)
    return todo


def google_table(pts, departs, morning, cache) -> np.ndarray | None:
    """
    Minutes between nodes on Google's traffic-aware model, each origin leaving
    at departs[i]. Unrequested pairs: straight-line km x this run's median
    Google minutes per straight-line km. None if Google gave nothing usable.
    """
    n = len(pts)
    for i, js in matrix_requests(pts, departs, morning, cache).items():
        if OFFLINE or not gmaps.available():
            break
        try:
            res = gmaps.route_matrix([pts[i]], [pts[j] for j in js],
                                     gmaps.school_day_epoch(departs[i]), traffic=True)
        except gmaps.GoogleUnavailable:
            break
        except gmaps.GoogleError:
            continue
        for b, j in enumerate(js):
            v = res.get((0, b))
            cache.put(_gm_key(departs[i], pts[i], pts[j]),
                      {"min": round(v[0] / 60.0, 3) if v else None, "fetched_at": gmaps.stamp()})
    H = np.array([[geo.haversine_km(*a, *b) for b in pts] for a in pts])
    D = np.full((n, n), np.nan)
    for i, j in wanted_pairs(pts, morning):
        got = cache.data.get(_gm_key(departs[i], pts[i], pts[j]))
        if got and gmaps.is_fresh(got.get("fetched_at")) and got.get("min") is not None:
            D[i, j] = got["min"]
    real = ~np.isnan(D) & (H > 0.05)
    if not real.any():
        return None
    pace = float(np.median(D[real] / H[real]))         # Google minutes per straight-line km
    D = np.where(np.isnan(D), H * pace, D)
    np.fill_diagonal(D, 0.0)
    return D


def node_departs(stops, school, morning) -> list[int]:
    """Departure band for each node: a stop's own printed time; school's for the PM start."""
    first = stops[0]["printed"] - (0 if morning else geo.haversine_km(
        *school, stops[0]["lat"], stops[0]["lon"]) * 1.35 / 22 * 60)
    return [_band(first)] + [_band(s["printed"]) for s in stops]


def route_osrm(pts, cache):
    d = osrm(f"/route/v1/driving/{coords_str(pts)}?overview=simplified&geometries=geojson", cache)
    if d.get("code") != "Ok" or not d.get("routes"):
        return None
    r = d["routes"][0]
    legs = [l["duration"] / 60.0 for l in r["legs"]]
    return {
        "legs_min": legs, "legs_free_min": legs,
        "km": r["distance"] / 1000.0,
        "geometry": [[round(c[1], 5), round(c[0], 5)] for c in r["geometry"]["coordinates"]],
        "provider": "osrm",
    }


def route_google(pts, depart_min: float, cache):
    """
    One traffic-aware request for the whole run. Google advances the clock
    along the route, so each leg is priced at the time the bus reaches it.
    Returns None if Google is unavailable, so the caller can fall back.
    """
    key = f"google|{int(round(depart_min)) % 1440}|{coords_str(pts)}"
    got = cache.get(key)
    if got is not None and gmaps.is_fresh(got.get("fetched_at")):
        return got
    if OFFLINE or not gmaps.available():
        return None
    try:
        r = gmaps.compute_route(pts, gmaps.school_day_epoch(depart_min))
    except gmaps.GoogleError:
        return None
    if r is None or len(r["legs"]) != len(pts) - 1:
        return None
    out = {
        "legs_min": [(l["duration_traffic_s"] or l["duration_s"]) / 60.0 for l in r["legs"]],
        "legs_free_min": [l["duration_s"] / 60.0 for l in r["legs"]],
        "km": r["distance_m"] / 1000.0, "geometry": r["geometry"],
        "provider": "google", "depart": r["depart"], "fetched_at": r["fetched_at"],
    }
    cache.put(key, out)
    return out


def route(pts, cache, depart_min: float | None = None):
    if ROUTER == "google" and depart_min is not None:
        r = route_google(pts, depart_min, cache)
        if r is not None:
            return r
    return route_osrm(pts, cache)


def purge_expired(cache: geo.JsonCache) -> int:
    """Google route results past the 30-day storage limit are deleted."""
    stale = [k for k, v in cache.data.items() if k.startswith("google")
             and not gmaps.is_fresh((v or {}).get("fetched_at"))]
    for k in stale:
        del cache.data[k]
    cache._dirty = cache._dirty or bool(stale)
    return len(stale)


# --------------------------------------------------------------------------
# Open path from node 0 visiting every other node once (no return)
# --------------------------------------------------------------------------

def solve_path(D: np.ndarray) -> list[int]:
    n = len(D)
    if n <= 2:
        return list(range(n))
    if n <= 13:                                   # exact, Held-Karp
        INF = float("inf")
        full = 1 << n
        best = {}
        best[(1, 0)] = (0.0, -1)
        for mask in range(1, full, 2):            # node 0 always in mask
            for j in range(n):
                if not (mask >> j) & 1 or (mask, j) not in best:
                    continue
                cost, _ = best[(mask, j)]
                for k in range(1, n):
                    if (mask >> k) & 1:
                        continue
                    nm = mask | (1 << k)
                    c = cost + D[j][k]
                    if c < best.get((nm, k), (INF, -1))[0]:
                        best[(nm, k)] = (c, j)
        end = min(range(1, n), key=lambda k: best.get((full - 1, k), (INF, -1))[0])
        path, mask, j = [], full - 1, end
        while j != -1:
            path.append(j)
            _, prev = best[(mask, j)]
            mask &= ~(1 << j)
            j = prev
        return path[::-1]

    # heuristic: nearest neighbour then 2-opt on the open path
    path, left = [0], set(range(1, n))
    while left:
        j = min(left, key=lambda k: D[path[-1]][k])
        path.append(j); left.remove(j)

    def cost(p):
        return sum(D[a][b] for a, b in zip(p, p[1:]))
    improved = True
    while improved:
        improved = False
        for i in range(1, n - 1):
            for j in range(i + 1, n):
                q = path[:i] + path[i:j + 1][::-1] + path[j + 1:]
                if cost(q) + 1e-9 < cost(path):
                    path, improved = q, True
    return path


def run_legs(seq, school, morning, cache, start=None):
    """Road-model legs for driving `seq` in order (with the school leg), unscaled."""
    wp = ([(s["lat"], s["lon"]) for s in seq] + [school]) if morning \
        else ([school] + [(s["lat"], s["lon"]) for s in seq])
    if morning:
        depart = seq[0]["printed"] if start is None else start
    elif start is not None:
        depart = start
    else:   # leave school roughly one straight-line leg before the first printed drop
        depart = seq[0]["printed"] - geo.haversine_km(
            school[0], school[1], seq[0]["lat"], seq[0]["lon"]) * 1.35 / 22 * 60
    return route(wp, cache, depart)


def load_taps(inputs: Path, routes_p: Path, roster_p: Path):
    """First-tap traces for whole-run tap times, or None if there are no tap logs."""
    logs = sorted(inputs.glob("*RFID*Attendance*.xlsx"))
    if not logs:
        return None
    import build_data as bd
    spine = ts.build_all(routes_p, roster_p, logs[0])["spine"]
    return {"am": bd.first_tap_trace(spine, "am_pickup"),
            "pm": bd.first_tap_trace(spine, "pm_dropoff"),
            "school": bd.school_arrival_trace(spine)}


def tap_run(taps, rk: str, leg: str, first: str, last: str) -> tuple[float | None, int]:
    """Median whole-run tap time: first stop -> school (am), first -> last drop (pm)."""
    if taps is None:
        return None, 0
    t = taps[leg]
    a = t[(t.route_key == rk) & (t.stop_name == first)][["date", "arrival_min"]]
    if leg == "am":
        b = taps["school"][taps["school"].route_key == rk][["date", "school_arrival_min"]]
        m = a.merge(b, on="date")
        d = m.school_arrival_min - m.arrival_min
    else:
        b = t[(t.route_key == rk) & (t.stop_name == last)][["date", "arrival_min"]]
        m = a.merge(b.rename(columns={"arrival_min": "last"}), on="date")
        d = m["last"] - m.arrival_min
    d = d[(d > 0) & (d < 180)]
    return (float(d.median()), int(len(d))) if len(d) else (None, 0)


def calibration_runs(out, school, cache, taps) -> list:
    """One calibration.Run per route and session, from the current (printed) order."""
    runs = []
    for it in out:
        rk = f"R{it['num']:02d}"
        for leg, morning in (("am", True), ("pm", False)):
            seq = it[leg]["_stops"]
            if len(seq) < 2:
                continue
            r = run_legs(seq, school, morning, cache)
            if r is None:
                continue
            g = r["legs_min"]
            stop_g = g[:len(seq) - 1] if morning else g[1:]
            legs = []
            for i in range(len(seq) - 1):
                a, b = seq[i], seq[i + 1]
                gap = b["printed"] - a["printed"] - DWELL_MIN
                legs.append(cal.Leg(geo.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]),
                                    stop_g[i], gap if gap >= 0 else None,
                                    verified=bool(a.get("verified") and b.get("verified"))))
            school_leg = (cal.Leg(geo.haversine_km(seq[-1]["lat"], seq[-1]["lon"], *school), g[-1],
                                  verified=bool(seq[-1].get("verified")))
                          if morning else None)
            tap, days = tap_run(taps, rk, leg, seq[0]["name"], seq[-1]["name"])
            runs.append(cal.Run(it["route"], leg, legs, DWELL_MIN * (len(seq) - 1), school_leg,
                                tap, days, [x["printed"] - seq[0]["printed"] for x in seq]))
    return runs


def gps_calibration_runs(school, cache, cache_dir: Path) -> list:
    """
    One calibration.Run per run logged on the bus (gps_runs.py). Each leg is the
    measured time from leaving one stop to reaching the next, priced by Google
    between the measured stop positions, departing when the bus really left.
    """
    import gps_runs
    runs = []
    for g in gps_runs.load_runs(cache_dir):
        morning = g["session"] == "am"
        st = [s for s in g["stops"] if not s["moving_at_tap"]]
        sch = g.get("school") or {}
        if not st:
            continue
        seq = [{"name": s["name"], "lat": s["lat"], "lon": s["lon"], "printed": s["printed"]} for s in st]
        # nodes in driving order, with the school end where Google routes it
        nodes = ([(s["lat"], s["lon"], s["arr_min"], s["dep_min"]) for s in st]
                 + [(*school, sch.get("arr_min"), None)]) if morning \
            else [(*school, None, sch.get("dep_min"))] + [(s["lat"], s["lon"], s["arr_min"], s["dep_min"]) for s in st]
        first = 0 if nodes[0][3] is not None else 1     # afternoon logged after leaving school
        if first >= len(nodes) - 1 or nodes[first][3] is None:
            continue
        r = run_legs(seq, school, morning, cache, nodes[first][3])
        if r is None:
            continue
        legs, offsets = [], []
        for i in range(first, len(nodes) - 1):
            a, b = nodes[i], nodes[i + 1]
            if a[3] is None or b[2] is None:
                break
            legs.append(cal.Leg(geo.haversine_km(a[0], a[1], b[0], b[1]), r["legs_min"][i],
                                gps=b[2] - a[3], verified=True))
            offsets.append(b[2] - nodes[first][3])
        if legs:
            runs.append(cal.Run(g["route"], g["session"], legs, DWELL_MIN * (len(legs) - 1),
                                gps_offsets=offsets,
                                gps_dwell_s=[s["dwell_s"] for s in st if s["dwell_s"] is not None]))
    return runs


def plan(stops: list[dict], school: tuple, morning: bool, cache, scale=None) -> dict | None:
    """
    stops: [{name, lat, lon, printed}] in printed order, all with coordinates.
    Morning: path ending at school. Afternoon: path starting at school.
    `scale(km, verified)` is the calibration factor for a leg of that
    straight-line length, `verified` when both ends are confirmed pins (the
    school counts as confirmed); it is applied to the search matrix and to
    every timed leg.
    """
    if not stops:
        return None
    pts = [school] + [(s["lat"], s["lon"]) for s in stops]
    ok = [True] + [bool(s.get("verified")) for s in stops]

    def search(D):
        # Morning ends at school: solve on the transposed matrix from school,
        # then reverse - a path school->a->b->c on D^T is c->b->a->school on D.
        order = solve_path(D.T.copy() if morning else D)[1:]      # drop school node
        return order[::-1] if morning else order

    # Candidate orders: one searched on Google's matrix, one on OSRM's. On long
    # runs the Google matrix is partly estimated, so either can win; Google's
    # full-route timing below is the judge.
    candidates: dict[tuple, str] = {}
    if USE_GOOGLE_MATRIX:
        D = google_table(pts, node_departs(stops, school, morning), morning, cache)
        if D is not None:
            if scale is not None:
                D = D * np.array([[scale(geo.haversine_km(*a, *b), ok[i] and ok[j])
                                   for j, b in enumerate(pts)] for i, a in enumerate(pts)])
            candidates.setdefault(tuple(search(D)), "google")
    D = table(pts, cache)
    if D is not None:
        candidates.setdefault(tuple(search(D)), "osrm")
    if not candidates:
        return None

    def timed(seq, start=None):   # both plans depart at the same moment
        r = run_legs(seq, school, morning, cache, start)
        if r is None:
            return None
        wp = ([(x["lat"], x["lon"]) for x in seq] + [school]) if morning \
            else ([school] + [(x["lat"], x["lon"]) for x in seq])
        kms = [geo.haversine_km(*wp[i], *wp[i + 1]) for i in range(len(wp) - 1)]
        sure = ([bool(x.get("verified")) for x in seq] + [True]) if morning \
            else ([True] + [bool(x.get("verified")) for x in seq])
        legs = [l * (scale(k, sure[i] and sure[i + 1]) if scale else 1.0)
                for i, (l, k) in enumerate(zip(r["legs_min"], kms))]
        rows, t = [], None
        if morning:
            t = seq[0]["printed"] if start is None else start   # current: first printed pickup
            for i, s in enumerate(seq):
                if i:
                    t += legs[i - 1] + DWELL_MIN
                rows.append({"name": s["name"], "printed": s["printed"], "eta": round(t, 1),
                             "leg_min": round(legs[i - 1], 1) if i else None})
            t += legs[-1] + DWELL_MIN
            school_eta = round(t, 1)
        else:
            t = (seq[0]["printed"] - legs[0]) if start is None else start   # current: first drop as printed
            depart = round(t, 1)
            for i, s in enumerate(seq):
                t += legs[i] + (DWELL_MIN if i else 0)
                rows.append({"name": s["name"], "printed": s["printed"], "eta": round(t, 1),
                             "leg_min": round(legs[i], 1)})
            school_eta = depart
        return {"stops": rows, "drive_min": round(sum(legs), 1),
                "drive_model_min": round(sum(r["legs_min"]), 1),
                "drive_free_min": round(sum(r["legs_free_min"]), 1), "km": round(r["km"], 1),
                "school_time": school_eta, "geometry": r["geometry"], "road_source": r["provider"]}

    cur = timed(stops)
    if cur is None:
        return None
    start = cur["stops"][0]["eta"] if morning else cur["school_time"]
    opt, order_source = None, None
    for order, src in candidates.items():
        t = timed([stops[i - 1] for i in order], start)
        if t is not None and (opt is None or t["drive_min"] < opt["drive_min"]):
            opt, order_source = t, src
    if opt is None:
        return None
    note = None
    if opt["drive_min"] > cur["drive_min"]:
        # No candidate beats the printed order on Google's full-route timing.
        opt, note = cur, "reordering is not faster once traffic is included; printed order kept"
    return {"current": cur, "optimised": opt, "note": note, "order_source": order_source,
            "saved_min": round(cur["drive_min"] - opt["drive_min"], 1)}


LINK_KM = 6.0        # consecutive bus stops are rarely further apart than this
MIN_CLUSTER = 3      # three independent geocodes agreeing is strong evidence


def clusters(stops: list[dict]) -> list[list[dict]]:
    """Single-linkage groups: stops within LINK_KM of any member join the group."""
    left = list(stops); out = []
    while left:
        grp = [left.pop()]
        i = 0
        while i < len(grp):
            g = grp[i]; i += 1
            for x in left[:]:
                if geo.haversine_km(g["lat"], g["lon"], x["lat"], x["lon"]) <= LINK_KM:
                    grp.append(x); left.remove(x)
        out.append(grp)
    return sorted(out, key=len, reverse=True)


def split_trusted(stops: list[dict]) -> tuple[list[dict], list[dict], tuple | None]:
    """
    Trusted = members of any cluster of MIN_CLUSTER or more. A wrong pin is
    almost always alone; three geocodes that land within a few km of each other
    are almost never all wrong the same way. Returns (trusted, suspect, centre
    of the largest cluster).
    """
    if not stops:
        return [], [], None
    cl = clusters(stops)
    good = [s_ for g in cl if len(g) >= MIN_CLUSTER for s_ in g]
    if not good and cl:                        # tiny route: trust the biggest group
        good = cl[0]
    names = {t["name"] for t in good}
    trusted = [s_ for s_ in stops if s_["name"] in names]        # keep input order
    suspect = [s_ for s_ in stops if s_["name"] not in names]
    main = cl[0]
    centre = (float(np.median([m["lat"] for m in main])), float(np.median([m["lon"] for m in main])))
    for s_ in suspect:
        s_["km"] = min(geo.haversine_km(s_["lat"], s_["lon"], t["lat"], t["lon"]) for t in trusted)                    if trusted else 0.0
    return trusted, suspect, centre


MAX_KM_PER_MIN = 0.8      # 48 km/h straight-line between printed neighbours is already generous
SAME_MINUTE_KM = 3.0      # stops printed at the same minute must be within this


def peel_detours(stops: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    The printed order is a route a bus really drives, with printed times. Two
    stops printed a minute apart cannot be 7 km apart; a correctly placed stop
    does not force a 10 km detour between its printed neighbours. Peel the
    worst offender, recompute, repeat - greedy, so one wrong pin cannot
    incriminate the correct stops beside it.
    """
    keep, out = list(stops), []

    def leg_impossible(a_, b_):
        km = geo.haversine_km(a_["lat"], a_["lon"], b_["lat"], b_["lon"])
        ta, tb = a_.get("printed"), b_.get("printed")
        if ta is None or tb is None:
            return km > 12.0
        dt = abs(tb - ta)
        return km > (SAME_MINUTE_KM if dt < 1 else MAX_KM_PER_MIN * dt + 1.0)

    while len(keep) >= 3:
        bad_count, det = [], []
        for i, b_ in enumerate(keep):
            n = 0
            if i > 0 and leg_impossible(keep[i - 1], b_):
                n += 1
            if i < len(keep) - 1 and leg_impossible(b_, keep[i + 1]):
                n += 1
            bad_count.append(n)
            if 0 < i < len(keep) - 1:
                a_, c_ = keep[i - 1], keep[i + 1]
                det.append(geo.haversine_km(a_["lat"], a_["lon"], b_["lat"], b_["lon"])
                           + geo.haversine_km(b_["lat"], b_["lon"], c_["lat"], c_["lon"])
                           - geo.haversine_km(a_["lat"], a_["lon"], c_["lat"], c_["lon"]))
            else:
                det.append(0.0)
        if max(bad_count) == 0:
            med = float(np.median(det)) if det else 0.0
            worst = int(np.argmax(det))
            if det[worst] <= max(5.0, 4.0 * med) and det[worst] <= 15.0:
                break
        else:
            # among stops touching an impossible leg, drop the one that detours most
            worst = max(range(len(keep)), key=lambda i: (bad_count[i], det[i]))
        bad = keep.pop(worst)
        bad["km"] = det[worst]
        out.append(bad)
    return keep, out


def merge_suggestions(out, leg: str) -> list[dict]:
    """
    Stops (across all buses) within MERGE_RADIUS_M of each other, folded into
    the one that minimises walking - the optimiser's consolidation rule. Only
    placed stops; a proposal, never applied here.
    """
    nodes, where = [], {}
    for it in out:
        rk = f"R{it['num']:02d}"
        for p_ in it[leg]["points"]:
            nid = f"{p_['key']}@{rk}"
            nodes.append(Node(id=nid, name=p_["name"], lat=p_["lat"], lon=p_["lon"],
                              demand=max(int(p_["riders"]), 0), current_vehicle=rk,
                              verified=p_["verified"]))
            where[nid] = (p_["lat"], p_["lon"], it["route"],
                          len(p_["agree"]) >= 2 or p_["verified"])
    props = consolidate.propose(nodes, MERGE_RADIUS_M, 10 ** 6)
    for pr in props:
        h = where[pr["hub"]]
        pr["hub_lat"], pr["hub_lon"], pr["hub_route"], pr["hub_confirmed"] = h
        for m in pr["members"]:
            w = where[m["node"]]
            m["lat"], m["lon"], m["route"], m["confirmed"] = w
    return props


def build(inputs: Path, outputs: Path, cache_dir: Path, offline: bool = False,
          calibrate: bool = True) -> Path:
    routes_p = sorted(inputs.glob("Route*List*Area*.xlsx"))[0]
    roster_p = sorted(inputs.glob("Route*Wise*Student*.xlsx"))[0]
    routes = ts.parse_route_list(routes_p)
    roster = ts.parse_roster(roster_p)
    students = ts.build_student_dim(roster)

    reporting = students.groupby("roster_am_route_key").reporting_time_min.median()
    riders_am = students.groupby(["roster_am_route_key", "am_stop_name"]).size()
    riders_pm = students.groupby(["roster_pm_route_key", "pm_stop_name"]).size()

    global OFFLINE, ROUTER
    OFFLINE = offline
    ROUTER = os.environ.get("ROUTER", "osrm").lower()
    gc = geo.Geocoder(cache_dir, offline=offline,
                      corpus=[t for n in routes.stop_name.unique() for t in geo.split_pickup_drop(n)])
    # Override key must not start with "_": geo.Geocoder drops such keys
    # (that is how it skips "_README"). Same key as optimiser.build_problem.SCHOOL_KEY.
    sc = gc.resolve("Prometheus School Noida", override_key="Prometheus School Noida")
    school = (sc["lat"], sc["lon"]) if sc["lat"] else (geo.BIAS_LAT, geo.BIAS_LON)
    cache = geo.JsonCache(cache_dir / "route_plan_cache.json")
    purge_expired(cache)
    # Where the free map services put each stop, for "pin moved" arrows. Cache only.
    old_gc = geo.Geocoder(cache_dir, provider="photon", offline=True,
                          corpus=[t for n in routes.stop_name.unique() for t in geo.split_pickup_drop(n)])

    # collapse stops printed twice on the same route to their first appearance
    routes = routes.drop_duplicates(["route_key", "stop_name"]).sort_values(["route_key", "stop_seq"])

    out = []
    for rk, g in routes.groupby("route_key", sort=True):
        item = {"route": f"Route {int(rk[1:])}", "num": int(rk[1:]),
                "reporting": float(reporting.get(rk)) if pd.notna(reporting.get(rk)) else None,
                "children": int((students.roster_am_route_key == rk).sum())}
        for leg, morning in (("am", True), ("pm", False)):
            tcol = "promised_pickup_min" if morning else "promised_drop_min"
            rows = []
            for r in g.itertuples():
                p_text, d_text = geo.split_pickup_drop(r.stop_name)
                rows.append({
                    "name": r.stop_name,
                    "text": p_text if morning else d_text,
                    "key": r.stop_name if p_text == d_text
                           else f"{r.stop_name}||{'P' if morning else 'D'}",
                    "printed": None if pd.isna(getattr(r, tcol)) else float(getattr(r, tcol)),
                    "riders": int((riders_am if morning else riders_pm).get((rk, r.stop_name), 0)),
                })

            # The printed list is not always in time order; the schedule is.
            # "Current order" means the order the bus actually runs the stops.
            rows.sort(key=lambda w: (w["printed"] is None, w["printed"] or 0))

            # Pass 1: fast resolve, then find the route's centre from the stops
            # that agree with each other.
            for row in rows:
                row["rec"] = gc.resolve(row["text"], override_key=row["key"])
            pts1 = [{"name": w["name"], "lat": w["rec"]["lat"], "lon": w["rec"]["lon"],
                     "printed": w["printed"]}
                    for w in rows if w["rec"]["lat"] is not None]
            trusted, _, centre = split_trusted(pts1)
            trusted, _ = peel_detours(trusted)          # printed-order consistency
            trusted_names = {t["name"] for t in trusted}

            # Pass 2: everything unresolved or not agreeing with the rest of the
            # route gets a deep search from all three sources, anchored to
            # where the trusted stops sit.
            for row in rows:
                if row["name"] in trusted_names or row["rec"].get("verified"):
                    continue
                row["rec"] = gc.resolve(row["text"], override_key=row["key"], deep=True,
                                        bias=centre, max_km=12.0 if centre else None)
            gc.save()

            located, missing = [], []
            for row in rows:
                rec, t = row["rec"], row["printed"]
                if rec["lat"] is None or t is None:
                    missing.append({"name": row["name"], "printed": t, "riders": row["riders"],
                                    "why": "could not be located" if rec["lat"] is None else "no printed time"})
                else:
                    old = old_gc.resolve(row["text"], override_key=row["key"])
                    moved = (None if old["lat"] is None else
                             round(geo.haversine_km(old["lat"], old["lon"], rec["lat"], rec["lon"]), 2))
                    located.append({"name": row["name"], "lat": rec["lat"], "lon": rec["lon"],
                                    "printed": t, "riders": row["riders"],
                                    "approx": rec.get("confidence") == "low",
                                    "source": rec.get("provider", ""),
                                    "agree": rec.get("agree", []),
                                    "verified": bool(rec.get("verified")),
                                    "verified_by": (("GPS" if str(rec.get("note", "")).startswith("GPS")
                                                     else "hand") if rec.get("verified") else None),
                                    "key": row["key"], "text": row["text"],
                                    "label": rec.get("label", ""),
                                    "old": (None if old["lat"] is None or moved < MOVED_KM
                                            else [round(old["lat"], 6), round(old["lon"], 6)]),
                                    "old_missing": old["lat"] is None, "moved_km": moved})
            # Final check: after the anchored retry, anything still not part of a
            # group of agreeing stops is reported, not planned around.
            located, suspect, _ = split_trusted(located)
            for s_ in suspect:
                missing.append({"name": s_["name"], "printed": s_["printed"], "riders": s_["riders"],
                                "why": f"location doubtful, {s_['km']:.0f} km from the nearest trusted stop"})
            located, detours = peel_detours(located)
            for s_ in detours:
                missing.append({"name": s_["name"], "printed": s_["printed"], "riders": s_["riders"],
                                "why": "location doubtful - too far from its printed neighbours for the printed times"})
            item[leg] = {"plan": None, "_stops": located, "missing": missing,
                         "located": len(located),
                         "points": [{k_: s[k_] for k_ in (
                             "name", "lat", "lon", "riders", "approx", "source", "agree", "verified",
                             "verified_by", "key", "text", "label", "old", "old_missing", "moved_km")}
                             for s in located]}
        out.append(item)

    # Cost the Google order-search matrix for the whole build before any request.
    global USE_GOOGLE_MATRIX
    need = sum(len(js) for it in out for leg, morning in (("am", True), ("pm", False))
               if it[leg]["_stops"]
               for js in matrix_requests(
                   [school] + [(s_["lat"], s_["lon"]) for s_ in it[leg]["_stops"]],
                   node_departs(it[leg]["_stops"], school, morning), morning, cache).values())
    left = gmaps.budget().remaining("route_matrix_pro")
    if ROUTER != "google":
        USE_GOOGLE_MATRIX = False
    elif need == 0 or offline:
        USE_GOOGLE_MATRIX = True                      # cache only
        print(f"  order search: Google matrix from cache ({need} elements not cached)")
    elif need <= left and gmaps.available():
        USE_GOOGLE_MATRIX = True
        print(f"  order search: Google traffic-aware matrix, {need} new elements "
              f"({left} left under this month's cap)")
    else:
        USE_GOOGLE_MATRIX = False
        print(f"  order search: NOT using Google - needs {need} elements, {left} left under "
              f"this month's cap (GMAPS_CAP_ROUTE_MATRIX_PRO). Falling back to OSRM.")

    # Calibrate Google's times: 80% printed timetable, 20% whole-run taps.
    calib = {"applied": False}
    model = None
    if calibrate:
        runs = calibration_runs(out, school, cache, load_taps(inputs, routes_p, roster_p))
        runs += gps_calibration_runs(school, cache, cache_dir)
        cache.save()
        model = cal.fit(runs)
        val = cal.validate(runs, DWELL_MIN)
        model.save(cache_dir / "calibration.json")
        calib = {"applied": True, "model": model.to_dict(), "validation": val}
        for s_ in ("am", "pm"):
            v = val[s_]
            print(f"  calibration {s_}: factors {model.factors[s_]} (bands <1,1-2,2-4,4-8,>8 km), "
                  f"taps x{model.tap_adjust[s_]}; error vs printed per stop "
                  f"{v['printed_stop_error_min']['google']} -> {v['printed_stop_error_min']['calibrated']} min, "
                  f"per run {v['printed_run_error_min']['google']} -> {v['printed_run_error_min']['calibrated']} min, "
                  f"vs taps {v['tap_run_error_min']['google']} -> {v['tap_run_error_min']['calibrated']} min"
                  f"  [{'within target' if v['passes'] else 'NOT yet within target'}]")
            gv, gr = v["gps_stop_error_min"], v["gps_run_error_min"]
            print(f"    verified pins: factor {model.verified_factor[s_]} from {model.gps_legs[s_]} GPS legs"
                  + ("" if model.verified_factor[s_] is not None else
                     f" (needs {cal.MIN_GPS_LEGS}; band factors used meanwhile)"))
            if gv["stops"]:
                print(f"    vs GPS ({gr['runs']} run(s), {gv['stops']} stops): per stop / whole run, "
                      f"raw Google {gv['google']} / {gr['google']} min, old model without GPS "
                      f"{gv['calibrated_without_gps']} / {gr['calibrated_without_gps']}, with GPS "
                      f"(in-sample) {gv['calibrated_with_gps']} / {gr['calibrated_with_gps']}; "
                      f"measured dwell median {v['gps_dwell_s']['median']} s")
    for item in out:
        for leg, morning in (("am", True), ("pm", False)):
            sc_ = (lambda km, v=False, s_=leg: model.factor(s_, km, v)) if model else None
            item[leg]["plan"] = plan(item[leg].pop("_stops"), school, morning, cache, sc_)
        cache.save()
        print(f"  {item['route']:9s} AM {item['am']['located']:2d} located "
              f"{len(item['am']['missing']):2d} missing"
              + (f"  saves {item['am']['plan']['saved_min']:5.1f} min" if item['am']['plan'] else "  (no plan)"))

    plans = [r[l]["plan"] for r in out for l in ("am", "pm") if r[l]["plan"]]
    road = sorted({pl[v]["road_source"] for pl in plans for v in ("current", "optimised")})
    pts = [p_ for r in out for l in ("am", "pm") for p_ in r[l]["points"]]
    geo_src: dict[str, int] = {}
    for p_ in pts:
        geo_src[p_["source"]] = geo_src.get(p_["source"], 0) + 1
    order_src = sorted({pl.get("order_source", "osrm") for pl in plans})
    merges = {leg: merge_suggestions(out, leg) for leg in ("am", "pm")}
    data = {"school": {"lat": school[0], "lon": school[1]}, "dwell_min": DWELL_MIN,
            "order_source": order_src,
            "road_source": road, "geo_sources": geo_src,
            "calibration": calib, "merges": merges, "merge_radius_m": MERGE_RADIUS_M,
            "corroborated": sum(1 for p_ in pts if len(p_["agree"]) >= 2),
            "points_total": len(pts),
            "routes": sorted(out, key=lambda r: r["num"])}
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "routes_data.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return render(data, outputs / "routes.html")


def render(data: dict, html: Path) -> Path:
    """Write the planner page for `data` (the routes_data.json contents)."""
    tpl = (Path(__file__).resolve().parent / "routes_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_text(tpl.replace("__GMAP_TAGS__", gmaps.browser_map_tags("dark"))
                    .replace("__DATA__", payload), encoding="utf-8")
    render_tracker(data, html.parent)
    return html


TRACKER_FILES = ["track-sw.js", "track.webmanifest", "track-icon-192.png", "track-icon-512.png"]


def render_tracker(data: dict, folder: Path) -> Path:
    """
    The phone page bus staff use to log a run (track.html), beside the planner.
    It carries route names only: no stops, pins or children, so it can be public.
    """
    here = Path(__file__).resolve().parent
    routes = [r["route"] for r in data["routes"]]
    tpl = (here / "track_template.html").read_text(encoding="utf-8")
    page = folder / "track.html"
    page.write_text(tpl.replace("__ROUTES__", json.dumps(routes)), encoding="utf-8")
    for f in TRACKER_FILES:
        (folder / f).write_bytes((here / "track_assets" / f).read_bytes())
    return page


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=str(root / "inputs"))
    ap.add_argument("--outputs", default=str(root / "outputs"))
    ap.add_argument("--cache", default=str(root / "cache"))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--raw-google", action="store_true",
                    help="show Google's raw times, without the calibration to the timetable")
    ap.add_argument("--render-only", action="store_true",
                    help="redraw routes.html from outputs/routes_data.json; no data work, no network")
    a = ap.parse_args()
    if a.render_only:
        data = json.loads((Path(a.outputs) / "routes_data.json").read_text(encoding="utf-8"))
        print(f"wrote {render(data, Path(a.outputs) / 'routes.html')}")
        sys.exit(0)
    html = build(Path(a.inputs), Path(a.outputs), Path(a.cache), offline=a.offline,
                 calibrate=not a.raw_google)
    print(f"\nwrote {html}")
    print(gmaps.usage_report())
