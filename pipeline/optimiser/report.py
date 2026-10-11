"""Writes the optimiser's outputs: machine-readable plan, human summary, change lists."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

import transport_spine as ts

METRICS = [
    ("buses_used", "Buses used", ""),
    ("buses_held_only", "... running only for held stops", ""),
    ("bus_minutes", "Total bus-minutes", "min"),
    ("bus_km", "Total bus-km", "km"),
    ("longest_run_min", "Longest run", "min"),
    ("student_ride_min_mean", "Mean ride per child", "min"),
    ("student_ride_min_max", "Longest ride of any child", "min"),
    ("seat_utilisation", "Seat utilisation", ""),
    ("routes_over_capacity", "Buses over capacity", ""),
    ("routes_over_time", "Buses over a time limit", ""),
    ("children_reassigned", "Children moved to another bus", ""),
    ("estimated_leg_share", "Legs on estimated times", ""),
    ("objective", "Objective (lower is better)", ""),
]


def clock(m):
    return ts.minutes_to_clock(m) if m is not None else ""


def student_changes(direction: str, routes: list[dict], nodes_by_id: dict) -> list[dict]:
    rows = []
    for r in routes:
        for s in r["stops"]:
            nd = nodes_by_id[s["node"]]
            for adm in nd.students:
                rows.append({
                    "direction": direction, "admission_no": adm, "stop": s["name"],
                    "bus_today": s["was_on"], "bus_proposed": r["vehicle"],
                    "moved": s["was_on"] != r["vehicle"],
                    "printed_time": clock(s["printed_min"]),
                    "proposed_time": clock(s["eta_min"]),
                    "shift_min": (None if s["printed_min"] is None or s["eta_min"] is None
                                  else round(s["eta_min"] - s["printed_min"], 1)),
                    "ride_min": s["ride_min"],
                })
    return rows


def write(out_dir: Path, results: dict, cfg_dict: dict, draft: bool, label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    plan = {"generated": stamp, "input": label, "draft": draft, "config": cfg_dict,
            "directions": {}}
    changes, readiness, merges = [], [], []
    md = [f"# Route optimisation - {label}", "",
          f"Generated {stamp}."]
    if draft:
        md += ["", "> **DRAFT - built on unverified coordinates.** A wrong pin produces a "
               "wrong plan. Do not act on this until every routed stop is verified."]
    for d, r in results.items():
        b, ev = r["built"], r["ev"]
        cur = [ev.route_detail(s, v) for v, s in enumerate(b.current.routes) if s]
        opt = [ev.route_detail(s, v) for v, s in enumerate(r["best"].routes) if s]
        sc, so = ev.summary(b.current), ev.summary(r["best"])
        plan["directions"][d] = {
            "notes": b.problem.notes, "summary_current": sc, "summary_optimised": so,
            "routes_current": cur, "routes_optimised": opt, "held": b.held,
            "stop_merge_proposals": b.merges, "search_history": r["history"],
        }
        # a stop no bus can serve inside the ride limit even on its own is
        # either a wrong pin or a limit/speed assumption that does not fit it
        T, S = ev.T, ev.S
        unreachable = [{"node": nd.id, "name": nd.name, "alone_min": round(T[0, j] + S[j], 1)}
                       for j, nd in enumerate(b.problem.nodes, start=1)
                       if T[0, j] + S[j] > ev.max_duration([j])]
        plan["directions"][d]["unreachable_alone"] = unreachable
        nodes = {nd.id: nd for nd in b.problem.nodes}
        changes += student_changes(d, opt, nodes)
        readiness += b.readiness
        merges += [{"direction": d, "hub": m["hub_name"], "member": x["name"],
                    "walk_m": x["walk_m"], "children": x["children"],
                    "bus_today": x["bus_today"], "same_place": m["same_place"]}
                   for m in b.merges for x in m["members"]]

        title = "Morning (pickups -> school)" if d == "am" else "Afternoon (school -> drops)"
        md += ["", f"## {title}", ""]
        md += [f"- {n}" for n in b.problem.notes]
        md += [f"- {b.problem.n} stops routed, {len(b.held)} held",
               f"- {len(b.merges)} stop-merge proposals "
               f"({'applied' if cfg_dict['apply_stop_merges'] else 'not applied'})"]
        if unreachable:
            md += [f"- **{len(unreachable)} stops cannot be reached inside the time limit even by "
                   f"a bus serving nothing else** - check the coordinate, then `max_ride_min` and the "
                   f"travel-time source: "
                   + "; ".join(f"{u['name']} ({u['alone_min']} min)" for u in unreachable[:8])
                   + (" ..." if len(unreachable) > 8 else "")]
        md += ["",
               "| Metric | Today | Optimised | Change |", "|---|---:|---:|---:|"]
        for k, name, unit in METRICS:
            a, o = sc[k], so[k]
            md.append(f"| {name} | {a}{' ' + unit if unit else ''} | {o}{' ' + unit if unit else ''} "
                      f"| {round(o - a, 3):+} |")
        md += ["", "| Bus | Stops | Children | Seats | Run | Start | End | Longest ride |",
               "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for x in opt:
            md.append(f"| {x['vehicle']} | {len(x['stops'])} | {x['children']}"
                      f"{' (+' + str(x['reserved_seats']) + ' held)' if x['reserved_seats'] else ''} "
                      f"| {x['capacity']} | {x['duration_min']} min | {clock(x['start_min'])} "
                      f"| {clock(x['end_min'])} | {x['max_ride_min']} min |")
    md += ["", "## Files", "",
           "- `plan.json` - every route, stop, ETA and metric, today vs optimised",
           "- `student_changes.csv` - per child: bus today, bus proposed, time shift",
           "- `readiness.csv` - which stops were routed and why the rest were held",
           "- `stop_merges.csv` - nearby stops that could become one",
           "", "Weights, limits and every ASSUMPTION are in `plan.json -> config` and "
           "can be changed in `optimiser_config.json`."]

    (out_dir / "plan.json").write_text(json.dumps(plan, indent=1, default=float), encoding="utf-8")
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    pd.DataFrame(changes).to_csv(out_dir / "student_changes.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(readiness).to_csv(out_dir / "readiness.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(merges, columns=["direction", "hub", "member", "walk_m", "children",
                                  "bus_today", "same_place"]).to_csv(
        out_dir / "stop_merges.csv", index=False, encoding="utf-8-sig")
    return out_dir / "summary.md"
