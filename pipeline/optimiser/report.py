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


def stop_moves(direction: str, cur: list[dict], opt: list[dict]) -> list[dict]:
    """One row per stop the proposal moves to another bus: who, from where to where, what it does to times."""
    today = {s["node"]: (r["vehicle"], s) for r in cur for s in r["stops"]}
    runs_today = {r["vehicle"]: r["duration_min"] for r in cur}
    runs_new = {r["vehicle"]: r["duration_min"] for r in opt}
    rows = []
    for r in opt:
        for s in r["stops"]:
            was, t = today.get(s["node"], (s["was_on"], None))
            if was == r["vehicle"]:
                continue
            rows.append({
                "direction": direction, "stop": s["name"], "riders": s["children"],
                "bus_today": was, "bus_proposed": r["vehicle"],
                "time_today": clock(t["eta_min"]) if t else "", "time_proposed": clock(s["eta_min"]),
                "ride_today_min": t["ride_min"] if t else None, "ride_proposed_min": s["ride_min"],
                "run_today_min": runs_today.get(r["vehicle"]), "run_proposed_min": runs_new.get(r["vehicle"]),
                "from_run_today_min": runs_today.get(was), "from_run_proposed_min": runs_new.get(was, 0.0),
                "lat": s["lat"], "lon": s["lon"],
            })
    return rows


def write_map(path: Path, plan: dict) -> Path:
    """The proposal on a Google map: each bus's run, stops that change bus ringed."""
    import gmaps
    gmaps.load_env()
    view = {k: plan[k] for k in ("generated", "input", "draft", "school")}
    view["directions"] = {d: {k: D[k] for k in ("notes", "summary_current", "summary_optimised",
                                                 "routes_current", "routes_optimised")}
                          for d, D in plan["directions"].items()}
    tpl = (Path(__file__).resolve().parent / "moves_template.html").read_text(encoding="utf-8")
    path.write_text(tpl.replace("__GMAP_TAGS__", gmaps.browser_map_tags("light"))
                    .replace("__DATA__", json.dumps(view, separators=(",", ":"), default=float).replace("</", "<\\/")),
                    encoding="utf-8")
    return path


def write(out_dir: Path, results: dict, cfg_dict: dict, draft: bool, label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    plan = {"generated": stamp, "input": label, "draft": draft, "config": cfg_dict,
            "directions": {}}
    changes, readiness, merges, moves = [], [], [], []
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
        nv = len(b.problem.vehicles)
        unreachable = [{"node": nd.id, "name": nd.name, "alone_min": round(T[0, j] + S[j], 1)}
                       for j, nd in enumerate(b.problem.nodes, start=1)
                       if T[0, j] + S[j] > max(ev.max_duration([j], v) for v in range(nv))]
        plan["directions"][d]["unreachable_alone"] = unreachable
        nodes = {nd.id: nd for nd in b.problem.nodes}
        changes += student_changes(d, opt, nodes)
        mv = stop_moves(d, cur, opt)
        moves += mv
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
        if mv:
            md += ["", f"**{len(mv)} stops move to another bus** ({sum(m['riders'] for m in mv)} riders):", "",
                   "| Stop | Riders | From | To | Time today | Proposed |", "|---|---:|---|---|---:|---:|"]
            for m in sorted(mv, key=lambda m: (m["bus_today"], m["bus_proposed"])):
                md.append(f"| {m['stop']} | {m['riders']} | {m['bus_today']} | {m['bus_proposed']} "
                          f"| {m['time_today']} | {m['time_proposed']} |")
        today_run = {x["vehicle"]: x["duration_min"] for x in cur}
        md += ["", "| Bus | Stops | Riders | Seats | Run today | Run proposed | Start | End | Longest ride |",
               "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for x in opt:
            md.append(f"| {x['vehicle']} | {len(x['stops'])} | {x['children']}"
                      f"{' (+' + str(x['reserved_seats']) + ' held)' if x['reserved_seats'] else ''} "
                      f"| {x['capacity']} | {today_run.get(x['vehicle'], 0)} min | {x['duration_min']} min "
                      f"| {clock(x['start_min'])} | {clock(x['end_min'])} | {x['max_ride_min']} min |")
        gone = sorted(set(today_run) - {x["vehicle"] for x in opt})
        if gone:
            md.append(f"\nNot needed in the proposal: {', '.join(gone)}")
    md += ["", "## Files", "",
           "- `plan.json` - every route, stop, ETA and metric, today vs optimised",
           "- `student_changes.csv` - per child: bus today, bus proposed, time shift",
           "- `stop_moves.csv` - per stop that changes bus: from, to, times and run lengths",
           "- `map.html` - the proposal on a map (open with `python pipeline/serve.py`)",
           "- `readiness.csv` - which stops were routed and why the rest were held",
           "- `stop_merges.csv` - nearby stops that could become one",
           "", "Weights, limits and every ASSUMPTION are in `plan.json -> config` and "
           "can be changed in `optimiser_config.json`."]

    if results:
        plan["school"] = list(next(iter(results.values()))["built"].problem.school)
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=1, default=float), encoding="utf-8")
    write_map(out_dir / "map.html", plan)
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    pd.DataFrame(changes).to_csv(out_dir / "student_changes.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(readiness).to_csv(out_dir / "readiness.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(moves, columns=["direction", "stop", "riders", "bus_today", "bus_proposed", "time_today",
                                 "time_proposed", "ride_today_min", "ride_proposed_min", "run_today_min",
                                 "run_proposed_min", "from_run_today_min", "from_run_proposed_min",
                                 "lat", "lon"]).to_csv(out_dir / "stop_moves.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(merges, columns=["direction", "hub", "member", "walk_m", "children",
                                  "bus_today", "same_place"]).to_csv(
        out_dir / "stop_merges.csv", index=False, encoding="utf-8-sig")
    return out_dir / "summary.md"
