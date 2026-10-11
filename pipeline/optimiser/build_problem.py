"""
Turns the school's workbooks + stop coordinates into optimiser Problems.

The optimiser never geocodes. It reads coordinates from the same two files the
dashboard uses - geocode_overrides.json (human-verified, e.g. imported from
Google Maps via `run_optimiser.py --import-coords`) and geocode_cache.json -
and by default routes only verified points. A stop without a usable point is
"held": it stays on the bus that serves it today, its children keep their
seats reserved on that bus, and it is listed in readiness.csv.

A node is one (stop, bus-today) pair, not one stop. Two buses that both call at
the same stop give two nodes at the same coordinate, zero minutes apart; the
optimiser is then free to put both on one bus, and the consolidation report
calls them out as the same place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import calibration
import geo
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


def load_sources(inputs: Path):
    routes = ts.parse_route_list(sorted(inputs.glob("Route*List*Area*.xlsx"))[0])
    roster = ts.parse_roster(sorted(inputs.glob("Route*Wise*Student*.xlsx"))[0])
    return routes, ts.build_student_dim(roster)


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
    nodes, held, readiness = [], [], []
    for (rk, stop), g in students.dropna(subset=[rcol]).groupby([rcol, scol], sort=True):
        key, text = location_key(stop, direction)
        rec = gc.resolve(text, override_key=key)
        good, why = usable(rec, cfg)
        printed = g[tcol].median()
        row = {"direction": direction, "bus_today": rk, "stop": stop, "key": key,
               "children": len(g), "lat": rec.get("lat"), "lon": rec.get("lon"),
               "source": rec.get("source"), "confidence": rec.get("confidence"),
               "status": "routed" if good else "held", "reason": why}
        readiness.append(row)
        if not good:
            held.append(row)
            continue
        nodes.append(Node(
            id=f"{key}@{rk}", name=stop, lat=rec["lat"], lon=rec["lon"], demand=len(g),
            deadline_min=float(g.reporting_time_min.min()) if am and g.reporting_time_min.notna().any() else None,
            current_vehicle=rk, students=list(g.admission_no),
            printed_min=None if pd.isna(printed) else float(printed),
            verified=bool(rec.get("verified")),
        ))

    # vehicles: every bus that carries children in this direction today, plus spares
    fleet = sorted(students[rcol].dropna().unique())
    reserved = pd.Series([h["children"] for h in held], index=[h["bus_today"] for h in held],
                         dtype=float).groupby(level=0).sum() if held else pd.Series(dtype=float)
    vehicles = [Vehicle(rk, int(cfg.capacity_by_route.get(rk, cfg.default_capacity)),
                        reserved_load=int(reserved.get(rk, 0))) for rk in fleet]
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
    if model is not None:
        # Same calibration as the route planner: leg-length factors (printed timetable 80%,
        # taps 20%) between unverified pins, the GPS-measured factor between verified ones.
        s_ = "am" if am else "pm"
        ok = [True] + [nd.verified for nd in nodes]
        F = np.array([[model.factor(s_, geo.haversine_km(*a, *b), ok[i] and ok[j])
                       for j, b in enumerate(pts)] for i, a in enumerate(pts)])
        T = T * F
    service = np.array([0.0] + [cfg.dwell_base_min + cfg.dwell_per_student_min * nd.demand
                                for nd in nodes])
    notes = [f"travel times: {cfg.matrix_provider}, departure hour {hour}:00, "
             + ("calibrated to the printed timetable by leg length (cache/calibration.json)"
                if model else f"x{cfg.time_multiplier_am if am else cfg.time_multiplier_pm} multiplier")]
    if held:
        notes.append(f"{len(held)} stops ({sum(h['children'] for h in held)} children) held on "
                     f"today's bus - no usable coordinate; their seats stay reserved but the "
                     f"time their stops add to a run is NOT counted")
    problem = Problem(direction, school, nodes, vehicles, T, K, est, service, notes)

    # today's plan, in the order the schedule runs it
    vix = {v.id: i for i, v in enumerate(vehicles)}
    current = Solution([[] for _ in vehicles])
    order = sorted(range(len(nodes)), key=lambda i: (
        nodes[i].printed_min is None, nodes[i].printed_min or 0,
        seq.get((nodes[i].current_vehicle, nodes[i].name), 999)))
    for i in order:
        current.routes[vix[nodes[i].current_vehicle]].append(i + 1)
    if am:          # driving order ends at school; outward order is the reverse
        current.routes = [r[::-1] for r in current.routes]
    return Built(problem, current, held, readiness, merges)


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
