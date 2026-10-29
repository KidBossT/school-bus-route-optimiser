"""
Scores any plan - today's routes or an optimised one - with the same rules.

The comparison between the current plan and a proposal is only honest if both
are costed by exactly this code, on exactly the same matrix. The solver calls
`route_cost` thousands of times; reports call `route_detail` once per route.
"""

from __future__ import annotations

import numpy as np

from .config import OptimiserConfig
from .model import Problem, Solution


class Evaluator:
    def __init__(self, problem: Problem, cfg: OptimiserConfig):
        self.p, self.cfg, self.w = problem, cfg, cfg.weights
        self.T, self.K = problem.outward()
        self.S = problem.service_min
        self.dem = np.array([0] + [nd.demand for nd in problem.nodes], dtype=float)
        dl = [nd.deadline_min for nd in problem.nodes]
        self.deadline = np.array([np.inf] + [np.inf if d is None else d for d in dl])
        self.cap = [v.capacity - v.reserved_load for v in problem.vehicles]
        self.vid = [v.id for v in problem.vehicles]
        self.existing = [v.existing for v in problem.vehicles]
        self.closed = [v.closed for v in problem.vehicles]
        self.retired = [v.retired for v in problem.vehicles]
        # on while searching; off once a plan is chosen, so today's plan (which still uses
        # those buses) is reported on the same terms as before
        self.enforce_retired = True
        self.home = [None] + [nd.current_vehicle for nd in problem.nodes]
        self.lock = [None] + [nd.locked_vehicle for nd in problem.nodes]

    # ------------------------------------------------------------------
    def max_duration(self, seq: list[int], v: int | None = None) -> float:
        """Longest this run may take, from the tightest of the time rules; a bus whose run
        is longer today may keep today's length (never_worse_than_today)."""
        c = self.cfg
        if self.p.direction == "am":
            dl = min((self.deadline[j] for j in seq), default=np.inf)
            window = dl - c.earliest_pickup_min if np.isfinite(dl) else np.inf
        else:
            window = c.latest_drop_min - c.dismissal_min - c.school_dwell_min
        limit = min(c.max_ride_min, window)
        if v is not None and self.p.vehicles[v].max_run_min is not None:
            limit = max(limit, self.p.vehicles[v].max_run_min)
        return limit

    def route_cost(self, seq: list[int], v: int) -> float:
        if not seq:
            return 0.0
        T, K, S, dem, w = self.T, self.K, self.S, self.dem, self.w
        t = km = ride = load = 0.0
        moved = 0.0
        prev = 0
        vid = self.vid[v]
        for j in seq:
            t += T[prev, j] + S[j]
            km += K[prev, j]
            ride += dem[j] * t
            load += dem[j]
            if self.home[j] is not None and self.home[j] != vid:
                moved += dem[j]
            if self.lock[j] is not None and self.lock[j] != vid:
                moved += 1e6          # locked stops never leave their bus
            elif self.closed[v] and self.home[j] != vid:
                moved += 1e6          # ... and a closed bus takes nobody new
            if self.enforce_retired and self.retired[v]:
                moved += 1e6          # a retired bus carries nobody
            prev = j
        over_cap = max(0.0, load - self.cap[v])
        over_time = max(0.0, t - self.max_duration(seq, v))
        return (w.bus_fixed + w.drive_min * t + w.km * km + w.student_ride_min * ride
                + w.reassigned_student * moved + w.violation * (over_cap + over_time))

    def total(self, sol: Solution) -> float:
        return sum(self.route_cost(r, v) for v, r in enumerate(sol.routes))

    # ------------------------------------------------------------------
    def route_detail(self, seq: list[int], v: int) -> dict:
        """Everything a person needs to run this bus, in driving order."""
        p, c = self.p, self.cfg
        T, K, S = self.T, self.K, self.S
        prefix, t, km, prev, est = [], 0.0, 0.0, 0, 0
        for j in seq:
            t += T[prev, j] + S[j]
            km += K[prev, j]
            est += bool(p.estimated[prev, j] if p.direction == "pm" else p.estimated[j, prev])
            prefix.append(t)
            prev = j
        duration = t
        if p.direction == "am":
            dl = min((self.deadline[j] for j in seq), default=np.inf)
            arrive = dl if np.isfinite(dl) else None
            etas = [None if arrive is None else arrive - x for x in prefix]
            start, end = (etas[-1] if etas else None), arrive
        else:
            depart = c.dismissal_min + c.school_dwell_min
            etas = [depart + x - S[j] for x, j in zip(prefix, seq)]
            start, end = depart, (etas[-1] if etas else None)
        load = int(sum(self.dem[j] for j in seq))
        order = list(zip(seq, etas, prefix))
        if p.direction == "am":
            order = order[::-1]
        stops = []
        for j, eta, ride in order:
            nd = p.nodes[j - 1]
            stops.append({
                "node": nd.id, "name": nd.name, "lat": nd.lat, "lon": nd.lon,
                "children": nd.demand, "eta_min": None if eta is None else round(eta, 1),
                "printed_min": nd.printed_min, "ride_min": round(ride, 1),
                "verified": nd.verified, "was_on": nd.current_vehicle,
            })
        limit = self.max_duration(seq, v) if seq else None
        return {
            "vehicle": self.vid[v], "stops": stops, "children": load,
            "capacity": self.cap[v] + p.vehicles[v].reserved_load,
            "reserved_seats": p.vehicles[v].reserved_load,
            "duration_min": round(duration, 1), "km": round(km, 1),
            "limit_min": None if limit is None or not np.isfinite(limit) else round(limit, 1),
            "start_min": None if start is None else round(start, 1),
            "end_min": None if end is None else round(end, 1),
            "max_ride_min": round(max(prefix), 1) if prefix else 0.0,
            "over_capacity": max(0, load - self.cap[v]),
            "over_time_min": round(max(0.0, duration - (limit or np.inf)), 1),
            "estimated_legs": est, "legs": len(seq),
            "cost": round(self.route_cost(seq, v), 1),
        }

    def summary(self, sol: Solution) -> dict:
        """Fleet-level metrics - the numbers a decision is made on."""
        det = [self.route_detail(r, v) for v, r in enumerate(sol.routes) if r]
        # A bus with no routed stops still runs if children at held stops ride it.
        held_only = [self.p.vehicles[v] for v, r in enumerate(sol.routes)
                     if not r and self.p.vehicles[v].reserved_load > 0]
        rides = [(s["ride_min"], s["children"]) for d in det for s in d["stops"]]
        kids = sum(k for _, k in rides) or 1
        moved = sum(nd.demand for v, r in enumerate(sol.routes) for j in r
                    for nd in [self.p.nodes[j - 1]]
                    if nd.current_vehicle is not None and nd.current_vehicle != self.vid[v])
        return {
            "buses_used": len(det) + len(held_only),
            "buses_held_only": len(held_only),
            "children": int(sum(k for _, k in rides)),
            "bus_minutes": round(sum(d["duration_min"] for d in det), 1),
            "bus_km": round(sum(d["km"] for d in det), 1),
            "student_ride_min_total": round(sum(r * k for r, k in rides), 1),
            "student_ride_min_mean": round(sum(r * k for r, k in rides) / kids, 1),
            "student_ride_min_max": round(max((r for r, _ in rides), default=0.0), 1),
            "longest_run_min": round(max((d["duration_min"] for d in det), default=0.0), 1),
            "seat_utilisation": round((sum(d["children"] + d["reserved_seats"] for d in det)
                                       + sum(v.reserved_load for v in held_only))
                                      / max(1, sum(d["capacity"] for d in det)
                                            + sum(v.capacity for v in held_only)), 3),
            "routes_over_capacity": sum(d["over_capacity"] > 0 for d in det),
            "routes_over_time": sum(d["over_time_min"] > 0 for d in det),
            "children_reassigned": int(moved),
            "estimated_leg_share": round(sum(d["estimated_legs"] for d in det)
                                         / max(1, sum(d["legs"] for d in det)), 3),
            "objective": round(self.total(sol), 1),
        }
