"""
The two handover PDFs for the chairman and the transport office.

    python pipeline/build_pdfs.py

1. outputs/handover/1 Final outcome and routes.pdf   INTERNAL: names the children who change bus
2. outputs/handover/2 Process.pdf

Both are drawn as print-styled HTML and printed by headless Chrome. Inputs are the same as
build_handover.py (planner data, GPS timetable) plus outputs/gps_stops.csv and the optimiser
runs outputs/optimiser_final_25 (moving stops between buses) and outputs/optimiser_final_23 (2-bus option).
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_handover as bh  # noqa: E402
import transport_spine as ts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "handover"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PILOT = {9, 14, 21}
MOVES = ROOT / "outputs" / "optimiser_final_25"
DATE = "14 November 2026"

CSS = """
@page { size: A4; margin: 13mm 12mm 14mm; }
* { box-sizing: border-box; }
body { font: 9.5pt/1.4 Arial, "Segoe UI", sans-serif; color: #111; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 2mm; }
h2 { font-size: 12.5pt; margin: 7mm 0 2mm; border-bottom: 1.5px solid #1e3a8a; padding-bottom: 1mm; color: #1e3a8a; }
h3 { font-size: 10.5pt; margin: 4mm 0 1.5mm; }
p, li { margin: 0 0 1.6mm; }
ul, ol { margin: 0 0 2mm; padding-left: 5mm; }
.sub { color: #555; margin-bottom: 4mm; }
.warn { color: #b91c1c; font-weight: bold; }
table { border-collapse: collapse; width: 100%; margin: 0 0 3mm; font-size: 8.4pt; }
th, td { border: 0.6px solid #bbb; padding: 1mm 1.4mm; text-align: left; vertical-align: top; }
th { background: #1e3a8a; color: #fff; font-weight: bold; }
td.n, th.n { text-align: right; white-space: nowrap; }
td.t { white-space: nowrap; }
tr.moved td { background: #e8f3e3; }
tr.nopin td { background: #fff4d6; }
b.chg { color: #b45309; }
.kv td:first-child { width: 34%; font-weight: bold; background: #f3f5f9; }
.route { break-before: page; }
.route h2 { margin-top: 0; }
.route h2.sec { margin-bottom: 4mm; }

.meta { color: #333; margin-bottom: 2mm; }
.tag { display: inline-block; background: #1e3a8a; color: #fff; padding: 0 1.6mm; border-radius: 2px; font-size: 8pt; margin-left: 2mm; }
.keep { break-inside: avoid; }
.small { font-size: 8pt; color: #444; }
"""


TIME = re.compile(r"(\d{1,2}:\d{2}) ([AP]M)")


def esc(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return html.escape(TIME.sub(lambda m: m.group(1) + " " + m.group(2), str(x)))  # "6:15 AM" on one line


def table(head: list[str], rows: list[list], num: set[int] = frozenset(), classes: list[str] | None = None) -> str:
    h = "<table><thead><tr>" + "".join(
        f"<th{' class=n' if i in num else ''}>{esc(c)}</th>" for i, c in enumerate(head)) + "</tr></thead><tbody>"
    for k, r in enumerate(rows):
        cls = f" class='{classes[k]}'" if classes and classes[k] else ""
        h += f"<tr{cls}>" + "".join(
            f"<td{' class=n' if i in num else ''}>{c if isinstance(c, str) and c.startswith('<') else esc(c)}</td>"
            for i, c in enumerate(r)) + "</tr>"
    return h + "</tbody></table>"


def kv(rows: list[tuple[str, str]]) -> str:
    return "<table class=kv><tbody>" + "".join(f"<tr><td>{esc(a)}</td><td>{b}</td></tr>" for a, b in rows) + "</tbody></table>"


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><meta charset=utf-8><title>{esc(title)}</title><style>{CSS}</style></head><body>{body}</body></html>"


def to_pdf(html_path: Path, pdf_path: Path) -> None:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf_path}", html_path.as_uri()], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def figures(routes: dict) -> dict:
    saved = kmc = kmo = cur = 0.0
    runs = faster = 0
    for r in routes.values():
        for s in ("am", "pm"):
            p = (r.get(s) or {}).get("plan")
            if not p:
                continue
            runs += 1
            faster += (p.get("saved_min") or 0) > 0.05
            saved += p.get("saved_min") or 0
            cur += p["current"]["drive_min"]
            kmc += p["current"]["km"]
            kmo += p["optimised"]["km"]
    km = kmc - kmo
    yr = km * 220
    return {"saved": saved, "pct": saved / cur, "km": km, "yr_km": yr, "runs": runs, "faster": faster,
            "rs": (yr / 5 * 88, yr / 4 * 85), "co2": (yr / 5 * 2.68 / 1000, yr / 4 * 2.7 / 1000)}


def summary_md(path: Path) -> dict:
    """bus-level numbers out of an optimiser summary.md (moved children, runs over a limit)."""
    txt = path.read_text(encoding="utf-8")
    out = {}
    for sess, part in zip(("am", "pm"), txt.split("## Afternoon")):
        for key, label in (("over", "Buses over a time limit"), ("moved", "Children moved to another bus")):
            line = next(l for l in part.splitlines() if l.startswith(f"| {label}"))
            out[(sess, key)] = int(line.split("|")[3].strip())
    return out


def unsure_stops() -> pd.DataFrame:
    """Stops still unconfirmed: the GPS never timed the bus standing still there.

    A stop where the logger timed the bus standing still (a dwell) on any run counts as confirmed, even
    if its name or pin was in doubt; what is left are stops the bus was never seen stopping at.
    """
    g = pd.read_csv(ROOT / "outputs" / "gps_stops.csv", encoding="utf-8-sig")
    g = g[~g["route"].isin(bh.CLOSED)]
    still = g[g["dwell (s)"].fillna(0) > 0].set_index(["route", "stop"]).index
    g = g[~g.set_index(["route", "stop"]).index.isin(still)]
    unsure = g[(g["name confidence"] == "low") | (g["location confidence"] == "low") | g["status"].str.startswith("NOT SEEN")
               | g["stop"].str.startswith("(GPS stop")]
    unsure = unsure.assign(why=unsure["what to check"].fillna(unsure["status"]))
    return unsure.groupby(["route", "stop"], sort=True).agg(
        sessions=("session", lambda x: " + ".join(sorted(set(x)))), why=("why", "first")).reset_index()


# ------------------------------------------------------------------ PDF 1
def pdf1(routes: dict, tt: pd.DataFrame) -> str:
    f = figures(routes)
    cfg = json.loads((ROOT / "optimiser_config.json").read_text(encoding="utf-8"))
    seats = cfg.get("capacity_by_route", {})
    o25, o23 = summary_md(MOVES / "summary.md"), summary_md(ROOT / "outputs" / "optimiser_final_23" / "summary.md")
    ch = tt[tt["is_changed"]]
    am_ch, pm_ch = ch[ch["sess"] == "am"], ch[ch["sess"] == "pm"]
    moves = pd.read_csv(MOVES / "stop_moves.csv", encoding="utf-8-sig")

    b = f"<h1>School bus routes: final outcome and routes</h1>"
    b += (f"<div class=sub>Prometheus School, Noida. {DATE}. Prepared for the chairman and the transport office. "
          f"<span class=warn>Internal: section 4 names children.</span></div>")

    b += "<h2>1. Final outcome</h2>"
    b += kv([
        ("Routes", "24 running (Route 23 is on the timetable but no bus runs it this term)"),
        ("Driving saved, new stop order", f"<b>{f['saved']:.0f} min a day</b> ({f['pct']:.1%} less drive time); "
                                          f"{f['faster']} of {f['runs']} runs faster, none longer in time or km"),
        ("Distance saved", f"<b>{f['km']:.0f} km a day</b>, about {f['yr_km']:,.0f} km a school year (220 days)"),
        ("Fuel saved", f"Rs {f['rs'][0] / 1e5:.1f} to {f['rs'][1] / 1e5:.1f} lakh a year (all diesel to all CNG)"),
        ("CO2 avoided", f"{f['co2'][0]:.0f} to {f['co2'][1]:.0f} tonnes a year"),
        ("Timetable corrected", f"{len(ch)} stop times on {ch.groupby(['route', 'sess']).ngroups} of 45 runs: "
                                f"{len(am_ch)} morning (at most {int((am_ch['final'] - am_ch['pr']).abs().max())} min), "
                                f"{len(pm_ch)} afternoon"),
        ("Moving stops between buses", f"Studied, not proposed now: {(moves['direction'] == 'am').sum()} morning and "
                                       f"{(moves['direction'] == 'pm').sum()} afternoon stops could move bus "
                                       f"({o25[('am', 'moved')]} and {o25[('pm', 'moved')]} riders), mainly to take load off "
                                       f"Route 11 (section 4)"),
        ("Fleet", f"Stays at 25 buses. Taking Routes 17 and 18 off works in the mornings but puts "
                  f"{o23[('pm', 'over')]} afternoon runs over the time limit, so it is not proposed"),
        ("Cost", "None: no new bus, driver or spending"),
    ])

    b += "<h2>2. Plan</h2>"
    b += table(["Phase", "What changes", "Needs"], [
        ["1. Corrected timetable", "Every route: same stops, order and bus; only the times change (section 5, column Corrected)",
         "Go-ahead; parent notice before it starts"],
        ["2. New stop order", "Same stops and bus, new order (section 5, column New order). Pilot on Routes 9, 14 (afternoon) "
         "and 21 first, then the other routes", "Go-ahead; didis log GPS during the pilot"],
    ])
    b += "<h2>3. How to read the route pages</h2><ul>"
    b += ("<li><b>Today #</b>: position in today's order. <b>New #</b>: position in the new order. Green rows move.</li>"
          "<li><b>Printed</b>: the current timetable. <b>Corrected</b>: Phase 1 time (orange = changed). "
          "<b>New order</b>: the time in the new order (Phase 2).</li>"
          "<li>Corrected times: a confirmed stop gets its GPS time when the bus was 3+ min off on 2+ days; on a run where "
          "the whole bus is 3+ min off, the other stops it was seen at move by that amount (mornings only earlier); "
          "each run is kept in driving order.</li>"
          "<li>New order times: the planner's order run at the pace the bus really drives; mornings reach school when they "
          "do today and at least 5 min before reporting. A run whose new order would save under 3 min keeps today's order.</li>"
          "<li>Yellow rows have no map pin: the didi keeps that stop next to its usual neighbours.</li></ul>")

    # ---- section 4: bus changes with names
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        roster = ts.parse_roster(ROOT / "inputs" / "Route Wise Student list (for council).xlsx",
                                 id_pattern=getattr(ts, "STAFF_OR_STUDENT_ID", None))
    names = roster.drop_duplicates("admission_no").set_index("admission_no")[["student_name", "grade"]]
    kids = pd.read_csv(MOVES / "student_changes.csv", encoding="utf-8-sig")
    kids = kids[kids["moved"].astype(str).str.lower() == "true"]
    b += "<h2 style='break-before:page'>4. Stops that could change bus (studied, not proposed now)</h2>"
    b += ("<p>For reference only: the result of letting the optimiser move stops between buses. Each child stays on one "
          "bus both ways. The JP Wishtown move to Route 12 would make Route 12's afternoon 5 min longer than today, so it "
          "would be dropped. Any of this needs a seat count on Route 11 first.</p>")
    b += table(["Session", "Stop", "Riders", "Bus today", "New bus", "Time today", "New time"],
               [[m.direction.upper(), m.stop, int(m.riders), f"Route {int(m.bus_today[1:])}",
                 f"Route {int(m.bus_proposed[1:])}", m.time_today, m.time_proposed]
                for m in moves.sort_values(["direction", "bus_today"]).itertuples()], num={2})
    b += "<h3>Children who would change bus</h3>"
    rows = []
    for k in kids.sort_values(["bus_today", "stop", "direction"]).itertuples():
        nm, gr = (names.loc[k.admission_no, "student_name"], names.loc[k.admission_no, "grade"]) \
            if k.admission_no in names.index else ("(staff)", "")
        rows.append([nm, gr, k.admission_no, k.direction.upper(), k.stop,
                     f"{int(k.bus_today[1:])} to {int(k.bus_proposed[1:])}", f"{k.printed_time} to {k.proposed_time}"])
    b += table(["Name", "Grade", "ID", "Session", "Stop", "Route", "Time"], rows)

    # ---- section 5: every route
    for k_, num in enumerate(sorted(routes)):
        r = routes[num]
        b += "<div class=route>" + ("<h2 class=sec>5. Final routes</h2>" if k_ == 0 else "")
        b += f"<h2>Route {num}{'<span class=tag>PILOT</span>' if num in PILOT else ''}</h2>"
        rk = f"R{num:02d}"
        b += (f"<div class=meta>Reporting {bh.clock(r['reporting'])} &middot; {r['children']} riders listed &middot; "
              f"{seats.get(rk, '?')} seats</div>")
        for s, label in (("am", "Morning pickups"), ("pm", "Afternoon drops")):
            sub = tt[(tt["route"] == num) & (tt["sess"] == s)]
            if sub.empty:
                continue
            res = bh.new_order(r, s, sub)
            p1 = {t.stop: t for t in sub.itertuples()}
            if res is None:
                note = "order unchanged" + ("" if num == 6 else " (a new order would save under 3 min)")
                rows, cls = [], []
                for i, t in enumerate(sub.itertuples(), 1):
                    corr = f"<b class=chg>{esc(bh.clock(t.final))}</b>" if t.is_changed else bh.clock(t.final)
                    rows.append([i, i, t.stop, bh.riders(r, s).get(t.stop, ""), bh.clock(t.pr), corr, bh.clock(t.final)])
                    cls.append("")
            else:
                note = f"new order saves about {res['saved']:.0f} min of driving"
                rows, cls = [], []
                for x in res["rows"]:
                    t = p1.get(x["stop"])
                    corr = bh.clock(x["p1"])
                    if t is not None and t.is_changed:
                        corr = f"<b class=chg>{esc(corr)}</b>"
                    rows.append([x["new"] or "-", x["old"], x["stop"], x["riders"], bh.clock(t.pr) if t is not None else "",
                                 corr, bh.clock(x["planned"]) or "keep near neighbours"])
                    cls.append("nopin" if x["note"].startswith("no map pin") else "moved" if x["old"] != x["new"] else "")
            b += f"<div class=keep><h3>{label} <span class=small>&middot; {note}</span></h3>"
            b += table(["New #", "Today #", "Stop", "Riders", "Printed", "Corrected", "New order"], rows,
                       num={0, 1, 3}, classes=cls) + "</div>"
        b += "</div>"
    # ---- section 6: stops not yet confirmed (last: not needed for this rollout)
    unsure = unsure_stops()
    b += "<h2 style='break-before:page'>6. Stops to confirm</h2>"
    b += (f"<p><b>Not essential right now:</b> no major reordering is done, so these stops keep their place and their "
          f"printed time. {len(unsure)} listed stops where the GPS never timed the bus standing still (any stop where it "
          "did counts as confirmed). Check them when the route list is next updated.</p>")
    b += table(["Route", "Stop", "Session", "What the GPS showed"],
               [[r.route, r.stop, r.sessions.replace("am", "morning").replace("pm", "afternoon"), r.why]
                for r in unsure.itertuples()])
    return page("Final outcome and routes", b)


# ------------------------------------------------------------------ PDF 2
def pdf2(routes: dict, tt: pd.DataFrame) -> str:
    f = figures(routes)
    calib = json.loads((ROOT / "outputs" / "routes_data.json").read_text(encoding="utf-8")).get("calibration", {})
    vf = (calib.get("model") or {}).get("verified_factor") or {}
    b = "<h1>School bus routes: the process</h1>"
    b += f"<div class=sub>Prometheus School, Noida. {DATE}. How the routes and times in the outcome document were made.</div>"

    b += "<h2>1. Data used</h2>"
    b += table(["Source", "What it gave"], [
        ["Route list with timings (school)", "25 routes, every stop in order, printed pickup and drop times, reporting times"],
        ["Route-wise student list (school)", "Who rides from which stop on which bus, including staff"],
        ["RFID attendance logs (school)", "About 10,000 ID and exit card taps (used for timing, lightly: taps lag the real boarding)"],
        ["Vehicle list (school)", "Seats per bus, 20 to 51"],
        ["GPS campaign (bus didis, 21, 22, 23 and 27 Oct)", "141 runs on 23 routes from our own tracker app: one tap at every stop"],
        ["Google Maps", "Stop locations, and travel times with traffic for every leg"],
    ])

    b += "<h2>2. Steps</h2><ol>"
    for x in [
        "<b>Clean and join the spreadsheets.</b> Each child is matched to a stop and a bus for the morning and the afternoon; "
        "names that differ only in spelling are joined.",
        "<b>Put every stop on the map.</b> Each stop name is searched on Google Places and Geocoding and on OpenStreetMap. "
        "Rare words in the name (a society or market) count more than common ones (sector, gate, metro). "
        "A pin counts as confirmed when two sources agree within 300 m.",
        "<b>GPS campaign.</b> The didis ran our tracker app for four school days. Where the bus stood still at a tap is the real "
        "stop: 117 pins moved (median 75 m; Google pins were often 50 to 600 m off, eg the society gate instead of the tower). "
        "It also gave the real time at every stop and the time spent at each stop (median 52 s morning, 43 s afternoon).",
        "<b>Travel times.</b> Google's traffic prediction for each leg on a school day. Between GPS-confirmed pins Google matched "
        f"the bus closely, so it is used almost as is (x{vf.get('am', 0.976)} morning, x{vf.get('pm', 1.04)} afternoon, fitted on "
        "942 GPS legs). Other legs are scaled by distance band, fitted 80% on the printed timetable and 20% on card taps. "
        "Checked against GPS: a whole run within about 3.5 min in the morning and 5 min in the afternoon.",
        "<b>Phase 1, corrected timetable.</b> A confirmed stop where the bus was 3+ min off the printed time on 2+ days gets its GPS "
        "time. On a run where the whole bus is 3+ min off, the other stops it was seen at move by that amount; mornings only ever "
        "move earlier this way, so no child is told to come later than the bus may arrive. Each run is then kept in driving order.",
        "<b>Phase 2, new stop order.</b> For each run the best order of the same stops is searched: exactly (every order) up to 13 "
        "stops, by repeated swapping above that. Mornings end at school, afternoons start there. A new order is kept only if Google "
        "times the full route faster than today's order and it is no longer in km. "
        f"Result: {f['saved']:.0f} min and {f['km']:.0f} km a day across the 24 running routes, {f['faster']} of {f['runs']} runs faster.",
        "<b>Phase 2 times.</b> The planner's clock can run longer or shorter than a bus really does, so each new order is stretched "
        "to the real pace of today's run (GPS), mornings anchored to today's school arrival (never later than 5 min before reporting), "
        "afternoons to today's departure. A run that would save under 3 min keeps today's order.",
        "<b>Moving stops between buses (studied).</b> A second program may move stops between buses as well as reorder them, under these rules: "
        "real seats (planned to 85%), longest ride 75 min, no pickup before 6:00, last drop by 4:45, every morning bus at school 5 min "
        "early, no run longer than today, each child on one bus both ways, and only GPS-confirmed stops may move.",
        "<b>Pilot first.</b> Routes 9, 14 and 21 (most GPS-confirmed stops) drive the new order first with GPS logging; the "
        "times are corrected from that before any other route changes.",
    ]:
        b += f"<li>{x}</li>"
    b += "</ol>"

    b += "<h2>3. Within routes or between routes?</h2>"
    b += ("<p><b>The 205 min a day is within routes only</b>: every child keeps their bus and stop, only the order of stops on each "
          "bus changes.</p><p><b>Swapping stops between routes was done too</b> (step 8), but it is not in the main proposal yet:</p><ul>"
          "<li><b>It adds little time.</b> On the same model, moving stops between buses saved only about 26 more bus-minutes in the "
          "morning and 1 in the afternoon on top of reordering (first run, 28 Oct: 99 vs 73 and 186 vs 185). Most of the gain is in the order.</li>"
          "<li><b>Only confirmed stops can move.</b> 130 morning stops (278 riders) have no GPS-confirmed position, so they stay on "
          "their bus; moving a stop we cannot place could strand a child.</li>"
          "<li><b>It changes the child's bus, didi and driver</b>, both ways, so it needs parents told individually and a seat check.</li>"
          "<li><b>Seats are tight.</b> With the real seat counts the useful moves are the ones that take load off Route 11 (full at 44 of 51).</li>"
          "<li><b>Fewer buses is not proven.</b> Taking Routes 17 and 18 off works in the mornings but puts 4 afternoon runs over the "
          "time limit (Route 3 about 12 min longer than today), so it would make some children's rides worse.</li></ul>"
          "<p>So the plan proves the order first (pilot), then decides the bus moves with the seat count and the pilot's GPS data.</p>")

    b += "<h2>4. Limits</h2><ul>"
    for x in [
        "GPS covers 4 school days in October (afternoons 3, one of them an early dismissal). Winter fog and exam days will change times; "
        "another GPS week later rechecks every route.",
        f"{len(unsure_stops())} listed stops were never timed by the GPS with the bus standing still; they keep their "
        "place and printed time (outcome document, section 6). Not needed for this rollout.",
        "Route 6 sent no GPS data, so its times stay as printed. Route 23 is not running.",
        "The 75 min ride limit, 6:00 earliest pickup, 4:45 last drop and 85% seat fill are assumptions to confirm.",
        "Fuel and CO2 count distance only (CNG 4 km/kg at Rs 85, diesel 5 km/litre at Rs 88, 220 school days).",
    ]:
        b += f"<li>{x}</li>"
    b += "</ul>"

    b += "<h2>5. How to run it again</h2>"
    b += table(["Step", "Command"], [
        ["Check the Google key", "python pipeline/gmaps.py --check"],
        ["New GPS files in", "python pipeline/gps_campaign.py (see docs/TECHNICAL.md)"],
        ["Rebuild the planner (routes, times, new order)", "python pipeline/build_routes.py"],
        ["Moving stops between buses", "python pipeline/run_optimiser.py --same-bus --lead am --out optimiser_final_25"],
        ["Route sheets (Excel)", "python pipeline/build_handover.py --pilot 9,14,21 --moves optimiser_final_25"],
        ["These two PDFs", "python pipeline/build_pdfs.py"],
        ["Update the password-locked website", "python pipeline/build_site.py, then commit and push"],
    ])
    return page("Process", b)


def main() -> int:
    routes, tt = bh.load()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in (("1 Final outcome and routes", pdf1(routes, tt)), ("2 Process", pdf2(routes, tt))):
        h = OUT / f"{name}.html"
        h.write_text(body, encoding="utf-8")
        to_pdf(h, OUT / f"{name}.pdf")
        if not os.environ.get("KEEP_HTML"):
            h.unlink()
        print(f"wrote {OUT / (name + '.pdf')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
