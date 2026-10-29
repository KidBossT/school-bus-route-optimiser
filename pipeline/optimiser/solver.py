"""
Network-wide route optimiser: which bus serves which stop, and in what order.

This is a capacitated, time-limited, open vehicle-routing problem, solved
without any external solver so it runs anywhere the pipeline runs:

  1. Construction - two starting points, and the better survives:
       * today's routes (warm start: the plan families already know), and
       * Clarke-Wright savings from scratch (no memory of today's plan).
  2. Local search to a local optimum with four moves:
       relocate   one stop to any position on any bus
       swap       two stops on different buses
       2-opt      reverse a stretch of one bus's run
       2-opt*     exchange the tails of two buses' runs
     Moves are only tried near each stop's k nearest neighbours ("granular"
     search) - a stop 20 km away is never a useful place to insert.
  3. Ruin-and-recreate until the time limit: pull out a cluster of stops,
     re-insert them greedily, local-search again, keep improvements.

Everything is costed by `Evaluator.route_cost`, so capacity, ride-time,
deadline, stability and fleet-size trade-offs all come from the weights in
config.py - the solver itself has no opinion about what "optimal" means.
"""

from __future__ import annotations

import random
import time

import numpy as np

from .evaluate import Evaluator
from .model import Problem, Solution


class Solver:
    def __init__(self, problem: Problem, ev: Evaluator, k: int = 15, seed: int = 7):
        self.p, self.ev = problem, ev
        self.rng = random.Random(seed)
        T = ev.T
        n = problem.n
        sym = np.minimum(T[1:, 1:], T[1:, 1:].T) if n else np.zeros((0, 0))
        self.near = [[]]
        for i in range(n):
            order = np.argsort(sym[i])
            self.near.append([int(j) + 1 for j in order if j != i][:k])

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def savings(self) -> Solution:
        """Clarke-Wright for open routes that all start from school."""
        ev, T = self.ev, self.ev.T
        n = self.p.n
        cap_max = max(ev.cap) if ev.cap else 0
        route_of = {j: [j] for j in range(1, n + 1)}
        head = {j: j for j in range(1, n + 1)}      # route id -> first node
        tail = {j: j for j in range(1, n + 1)}      # route id -> last node
        rid = {j: j for j in range(1, n + 1)}       # node -> route id
        pairs = []
        for i in range(1, n + 1):
            for j in self.near[i]:
                # extend a run ending at i with a run starting at j:
                # drop the leg school->j, add the leg i->j
                pairs.append((T[0, j] - T[i, j], i, j))
        pairs.sort(reverse=True)
        for s, i, j in pairs:
            a, b = rid[i], rid[j]
            if a == b or tail[a] != i or head[b] != j:
                continue
            merged = route_of[a] + route_of[b]
            load = sum(ev.dem[x] for x in merged)
            if load > cap_max:
                continue
            probe = merged
            t, prev = 0.0, 0
            for x in probe:
                t += T[prev, x] + ev.S[x]
                prev = x
            if t > ev.max_duration(probe):
                continue
            route_of[a] = merged
            tail[a] = tail[b]
            for x in route_of[b]:
                rid[x] = a
            del route_of[b], head[b], tail[b]
        return self._assign_to_vehicles([r for r in route_of.values()])

    def _assign_to_vehicles(self, runs: list[list[int]]) -> Solution:
        """Match runs to buses at least cost; surplus runs are folded in later."""
        V = len(self.p.vehicles)
        runs = sorted(runs, key=lambda r: -sum(self.ev.dem[x] for x in r))
        keep, spill = runs[:V], [x for r in runs[V:] for x in r]
        cost = np.array([[self.ev.route_cost(r, v) for v in range(V)] for r in keep])
        try:
            from scipy.optimize import linear_sum_assignment
            ri, vi = linear_sum_assignment(cost)
            pairs = list(zip(ri, vi))
        except ImportError:                      # greedy fallback
            pairs, used = [], set()
            for r in range(len(keep)):
                v = min((v for v in range(V) if v not in used), key=lambda v: cost[r, v])
                used.add(v)
                pairs.append((r, v))
        sol = Solution([[] for _ in range(V)])
        for r, v in pairs:
            sol.routes[v] = list(keep[r])
        self._insert_all(sol, spill)
        return sol

    def from_locks(self) -> Solution:
        """Every stop inserted at its cheapest place; a locked stop can only land on its bus."""
        sol = Solution([[] for _ in self.p.vehicles])
        self._insert_all(sol, list(range(1, self.p.n + 1)))
        return sol

    # ------------------------------------------------------------------
    # insertion
    # ------------------------------------------------------------------
    def _insert_all(self, sol: Solution, nodes: list[int]) -> None:
        ev = self.ev
        costs = [ev.route_cost(r, v) for v, r in enumerate(sol.routes)]
        for j in sorted(nodes, key=lambda x: -ev.dem[x]):
            best = (np.inf, None, None)
            tried_empty = False
            for v, r in enumerate(sol.routes):
                if not r:
                    if tried_empty and ev.vid[v] != ev.lock[j]:
                        continue                  # one empty bus is as good as another
                    tried_empty = True
                for pos in range(len(r) + 1):
                    c = ev.route_cost(r[:pos] + [j] + r[pos:], v) - costs[v]
                    if c < best[0]:
                        best = (c, v, pos)
            _, v, pos = best
            sol.routes[v].insert(pos, j)
            costs[v] = ev.route_cost(sol.routes[v], v)

    # ------------------------------------------------------------------
    # local search
    # ------------------------------------------------------------------
    def local_search(self, sol: Solution) -> Solution:
        ev = self.ev
        R = sol.routes
        cost = [ev.route_cost(r, v) for v, r in enumerate(R)]
        where = {}

        def index():
            where.clear()
            for v, r in enumerate(R):
                for p, j in enumerate(r):
                    where[j] = (v, p)

        def try_set(changes: dict[int, list[int]]) -> bool:
            new = {v: ev.route_cost(r, v) for v, r in changes.items()}
            if sum(new.values()) + 1e-6 < sum(cost[v] for v in changes):
                for v, r in changes.items():
                    R[v], cost[v] = r, new[v]
                index()
                return True
            return False

        index()
        improved = True
        while improved:
            improved = False
            nodes = list(where)
            self.rng.shuffle(nodes)
            for j in nodes:
                a, pa = where[j]
                ra = R[a]
                # relocate: next to any near neighbour, or onto one empty bus
                targets = set()
                for k in self.near[j]:
                    b, pb = where[k]
                    targets.add((b, pb)); targets.add((b, pb + 1))
                empty = next((v for v, r in enumerate(R) if not r), None)
                if empty is not None:
                    targets.add((empty, 0))
                removed = ra[:pa] + ra[pa + 1:]
                moved = False
                for b, pos in targets:
                    if b == a:
                        pos = pos - (pos > pa)
                        cand = removed[:pos] + [j] + removed[pos:]
                        moved = cand != ra and try_set({a: cand})
                    else:
                        rb = R[b]
                        moved = try_set({a: removed, b: rb[:pos] + [j] + rb[pos:]})
                    if moved:
                        break
                if not moved:
                    # swap and 2-opt* with near neighbours on other buses
                    for k in self.near[j]:
                        b, pb = where[k]
                        if b == a:
                            continue
                        rb = R[b]
                        na, nb = list(ra), list(rb)
                        na[pa], nb[pb] = k, j
                        moved = (try_set({a: na, b: nb}) or
                                 try_set({a: ra[:pa + 1] + rb[pb + 1:],
                                          b: rb[:pb + 1] + ra[pa + 1:]}))
                        if moved:
                            break
                improved |= moved
            if improved:
                continue
            # 2-opt inside each run
            for v, r in enumerate(R):
                for i in range(len(r) - 1):
                    for k in range(i + 1, len(r)):
                        cand = r[:i] + r[i:k + 1][::-1] + r[k + 1:]
                        if try_set({v: cand}):
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
        return sol

    # ------------------------------------------------------------------
    # ruin and recreate
    # ------------------------------------------------------------------
    def ruin_recreate(self, sol: Solution) -> Solution:
        n = self.p.n
        if n < 4:
            return sol.copy()
        new = sol.copy()
        seed = self.rng.randint(1, n)
        size = self.rng.randint(max(2, n // 20), max(3, n // 7))
        out = {seed, *self.near[seed][: size - 1]}
        new.routes = [[j for j in r if j not in out] for r in new.routes]
        self._insert_all(new, list(out))
        return new

    def solve(self, starts: list[tuple[str, Solution]], time_limit_s: float = 30.0,
              log=print) -> tuple[Solution, list[dict]]:
        t0 = time.time()
        history = []
        best, best_c = None, np.inf
        for label, s in starts:
            s = self.local_search(s.copy())
            c = self.ev.total(s)
            history.append({"t": round(time.time() - t0, 2), "event": f"start:{label}", "cost": round(c, 1)})
            log(f"    start {label:9s} -> {c:10.1f}")
            if c < best_c:
                best, best_c = s, c
        cur, cur_c = best.copy(), best_c
        it = 0
        while time.time() - t0 < time_limit_s and self.p.n >= 4:
            it += 1
            cand = self.local_search(self.ruin_recreate(cur))
            c = self.ev.total(cand)
            if c < cur_c * 1.005:                # accept slightly worse to escape
                cur, cur_c = cand, c
            if c + 1e-6 < best_c:
                best, best_c = cand.copy(), c
                history.append({"t": round(time.time() - t0, 2), "event": f"iter {it}", "cost": round(c, 1)})
        log(f"    {it} ruin-recreate rounds, best {best_c:.1f}")
        return best, history
