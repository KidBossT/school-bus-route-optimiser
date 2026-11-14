"""
Route sheets for the transport team: one Excel workbook with everything the
rollout needs, in three phases.

    python pipeline/build_handover.py --pilot 9,14,21 --moves optimiser_final_25
    python pipeline/build_handover.py --pilot 9,14,21 --moves optimiser_final_25 --names         --out "outputs/handover/Route sheets - internal.xlsx"

Reads outputs/routes_data.json (the planner), outputs/gps_timetable.csv (the GPS
campaign's time proposals) and, with --moves, an optimiser run's stop_moves.csv /
student_changes.csv. Writes outputs/handover/Route sheets.xlsx.

With --names the Phase 3 sheet lists children by name: that copy is for the
transport office only. Both live under outputs/ (git-ignored) and are never published.

Times:
- Phase 1 (every route): the printed time, or the GPS time where the campaign found
  it 3+ min off on 2+ runs at a confirmed stop.
- Phase 2 (pilot routes only, new stop order): each stop's Phase 1 time moved by how
  much the new order changes the bus's arrival there in the planner (one factor per
  run, Google traffic times). Morning runs keep today's school arrival, so pickups
  move later; afternoon runs keep today's departure from school.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import warnings
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transport_spine as ts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CLOSED = {23}          # not running this term
MIN_SAVE = 3           # min/run: below this a pilot run keeps today's order (not worth retraining a didi)
BUFFER = 5             # min: morning runs reach school at least this long before reporting time
COMBO = re.compile(r"^P\s*-\s*(.*?)\s+D\s*-\s*(.*)$", re.I)

HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1E3A8A")
TITLE = Font(bold=True, size=14)
SUB = Font(bold=True, size=12)
CHANGED = PatternFill("solid", fgColor="FFF2CC")
MOVED = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def same_place(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return a == b or a.startswith(b) or b.startswith(a)


def clock(m: float | None) -> str:
    if m is None or pd.isna(m):
        return ""
    m = int(round(m)) % (24 * 60)
    h, mi = divmod(m, 60)
    return f"{(h - 1) % 12 + 1}:{mi:02d} {'AM' if h < 12 else 'PM'}"


def to_min(s) -> float | None:
    if s is None or (isinstance(s, float) and pd.isna(s)) or str(s).strip() == "":
        return None
    h, m = str(s).strip().split(":")[:2]
    return int(h) * 60 + int(m)


def table(ws, row: int, headers: list[str], rows: list[list], widths=None, fills=None) -> int:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font, cell.fill, cell.border = HEAD, HEAD_FILL, BOX
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for i, vals in enumerate(rows, 1):
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=row + i, column=c, value=v)
            cell.border = BOX
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if fills and fills[i - 1]:
                cell.fill = fills[i - 1]
    return row + len(rows) + 2


def widths(ws, w: list[int]) -> None:
    for i, x in enumerate(w, 1):
        ws.column_dimensions[get_column_letter(i)].width = x


def setup_print(ws) -> None:
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.gridLines = False


def phase1_times(tt: pd.DataFrame) -> pd.DataFrame:
    """The Phase 1 time for every stop, in the order the bus really drives.

    - a confirmed stop 3+ min off on 2+ GPS days: its GPS time (the campaign's rule)
    - any other stop the GPS saw on 2+ days, on a run whose whole bus is 3+ min off the printed
      times (median over the stops it saw): printed time + that shift. Mornings take the shift
      only when it is EARLIER, so no child is told to come later than the bus may arrive.
    - then the run is kept in order: an afternoon drop is never before the one before it, a
      morning pickup never after the one after it (the earlier time wins)
    """
    tt = tt.rename(columns={"new time": "new_time"})
    tt["pr"] = tt["printed"].map(to_min)
    tt["gps"] = tt["GPS typical"].map(to_min)
    tt["runs"] = pd.to_numeric(tt["runs seen"], errors="coerce").fillna(0)
    tt["pr"] = tt["pr"].fillna(tt["new_time"].map(to_min))
    out = []
    for (num, s), g in tt.groupby(["route", "sess"], sort=True):
        g = g.copy()
        seen = g[(g["runs"] >= 2) & g["gps"].notna() & g["pr"].notna()]
        shift = float((seen["gps"] - seen["pr"]).median()) if len(seen) >= 3 else 0.0
        # where the bus really is: its GPS time if seen, else the printed time moved by the run's shift
        g["key"] = g["gps"].where(g["runs"] >= 2).fillna(g["pr"] + (shift if abs(shift) >= 3 else 0))
        g = g.sort_values(["key", "pr"], kind="stable")
        final, notes = [], []
        for t in g.itertuples():
            note = t.note if isinstance(t.note, str) else ""
            if t.is_changed:
                f = to_min(t.new_time)
            elif t.runs >= 2 and abs(shift) >= 3 and not (s == "am" and shift > 0):
                f = t.pr + round(shift)
                note = f"whole bus runs about {abs(round(shift))} min {'late' if shift > 0 else 'early'} on GPS"
            else:
                f = t.pr
            final.append(f)
            notes.append(note)
        before = list(final)
        if s == "pm":
            for i in range(1, len(final)):
                final[i] = max(final[i], final[i - 1])
        else:
            for i in range(len(final) - 2, -1, -1):
                final[i] = min(final[i], final[i + 1])
        notes = [n if a == b else "kept in order with the stops around it" for n, a, b in zip(notes, before, final)]
        g["final"], g["note"] = final, notes
        g["is_changed"] = (g["final"] - g["pr"]).abs() >= 1
        out.append(g)
    return pd.concat(out, ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pilot", default="", help="pilot route numbers, e.g. 9,14,21")
    ap.add_argument("--moves", default="", help="optimiser output folder under outputs/ for Phase 3")
    ap.add_argument("--names", action="store_true",
                    help="list children by name on the Phase 3 sheet (internal copy only)")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "handover" / "Route sheets.xlsx"))
    a = ap.parse_args()
    pilot = {int(x) for x in a.pilot.split(",") if x.strip()}

    data = json.loads((ROOT / "outputs" / "routes_data.json").read_text(encoding="utf-8"))
    routes = {r["num"]: r for r in data["routes"] if r["num"] not in CLOSED}
    tt = pd.read_csv(ROOT / "outputs" / "gps_timetable.csv", encoding="utf-8-sig")
    tt = tt[~tt["route"].isin(CLOSED)]
    tt["sess"] = tt["session"].map({"morning": "am", "afternoon": "pm"})
    tt["is_changed"] = tt["changed"].eq("yes")
    tt = tt[tt["printed"].notna() | tt["new time"].notna()]
    # stops the GPS check did not cover (Route 6, Route 25 afternoons, ...) keep their printed time
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        printed = ts.parse_route_list(ROOT / "inputs" / "Route List Area Wise with Timings.xlsx")
    extra = []
    for p in printed.itertuples():
        num = int(p.route_key[1:])
        if num in CLOSED:
            continue
        for s, sess, col in (("am", "morning", "promised_pickup_min"), ("pm", "afternoon", "promised_drop_min")):
            m = getattr(p, col)
            have = tt[(tt["route"] == num) & (tt["sess"] == s)]["stop"]
            if pd.notna(m) and p.stop_name not in set(have):
                hh = f"{int(m) // 60:02d}:{int(m) % 60:02d}"
                extra.append({"route": num, "session": sess, "stop": p.stop_name, "printed": hh,
                              "new time": hh, "changed": None, "runs seen": 0,
                              "note": "no GPS data - keep printed time", "sess": s, "is_changed": False})
    tt = pd.concat([tt, pd.DataFrame(extra)], ignore_index=True)
    tt = phase1_times(tt)

    def riders(r: dict, s: str) -> dict:
        x = r.get(s) or {}
        out = {p["name"]: p.get("riders") for p in x.get("points") or []}
        out.update({p["name"]: p.get("riders") for p in x.get("missing") or []})
        return out

    wb = Workbook()
    readme = wb.active
    readme.title = "Read me"

    # ---------------- per-route sheets (Phase 1) ----------------
    summary = []
    pilot_rows: dict[int, dict[str, list]] = {}
    for num in sorted(routes):
        r = routes[num]
        ws = wb.create_sheet(f"Route {num}")
        ws["A1"] = f"Route {num}"
        ws["A1"].font = TITLE
        ws["A2"] = (f"Reporting time {clock(r['reporting'])}. Highlighted rows: the time changes in Phase 1 "
                    "(GPS showed the bus there 3+ min off the printed time on 2 or more days).")
        row = 4
        n_changed = {}
        for s, label in (("am", "Morning pickups"), ("pm", "Afternoon drops")):
            sub = tt[(tt["route"] == num) & (tt["sess"] == s)]
            if sub.empty:
                continue
            rid = riders(r, s)
            ws.cell(row=row, column=1, value=label).font = SUB
            rows, fills = [], []
            for i, t in enumerate(sub.itertuples(), 1):
                delta = int(t.final - t.pr)
                rows.append([i, t.stop, rid.get(t.stop, ""), clock(t.pr), clock(t.final),
                             (f"{'+' if delta > 0 else ''}{delta} min" if t.is_changed else ""),
                             t.note if t.is_changed or "no GPS" in t.note or "confirm" in t.note else ""])
                fills.append(CHANGED if t.is_changed else None)
            n_changed[s] = int(sub["is_changed"].sum())
            row = table(ws, row + 1, ["#", "Stop", "Riders", "Printed time", "New time", "Change", "Note"],
                        rows, fills=fills)
            if num in pilot:
                pilot_rows.setdefault(num, {})[s] = sub
        widths(ws, [5, 46, 8, 12, 12, 10, 40])
        setup_print(ws)
        saved = sum(((r.get(s) or {}).get("plan") or {}).get("saved_min") or 0 for s in ("am", "pm"))
        summary.append([f"Route {num}", n_changed.get("am", 0), n_changed.get("pm", 0),
                        "Yes" if num in pilot else "", round(saved, 1)])

    # ---------------- Phase 1 list ----------------
    ws = wb.create_sheet("Phase 1 changes", 1)
    ws["A1"] = "Phase 1: stop times that change (all routes, same stops, same order)"
    ws["A1"].font = TITLE
    ch = tt[tt["is_changed"]].copy()
    ch["d"] = ch["final"] - ch["pr"]
    rows = [[f"Route {t['route']}", t["session"], t["stop"], clock(t["pr"]), clock(t["final"]),
             f"{'+' if t['d'] > 0 else ''}{int(t['d'])} min", int(t["runs"]), t["note"]]
            for _, t in ch.iterrows()]
    table(ws, 3, ["Route", "Session", "Stop", "Printed", "New time", "Change", "GPS days seen", "Why"], rows)
    widths(ws, [10, 11, 46, 10, 10, 10, 9, 36])
    print(f"  Phase 1: {len(ch)} stop times change on {ch.groupby(['route', 'sess']).ngroups} runs "
          f"({(ch['sess'] == 'am').sum()} morning, {(ch['sess'] == 'pm').sum()} afternoon)")
    setup_print(ws)

    # ---------------- Phase 2 pilot ----------------
    if pilot:
        ws = wb.create_sheet("Phase 2 pilot", 2)
        ws["A1"] = "Phase 2 pilot: new stop order on " + ", ".join(f"Route {n}" for n in sorted(pilot))
        ws["A1"].font = TITLE
        ws["A2"] = ("Planned times: the new order from the planner, run at the pace this bus really drives today "
                    "(GPS timetable). Mornings reach school when they do today (at least 5 min before reporting), "
                    "so most pickups move later; afternoons leave school as today. Highlighted stops move in the "
                    "order. The didi logs every pilot run on the Bus GPS app so the times can be corrected after week 1.")
        ws["A2"].alignment = Alignment(wrap_text=True)
        ws.merge_cells("A2:G2")
        ws.row_dimensions[2].height = 48
        row = 4
        for num in sorted(pilot):
            r = routes[num]
            for s, label in (("am", "morning"), ("pm", "afternoon")):
                plan = ((r.get(s) or {}).get("plan") or {})
                sub = pilot_rows.get(num, {}).get(s)
                if not plan or sub is None:
                    continue
                if (plan.get("saved_min") or 0) < MIN_SAVE:
                    ws.cell(row=row, column=1, value=f"Route {num} {label}: no change, keep today's order "
                            f"(a new order would save under {MIN_SAVE} min)").font = SUB
                    row += 2
                    continue
                cur = {st["name"]: st["eta"] for st in plan["current"]["stops"]}
                # today's order in the PRINTED sequence (the planner's 'current' is the GPS-driven order)
                seq = list(dict.fromkeys(t.stop for t in sub.itertuples()))
                p1 = {t.stop: t.final for t in sub.itertuples()}
                # The planner's clock can run longer or shorter than the bus really does (R9 AM: 18 min
                # longer end to end). So the new order is drawn at the real pace of today's run: k = real
                # span / planner span over the same stops, then anchored where the bus really is today.
                common = [n for n in cur if p1.get(n) is not None]
                real_span = max(p1[n] for n in common) - min(p1[n] for n in common)
                model_span = max(cur[n] for n in common) - min(cur[n] for n in common)
                k = real_span / model_span if model_span > 1 else 1.0
                cs, os_ = plan["current"]["school_time"], plan["optimised"]["school_time"]
                if s == "am":   # today's real arrival, never later than reporting - 5 min
                    last = max(common, key=lambda n: cur[n])
                    arrive = min(p1[last] + k * (cs - cur[last]), r["reporting"] - BUFFER)
                    at = lambda eta: arrive - k * (os_ - eta)          # noqa: E731
                else:           # today's real departure from school
                    first = min(common, key=lambda n: cur[n])
                    depart = p1[first] - k * (cur[first] - cs)
                    at = lambda eta: depart + k * (eta - os_)          # noqa: E731
                rid = riders(r, s)
                order = []
                for st in plan["optimised"]["stops"]:
                    t_plan = at(st["eta"])
                    order.append((st["name"], t_plan, ""))
                    for nm in seq:     # roster variants "P - X D - Y" ride from / to X (am) or Y (pm)
                        if nm in cur or nm in [o[0] for o in order]:
                            continue
                        m = COMBO.match(nm)
                        if m and same_place(m.group(1 if s == "am" else 2), st["name"]):
                            order.append((nm, t_plan, "same place as the stop above"))
                rows, fills = [], []
                for i, (nm, t_plan, note) in enumerate(order, 1):
                    old = seq.index(nm) + 1 if nm in seq else ""
                    t_plan = math.floor(t_plan) if s == "am" else round(t_plan)
                    rows.append([i, nm, rid.get(nm, ""), old, clock(p1.get(nm)), clock(t_plan), note])
                    fills.append(MOVED if old != i else None)
                for nm in seq:
                    if nm not in [o[0] for o in order]:
                        rows.append(["", nm, rid.get(nm, ""), seq.index(nm) + 1, clock(p1.get(nm)), "",
                                     "no map pin: didi keeps it next to its usual neighbours"])
                        fills.append(CHANGED)
                ws.cell(row=row, column=1, value=f"Route {num} {label}: saves about "
                        f"{plan['saved_min'] * k:.0f} min of driving").font = SUB
                row = table(ws, row + 1, ["New #", "Stop", "Riders", "Old #", "Time today (Phase 1)",
                                          "Planned pilot time", "Note"], rows, fills=fills)
        widths(ws, [7, 46, 8, 7, 14, 14, 30])
        setup_print(ws)

    # ---------------- Phase 3 bus changes ----------------
    if a.moves:
        mdir = ROOT / "outputs" / a.moves
        moves = pd.read_csv(mdir / "stop_moves.csv", encoding="utf-8-sig")
        kids = pd.read_csv(mdir / "student_changes.csv", encoding="utf-8-sig")
        kids = kids[kids["moved"].astype(str).str.lower() == "true"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            roster = ts.parse_roster(ROOT / "inputs" / "Route Wise Student list (for council).xlsx",
                                     id_pattern=getattr(ts, "STAFF_OR_STUDENT_ID", None))
        names = roster.drop_duplicates("admission_no").set_index("admission_no")[["student_name", "grade"]]
        ws = wb.create_sheet("Phase 3 bus changes", 3)
        ws["A1"] = "Phase 3 (needs approval): stops that move to another bus"
        ws["A1"].font = TITLE
        ws["A2"] = ("INTERNAL: contains children's names. Not for circulation outside the transport office."
                    if a.names else "Draft for decision. Children's names are in the transport office's internal copy.")
        ws["A2"].font = Font(bold=True, color="C00000")
        ws["A3"] = ("Drop the JP Wishtown move to Route 12: it makes Route 12's afternoon 5 min longer than today. "
                    "Needs a seat count on Route 11 before any change.")
        rows = [[m.direction.upper(), m.stop, int(m.riders), m.bus_today, m.bus_proposed, m.time_today,
                 m.time_proposed] for m in moves.itertuples()]
        row = table(ws, 5, ["Session", "Stop", "Riders", "Bus today", "New bus", "Time today", "New time"], rows)
        widths(ws, [9, 46, 8, 10, 10, 11, 11])
        setup_print(ws)
    if a.moves and a.names:
        ws.cell(row=row, column=1, value="Children who change bus").font = SUB
        rows = []
        for k in kids.sort_values(["bus_today", "stop", "direction"]).itertuples():
            nm = names["student_name"].get(k.admission_no, "") if k.admission_no in names.index else ""
            gr = names["grade"].get(k.admission_no, "") if k.admission_no in names.index else ""
            rows.append([k.direction.upper(), k.admission_no, nm, gr, k.stop, k.bus_today, k.bus_proposed,
                         k.printed_time, k.proposed_time])
        table(ws, row + 1, ["Session", "ID", "Name", "Grade", "Stop", "Bus today", "New bus", "Time today",
                            "New time"], rows)
        widths(ws, [9, 14, 22, 20, 40, 9, 9, 10, 10])
        setup_print(ws)

    # ---------------- summary + read me ----------------
    ws = wb.create_sheet("Summary", 1)
    ws["A1"] = "Summary by route"
    ws["A1"].font = TITLE
    table(ws, 3, ["Route", "Morning times changed", "Afternoon times changed", "Phase 2 pilot",
                  "Driving saved by new order (min/day, planner)"], summary)
    widths(ws, [10, 12, 12, 10, 18])

    lines = [
        ("Prometheus School bus routes: route sheets", TITLE),
        ("", None),
        ("Summary: one line per route.", None),
        ("Phase 1 changes: the stop times that change on every route. Same stops, same order, same bus.", None),
        ("Phase 2 pilot: the new stop order and planned times for the pilot routes only.", None),
        ("Phase 3 bus changes: only if approved. The internal copy also lists the children by name.", None),
        ("Route N: the full timetable for each route after Phase 1, to print for the didi and driver.", None),
        ("", None),
        (f"Built from the planner data and the October GPS campaign. Route 23 is not running and is left out.", None),
        ("Route 6 sent no GPS data, so its times stay as printed.", None),
    ]
    for i, (txt, f) in enumerate(lines, 1):
        readme.cell(row=i, column=1, value=txt)
        if f:
            readme.cell(row=i, column=1).font = f
    readme.column_dimensions["A"].width = 110

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"wrote {out} ({len(wb.sheetnames)} sheets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
