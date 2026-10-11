"""
Stop consolidation: nearby stops that could become one.

Every stop costs a bus a minute or more of dwell plus the detour to reach it.
Where stops sit within a short walk of each other - or are literally the same
place served by two buses - folding them into one shortens every run through
them. This module only proposes; nothing is merged unless
`apply_stop_merges` is switched on, and then only between stops whose
coordinates a human has verified (unless the run allows unverified points).

The merged stop is always one of the existing stops - the one that minimises
total walking for the children affected - never a computed midpoint, which
could land in the middle of a road or a private compound.
"""

from __future__ import annotations

import geo

from .model import Node


def _walk_m(a: Node, b: Node) -> float:
    return geo.haversine_km(a.lat, a.lon, b.lat, b.lon) * 1000.0


def propose(nodes: list[Node], radius_m: float, max_group_demand: int) -> list[dict]:
    left = sorted(nodes, key=lambda x: -x.demand)
    out = []
    while left:
        seed = left.pop(0)
        group = [seed] + [x for x in left if _walk_m(seed, x) <= radius_m]
        if len(group) < 2:
            continue
        # every member must be within the radius of the chosen hub, not just the seed
        hub = min(group, key=lambda h: sum(_walk_m(h, x) * x.demand for x in group))
        group = [x for x in group if _walk_m(hub, x) <= radius_m]
        if len(group) < 2 or sum(x.demand for x in group) > max_group_demand:
            continue
        for x in group:
            if x in left:
                left.remove(x)
        walks = [(x, _walk_m(hub, x)) for x in group if x is not hub]
        out.append({
            "hub": hub.id, "hub_name": hub.name,
            "members": [{"node": x.id, "name": x.name, "walk_m": round(w),
                         "children": x.demand, "bus_today": x.current_vehicle}
                        for x, w in walks],
            "children_walking_further": sum(x.demand for x, _ in walks),
            "max_walk_m": round(max(w for _, w in walks)),
            "same_place": all(w < 30 for _, w in walks),
            "stops_removed": len(walks),
        })
    return out


def apply(nodes: list[Node], proposals: list[dict]) -> list[Node]:
    """Fold each proposal's members into its hub. Returns the new node list."""
    by_id = {nd.id: nd for nd in nodes}
    gone = set()
    for pr in proposals:
        hub = by_id[pr["hub"]]
        for m in pr["members"]:
            x = by_id[m["node"]]
            hub.demand += x.demand
            hub.students += x.students
            if x.deadline_min is not None:
                hub.deadline_min = min(filter(None, [hub.deadline_min, x.deadline_min]))
            gone.add(x.id)
    return [nd for nd in nodes if nd.id not in gone]
