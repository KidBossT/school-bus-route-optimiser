"""
The optimisation problem as plain data, independent of where it came from.

One Problem is one direction (AM pickups ending at school, or PM drops
starting from school). Node 0 is always the school. Everything the solver and
the evaluator need is here; nothing here knows about Excel, geocoders or maps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Node:
    """One place a bus must stop, carrying `demand` children."""
    id: str                       # stable key: "<stop key>@<current route>"
    name: str                     # stop name as printed
    lat: float
    lon: float
    demand: int                   # children boarding (AM) or alighting (PM)
    deadline_min: float | None = None   # AM: school reporting time for these children
    current_vehicle: str | None = None  # bus that serves it today (for stability)
    locked_vehicle: str | None = None   # must stay on this bus
    students: list[str] = field(default_factory=list)   # admission numbers
    printed_min: float | None = None    # time printed for parents today
    verified: bool = False


@dataclass
class Vehicle:
    id: str
    capacity: int
    reserved_load: int = 0        # seats taken by children at stops we cannot route yet
    existing: bool = True         # False for a spare the solver may add


@dataclass
class Problem:
    direction: str                # "am" | "pm"
    school: tuple[float, float]
    nodes: list[Node]             # index i here is matrix index i + 1
    vehicles: list[Vehicle]
    time_min: np.ndarray          # (n+1, n+1) travel minutes, index 0 = school
    dist_km: np.ndarray           # (n+1, n+1)
    estimated: np.ndarray         # (n+1, n+1) bool, True where not a real road time
    service_min: np.ndarray       # (n+1,) dwell at each node, [0] = school
    notes: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.nodes)

    def outward(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Matrices oriented away from school. A PM run is school -> a -> b.
        An AM run a -> b -> school is the same path read backwards on the
        transposed matrix, so the solver only ever builds paths from node 0.
        """
        if self.direction == "am":
            return self.time_min.T.copy(), self.dist_km.T.copy()
        return self.time_min, self.dist_km


@dataclass
class Solution:
    """routes[v] is the outward sequence of matrix indices for vehicles[v]."""
    routes: list[list[int]]

    def copy(self) -> "Solution":
        return Solution([list(r) for r in self.routes])

    def stops_in_driving_order(self, direction: str, v: int) -> list[int]:
        return self.routes[v][::-1] if direction == "am" else list(self.routes[v])
