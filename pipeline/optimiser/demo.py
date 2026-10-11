"""
A synthetic network shaped like the real one, so the optimiser can be run,
tested and tuned before real coordinates exist.

Stops are scattered in neighbourhood clusters around a school in Noida, and
"today's" routes are deliberately imperfect: each bus is assigned a cluster
but also picks up a few stops from a neighbouring one, the way routes drift
as families move. Nothing here touches the real workbooks or caches.
"""

from __future__ import annotations

import math
import random

import numpy as np

from .build_problem import Built
from .config import OptimiserConfig
from .matrix import haversine_matrix
from .model import Node, Problem, Solution, Vehicle

SCHOOL = (28.5700, 77.3900)


def make(direction: str, cfg: OptimiserConfig, n_buses: int = 20, stops_per_bus: int = 10,
         seed: int = 1) -> Built:
    rng = random.Random(seed)
    am = direction == "am"
    centres = []
    for b in range(n_buses):
        ang = 2 * math.pi * b / n_buses + rng.uniform(-0.2, 0.2)
        r_km = rng.uniform(3, 10)
        centres.append((SCHOOL[0] + r_km / 111 * math.cos(ang),
                        SCHOOL[1] + r_km / 98 * math.sin(ang)))
    nodes, owner = [], []
    for b, (clat, clon) in enumerate(centres):
        for s in range(stops_per_bus):
            lat = clat + rng.gauss(0, 0.9) / 111
            lon = clon + rng.gauss(0, 0.9) / 98
            # a quarter of stops are served by the neighbouring bus today
            bus = (b + 1) % n_buses if rng.random() < 0.25 else b
            kids = rng.choice([1, 1, 2, 2, 3, 4])
            i = len(nodes)
            nodes.append(Node(id=f"S{i:03d}@R{bus + 1:02d}", name=f"Demo stop {i}", lat=lat, lon=lon,
                              demand=kids, deadline_min=455.0 if am else None,
                              current_vehicle=f"R{bus + 1:02d}",
                              students=[f"DEMO{i:03d}-{k}" for k in range(kids)],
                              printed_min=None, verified=True))
            owner.append(bus)
    vehicles = [Vehicle(f"R{b + 1:02d}", cfg.default_capacity) for b in range(n_buses)]
    vehicles += [Vehicle(f"SPARE{i + 1}", cfg.default_capacity, existing=False)
                 for i in range(cfg.extra_vehicles)]

    pts = [SCHOOL] + [(nd.lat, nd.lon) for nd in nodes]
    H = haversine_matrix(pts)
    K = H * cfg.haversine_detour
    T = K / cfg.haversine_speed_kmh * 60.0
    # mild asymmetry, as on real one-way systems
    T = T * (1 + 0.05 * np.random.default_rng(seed).standard_normal(T.shape).clip(-2, 2))
    np.fill_diagonal(T, 0.0)
    est = np.ones_like(T, dtype=bool)
    service = np.array([0.0] + [cfg.dwell_base_min + cfg.dwell_per_student_min * nd.demand
                                for nd in nodes])
    p = Problem(direction, SCHOOL, nodes, vehicles, T, K, est, service,
                ["SYNTHETIC DEMO NETWORK - not the school's stops"])

    # today's order: each bus sweeps its stops by distance from school (a
    # plausible, imperfect habit), far-to-near in the morning
    cur = Solution([[] for _ in vehicles])
    for b in range(n_buses):
        mine = [i + 1 for i, o in enumerate(owner) if o == b]
        cur.routes[b] = sorted(mine, key=lambda j: T[0, j] if not am else T[j, 0])
    for nd in nodes:
        nd.printed_min = None
    return Built(p, cur, [], [], [])
