"""
Turns the school's workbooks + stop coordinates into optimiser Problems.

The optimiser never geocodes. It reads coordinates from the same two files the
dashboard uses - geocode_overrides.json (verified: the GPS campaign's confirmed
stops, or imported from Google Maps via `run_optimiser.py --import-coords`) and
geocode_cache.json. Only verified points may move between buses. A stop without
one is, by default (lock_unverified), still routed - at the place the GPS
campaign measured the bus standing, else the planner's pin - but locked to the
bus that serves it today, so every run's time is counted. With nothing to route
on it is "held": it stays on today's bus, its children keep their seats reserved
on that bus, and it is listed in readiness.csv. Staff ride too (include_staff).

A node is one (stop, bus-today) pair, not one stop. Two buses that both call at
the same stop give two nodes at the same coordinate, zero minutes apart; the
optimiser is then free to put both on one bus, and the consolidation report
calls them out as the same place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import calibration
import geo
import gmaps
import transport_spine as ts

from . import consolidate
from .config import OptimiserConfig
from .matrix import MatrixBuilder
from .model import Node, Problem, Solution, Vehicle

_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
SCHOOL_QUERY = "Prometheus School Noida"
# Not "__SCHOOL__": geo.Geocoder ignores override keys starting with "_" (that
# is how it skips the README block), so such a key could never be loaded.
SCHOOL_KEY = SCHOOL_QUERY


@dataclass
class Built:
    problem: Problem
    current: Solution
    held: list[dict] = field(default_factory=list)
    readiness: list[dict] = field(default_factory=list)
    merges: list[dict] = field(default_factory=list)


def location_key(stop_name: str, direction: str) -> tuple[str, str]:
    """(override key, text) for the pickup (am) or drop (pm) half of a stop cell."""
    p_text, d_text = geo.split_pickup_drop(stop_name)
    if p_text == d_text:
        return stop_name, p_text
    return f"{stop_name}||{'P' if direction == 'am' else 'D'}", (p_text if direction == "am" else d_text)


def load_sources(inputs: Path, include_staff: bool = False):
    routes = ts.parse_route_list(sorted(inputs.glob("Route*List*Area*.xlsx"))[0])
    roster = ts.parse_roster(sorted(inputs.glob("Route*Wise*Student*.xlsx"))[0],
                             id_pattern=ts.STAFF_OR_STUDENT_ID if include_staff else None)
    return routes, ts.build_student_dim(roster)


OFF_TRACK_M = 300          # a pin further than this from the bus's GPS track is not where the bus stops


def gps_places(cache_dir: Path, direction: str) -> tuple[dict, dict]:
    """
    From gps_campaign.py: (route number, stop key) -> where the bus was measured standing there
    (with how sure the place and name are), and route number -> one run's track on the printed clock.
    """
    p = Path(cache_dir) / "gps_campaign.json"
    if not p.exists():
        return {}, {}
    places, tracks = {}, {}
    for r in json.loads(p.read_text(encoding="utf-8"))["routes"]:
        if r["session"] != direction:
            continue
        if r.get("track"):
            tracks[r["route"]] = r["track"]["points"]
        for s in r["stops"]:
            if s["lat"] is None:
                continue
            for m in s["members"]:
                places[(r["route"], m["key"])] = {"lat": s["lat"], "lon": s["lon"],
                                                  "loc_conf": s["loc_conf"], "name_conf": s["name_conf"]}
    return places, tracks


def on_track(track: list, lat, lon, printed) -> tuple[tuple[float, float] | None, float | None]:
    """
    Where to time a stop the GPS did not place: its pin if the bus passes within OFF_TRACK_M of it,
    else where the bus is at the stop's printed time (else the track point nearest the pin).
    Returns (point or None to keep the pin, metres the pin is off the track).
    """
    if not track:
        return None, None
    off = None
    if lat is not None:
        off = min(geo.haversine_km(lat, lon, t[0], t[1]) for t in track) * 1000
        if off <= OFF_TRACK_M:
            return None, off
    if printed is not None:
        t = min(track, key=lambda t: abs(t[2] - printed))
    elif lat is not None:
        t = min(track, key=lambda t: geo.haversine_km(lat, lon, t[0], t[1]))
    else:
        return None, None
    return (t[0], t[1]), off


PLANNER_CACHE = "route_plan_cache.json"     # build_routes.py's traffic-aware pair times (google-matrix|...)


def static_traffic_ratio(cache_dir: Path, static_cache: dict, am: bool) -> tuple[float | None, int]:
    """
    Google's traffic-aware minutes / its no-traffic minutes, on the pairs both caches hold: the
    route planner prices its legs with traffic, the optimiser fetches the cheaper no-traffic times.
    Departure bands of this direction only. (None, n) when fewer than 30 pairs overlap.
    """
    static = {}
    for k, v in static_cache.items():
        if k.startswith("google_static|") and isinstance(v, list) and len(v) >= 3 and gmaps.is_fresh(v[-1]):
            _, h, a, b = k.split("|")
            if (int(h[1:]) < 12) == am:
                static[(a, b)] = v[0] / 60.0
    path = Path(cache_dir) / PLANNER_CACHE
    planner = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ratios = []
    for k, v in planner.items():
        if not k.startswith("google-matrix|") or not isinstance(v, dict) or v.get("min") is None:
            continue
        _, dep, a, b = k.split("|")
        if (int(dep) < 720) != am or not gmaps.is_fresh(v.get("fetched_at")):
            continue
        s_ = static.get((a, b))
        if s_ and s_ >= 1.0:
            ratios.append(v["min"] / s_)
    if len(ratios) < 30:
        return None, len(ratios)
    return float(np.median(ratios)), len(ratios)


def make_geocoder(routes: pd.DataFrame, cache_dir: Path) -> geo.Geocoder:
    corpus = [t for n in routes.stop_name.unique() for t in geo.split_pickup_drop(n)]
    return geo.Geocoder(cache_dir, offline=True, corpus=corpus)


def usable(rec: dict, cfg: OptimiserConfig) -> tuple[bool, str]:
    if rec.get("lat") is None:
        return False, "no coordinate"
    if rec.get("verified"):
        return True, "verified"
    if cfg.allow_corroborated and len(rec.get("agree", [])) >= 2 and geo.in_bbox(rec["lat"], rec["lon"]):
        return True, "confirmed by 2+ independent sources (not hand-verified)"
    if cfg.require_verified:
        return False, "coordinate not verified"
    if not geo.in_bbox(rec["lat"], rec["lon"]):
        return False, "outside the NCR bounding box"
    if _CONF_RANK.get(rec.get("confidence", "none"), 0) < _CONF_RANK[cfg.min_confidence]:
        return False, f"geocoder confidence {rec.get('confidence')}"
    return True, "unverified geocode (draft only)"


def build(direction: str, routes: pd.DataFrame, students: pd.DataFrame,
          gc: geo.Geocoder, cfg: OptimiserConfig, cache_dir: Path, offline: bool,
          log=print) -> Built:
    am = direction == "am"
    rcol = "roster_am_route_key" if am else "roster_pm_route_key"
    scol = "am_stop_name" if am else "pm_stop_name"
    tcol = "promised_pickup_min" if am else "promised_drop_min"

    sc = gc.resolve(SCHOOL_QUERY, override_key=SCHOOL_KEY)
    ok, why = usable(sc, cfg)
    if not ok:
        raise RuntimeError(f"school location is not usable ({why}). Add a '{SCHOOL_KEY}' "
                           f"row to the coordinates file and import it.")
    school = (sc["lat"], sc["lon"])

    seq = {(r.route_key, r.stop_name): r.stop_seq for r in routes.itertuples()}
    places, tracks = gps_places(cache_dir, direction) if cfg.gps_campaign else ({}, {})
    retired = set(cfg.retired_vehicles)
    closed = set(cfg.closed_vehicles) - retired
    nodes, held, readiness = [], [], []
    for (rk, stop), g in students.dropna(subset=[rcol]).groupby([rcol, scol], sort=True):
        key, text = location_key(stop, direction)
        rec = gc.resolve(text, override_key=key)
        good, why = usable(rec, cfg)
        lat, lon, locked = rec.get("lat"), rec.get("lon"), None
        verified = good and bool(rec.get("verified"))
        measured = verified
        num = int(rk[1:]) if rk[1:].isdigit() else None
        gp = places.get((num, key))
        printed = g[tcol].median()
        printed = None if pd.isna(printed) else float(printed)
        if not good and gp and cfg.draft_gps and gp["loc_conf"] in ("high", "medium") and gp["name_conf"] == "medium" \
                and rk not in closed:
            lat, lon, good, measured = gp["lat"], gp["lon"], True, True
            why = "GPS place sure, name not yet confirmed (DRAFT)"
        elif not good and cfg.lock_unverified:
            # routed so its time counts, kept on today's bus: timed where the bus really goes
            if gp:
                lat, lon, measured, where = gp["lat"], gp["lon"], True, "the GPS place"
            else:
                pt, off = on_track(tracks.get(num), lat if lat is not None and geo.in_bbox(lat, lon) else None,
                                   lon, printed)
                if pt:
                    lat, lon, measured = pt[0], pt[1], True
                    where = ("where the bus is at its printed time" if printed is not None else "the nearest point of the bus's path") \
                        + (f" (pin {off:.0f} m off the GPS track)" if off is not None else " (no pin)")
                else:
                    where = "the planner pin"
            if lat is not None and geo.in_bbox(lat, lon):
                good = True
                if cfg.draft_all and rk not in closed:
                    why = f"not verified, moved anyway (DRAFT ceiling); timed at {where}"
                else:
                    locked = rk
                    why = f"kept on today's bus ({why}); timed at {where}"
        if good and (rk in closed or cfg.reorder_only):
            locked = rk                        # a closed bus keeps exactly its own stops
        if good and rk in retired:
            locked = None                      # its bus is off the road: the stop has to move
        row = {"direction": direction, "bus_today": rk, "stop": stop, "key": key,
               "children": len(g), "lat": lat, "lon": lon,
               "source": "GPS" if (measured and not verified) else rec.get("source"),
               "confidence": rec.get("confidence"),
               "status": ("locked" if locked else "routed") if good else "held", "reason": why}
        readiness.append(row)
        if not good:
            held.append(row)
            continue
        nodes.append(Node(
            id=f"{key}@{rk}", name=stop, lat=lat, lon=lon, demand=len(g),
            deadline_min=float(g.reporting_time_min.min()) if am and g.reporting_time_min.notna().any() else None,
            current_vehicle=rk, locked_vehicle=locked, students=list(g.admission_no),
            printed_min=printed, verified=verified, measured=measured,
        ))

    # vehicles: every bus that carries children in this direction today, plus spares
    fleet = sorted(students[rcol].dropna().unique())
    reserved = pd.Series([h["children"] for h in held], index=[h["bus_today"] for h in held],
                         dtype=float).groupby(level=0).sum() if held else pd.Series(dtype=float)
    vehicles = [Vehicle(rk, int(cfg.capacity_by_route.get(rk, cfg.default_capacity)),
                        reserved_load=int(reserved.get(rk, 0)), closed=rk in closed or cfg.reorder_only,
                        retired=rk in retired)
                for rk in fleet]
    vehicles += [Vehicle(f"SPARE{i + 1}", cfg.default_capacity, existing=False)
                 for i in range(cfg.extra_vehicles)]

    nodes = _split_oversized(nodes, max(v.capacity for v in vehicles))

    merges = consolidate.propose(nodes, cfg.merge_radius_m, min(v.capacity for v in vehicles))
    if cfg.apply_stop_merges and merges:
        nodes = consolidate.apply(nodes, merges)

    # matrix
    hours = [nd.printed_min for nd in nodes if nd.printed_min is not None]
    hour = int(np.median(hours) // 60) if hours else (7 if am else 15)
    mb = MatrixBuilder(cache_dir, cfg.matrix_provider, offline=offline,
                       detour=cfg.haversine_detour, speed_kmh=cfg.haversine_speed_kmh,
                       neighbours=cfg.neighbours, log=log)
    pts = [school] + [(nd.lat, nd.lon) for nd in nodes]
    model = calibration.Model.load(cache_dir / "calibration.json") if cfg.use_calibration else None
    T, K, est = mb.build(pts, hour, 1.0 if model else
                         (cfg.time_multiplier_am if am else cfg.time_multiplier_pm))
    traffic_note = None
    if cfg.matrix_provider == "google_static":
        # The calibration below was fitted on traffic-aware Google times; no-traffic times need the
        # traffic share put back first.
        if cfg.traffic_scale == "auto":
            r, n_pairs = static_traffic_ratio(cache_dir, mb.cache.data, am)
        else:
            r, n_pairs = float(cfg.traffic_scale), None
        if r:
            T = T * r
            traffic_note = (f"no-traffic times x{r:.2f} for traffic"
                            + (f" (traffic/no-traffic measured on {n_pairs} pairs the route planner priced)"
                               if n_pairs else " (set in config)"))
        else:
            traffic_note = (f"no-traffic times NOT scaled for traffic: only {n_pairs} pairs to measure "
                            f"it on - set traffic_scale in optimiser_config.json")
    if model is not None:
        # Same calibration as the route planner: leg-length factors (printed timetable 80%,
        # taps 20%) between unverified pins, the GPS-measured factor between measured points.
        s_ = "am" if am else "pm"
        ok = [True] + [nd.verified or nd.measured for nd in nodes]
        F = np.array([[model.factor(s_, geo.haversine_km(*a, *b), ok[i] and ok[j])
                       for j, b in enumerate(pts)] for i, a in enumerate(pts)])
        T = T * F
    service = np.array([0.0] + [cfg.dwell_base_min + cfg.dwell_per_student_min * nd.demand
                                for nd in nodes])
    notes = [f"travel times: {cfg.matrix_provider}, departure hour {hour}:00, "
             + ("calibrated to the printed timetable by leg length (cache/calibration.json)"
                if model else f"x{cfg.time_multiplier_am if am else cfg.time_multiplier_pm} multiplier")]
    if traffic_note:
        notes.append(traffic_note)
    locked_rows = [r for r in readiness if r["status"] == "locked" and "kept on today's bus" in r["reason"]]
    if locked_rows:
        gps_n = sum(1 for r in locked_rows if "the GPS place" in r["reason"])
        path_n = sum(1 for r in locked_rows if "printed time" in r["reason"] or "nearest point" in r["reason"])
        notes.append(f"{len(locked_rows)} stops ({sum(r['children'] for r in locked_rows)} riders) have no "
                     f"verified point: routed so their time counts, but kept on today's bus ({gps_n} timed at "
                     f"the GPS place, {path_n} on the bus's GPS path because their pin is off it, "
                     f"{len(locked_rows) - gps_n - path_n} at the planner pin)")
    if closed:
        notes.append(f"closed (keep exactly their own stops, nobody moves on or off): {', '.join(sorted(closed))}")
    if retired:
        notes.append(f"taken off the road in this proposal, every stop they serve today moves: "
                     f"{', '.join(sorted(retired))}")
    if held:
        notes.append(f"{len(held)} stops ({sum(h['children'] for h in held)} children) held on "
                     f"today's bus - no usable coordinate; their seats stay reserved but the "
                     f"time their stops add to a run is NOT counted")
    problem = Problem(direction, school, nodes, vehicles, T, K, est, service, notes)

    # today's plan, in the order the bus really drives it: where a stop lies on the bus's GPS track
    # (that run's clock, lined up with the printed one), else its printed time
    def driven_at(nd):
        num = int(nd.current_vehicle[1:]) if nd.current_vehicle[1:].isdigit() else None
        tr = tracks.get(num)
        if tr:
            d_, t_ = min((geo.haversine_km(nd.lat, nd.lon, t[0], t[1]), t[2]) for t in tr)
            if d_ * 1000 <= OFF_TRACK_M:
                return t_
        return nd.printed_min

    vix = {v.id: i for i, v in enumerate(vehicles)}
    current = Solution([[] for _ in vehicles])
    when = [driven_at(nd) for nd in nodes]
    order = sorted(range(len(nodes)), key=lambda i: (
        when[i] is None, when[i] or 0,
        seq.get((nodes[i].current_vehicle, nodes[i].name), 999)))
    for i in order:
        current.routes[vix[nodes[i].current_vehicle]].append(i + 1)
    if am:          # driving order ends at school; outward order is the reverse
        current.routes = [r[::-1] for r in current.routes]
    return Built(problem, current, held, readiness, merges)


def follow(built: Built, bus_of: dict[str, str]) -> int:
    """
    Each child keeps one bus both ways: lock every stop of this session to the bus most of its
    riders take in the other session (already solved). Returns how many stops were fixed.
    """
    usable_buses = {v.id for v in built.problem.vehicles if not v.retired}
    n = 0
    for nd in built.problem.nodes:
        votes = pd.Series([bus_of[s] for s in nd.students if s in bus_of], dtype=object).value_counts()
        if len(votes) and votes.index[0] in usable_buses:
            nd.locked_vehicle = votes.index[0]
            n += 1
    built.problem.notes.append(f"each child keeps one bus both ways: {n} of {built.problem.n} stops fixed to "
                               f"the bus their riders take in the other session")
    return n


def _split_oversized(nodes: list[Node], cap: int) -> list[Node]:
    """A stop with more children than any bus seats is served by several buses."""
    out = []
    for nd in nodes:
        if nd.demand <= cap:
            out.append(nd)
            continue
        kids = list(nd.students)
        for c, s in enumerate(range(0, len(kids), cap)):
            part = kids[s:s + cap]
            out.append(Node(**{**nd.__dict__, "id": f"{nd.id}#{c + 1}",
                               "demand": len(part), "students": part}))
    return out
