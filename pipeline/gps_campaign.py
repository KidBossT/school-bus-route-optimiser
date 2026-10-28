"""
GPS campaign: a folder of bus runs logged by the didis -> where each bus really stops, when, and
what a person still has to check.

    python pipeline/gps_campaign.py "GPS Bus Data" --no-seniors 2026-10-21,2026-10-22,2026-10-23
    python pipeline/gps_campaign.py "GPS Bus Data" --no-seniors ... --places       # + Google name check
    python pipeline/gps_campaign.py "GPS Bus Data" --no-seniors ... --apply-pins   # write confident pins

The tracker files are messy in known ways, so nothing is taken from a file at face value:
  * a run is rebuilt by clock time (04:30-10:30 morning, 12:00-20:30 afternoon) from every file of
    that route and day - didis picked "afternoon" in the morning, resumed a test run from days before,
    split one run over several files, or forgot to end a run until the evening;
  * a stop is where the bus STOOD (the track), not where a button was pressed: a tap only says the
    didi thought it was a stop, and taps were missed, repeated (up to 77 in a run) or made while moving;
  * a stop counts when it recurs across days; each recurring place is named from the roster by
    printed order and time, distance to the planner's pin, the August card taps, who rides there
    (Grade 9-12 stops should be empty on --no-seniors days) and, with --places, a Google Places
    search for the stop's name around the GPS position.

Writes cache/gps_campaign.json (everything), outputs/gps_check.html (map + table per route, open
with python pipeline/serve.py) and outputs/gps_stops.csv (one row per roster stop). --apply-pins
puts every stop whose position AND name are confirmed into cache/geocode_overrides.json, noted
"GPS campaign ..." (a pin a person placed by hand is never touched).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import math
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo
import transport_spine as ts

ROOT = Path(__file__).resolve().parent.parent
IST = dt.timedelta(hours=5, minutes=30)
NS = {"g": "http://www.topografix.com/GPX/1/1"}

WINDOWS = {"am": (4.5, 10.5), "pm": (12.0, 20.5)}   # hours: a fix belongs to the run of its clock time
MIN_RUN_PTS = 30           # fewer fixes than this is a false start (web-tracker runs with the screen off are thin)
MIN_RUN_AWAY_M = 1500      # a run that never gets this far from school is a test or a bus parked at school
SCHOOL_NEAR_M = 250        # morning run ends on reaching school; afternoon run starts on leaving it
SCHOOL_FAR_M = 1500
V_STOP = 1.0               # m/s, over 4 s of median-smoothed track: below this the bus is standing
MIN_SPELL_S = 8
MERGE_GAP_S = 12           # two standing spells this close in time and space are one stop
MERGE_M = 30
KEEP_SPELL_S = 15          # untapped spells shorter than this are traffic, not candidate stops
TAP_SPELL_S = 45           # a tap belongs to a spell it falls in, or one this close in time ...
TAP_SPELL_M = 60           # ... and space; otherwise it was made while moving
REPEAT_TAP_S = 60          # a moving tap this soon after another tap is the same press repeated
SITE_M = 40                # spells from different days this close are one place
ABSORB_M = 90              # ... or this close, if they never coincide on a day
TOL = {"am": 3.0, "pm": 5.0}            # minutes off the printed time before the score drops
CARD_LAG = {"am": 2.6, "pm": 3.6}       # Aug card tap minus GPS arrival, median at agreeing stops
MIN_CARD_DAYS = 8
OFFSET_SEARCH = {"am": (-20, 20), "pm": (-60, 30)}   # a run's clock shift vs the printed times
MATCH_MIN = 0.9            # weakest evidence that may name a place
PIN_RANK = {"verified": 3, "strong": 2, "weak": 1, "none": 0}
PROX = {
    "verified": ((40, 2.5), (80, 1.5), (150, 0.5), (300, -1.0)),
    "strong": ((60, 2.0), (120, 1.6), (250, 1.0), (400, 0.5), (700, 0.0), (1200, -1.0)),
    "weak": ((120, 1.2), (250, 0.8), (400, 0.4), (700, 0.0), (1500, -0.5)),
}
PROX_FAR = {"verified": -3.0, "strong": -3.0, "weak": -1.5}
NAME_SCORE = {"confirmed": 1.5, "nearby": 0.4, "area": 0.2}
GENERIC = set("""gate main road near opp opposite sector sec block phase the and noida delhi greater west east
new back entry society apartment apartments apartmnt market red light chowk round about roundabout point side
roadside front inside outside towers tower enclave extension ext no gol chakkar golchakkar cross crossing metro
station india uttar pradesh ghaziabad""".split())


def hav_m(a, b, c, d) -> float:
    return geo.haversine_km(a, b, c, d) * 1000.0


def dist_arr(la, lo, lat, lon):
    la_r = np.radians(la)
    return 2 * 6371000 * np.arcsin(np.sqrt(np.sin((la_r - math.radians(lat)) / 2) ** 2 + math.cos(math.radians(lat))
                                           * np.cos(la_r) * np.sin((np.radians(lo) - math.radians(lon)) / 2) ** 2))


def mins(t: dt.datetime) -> float:
    return t.hour * 60 + t.minute + t.second / 60


def hm(m) -> str:
    if m is None:
        return ""
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------
# Files -> runs
# --------------------------------------------------------------------------

def _ist(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) + IST


def read_gpx(path: Path) -> dict:
    root = ET.parse(path).getroot()
    desc = root.find("g:metadata/g:desc", NS)
    m = re.search(r"route=(\d+)\s+session=(am|pm)", desc.text if desc is not None and desc.text else "")
    if not m:
        m = re.match(r"R(\d+)_(am|pm)_", path.name)
    pts = [(float(e.get("lat")), float(e.get("lon")), _ist(e.find("g:time", NS).text))
           for e in root.iterfind(".//g:trkpt", NS) if e.find("g:time", NS) is not None]
    taps = [(float(e.get("lat")), float(e.get("lon")), _ist(e.find("g:time", NS).text))
            for e in root.iterfind("g:wpt", NS) if e.find("g:time", NS) is not None]
    return dict(route=int(m.group(1)) if m else None, label=m.group(2) if m else None,
                creator=root.get("creator", ""), pts=pts, taps=taps)


def session_of(t: dt.datetime) -> str | None:
    h = t.hour + t.minute / 60
    return next((s for s, (a, b) in WINDOWS.items() if a <= h < b), None)


def collect_runs(folder: Path, school: tuple) -> tuple[dict, list[str]]:
    """{(route, day, session): run} rebuilt from every file, by the clock time of each fix."""
    runs, notes = {}, []
    for p in sorted(folder.rglob("*.gpx")) + sorted(folder.rglob("*.gpx.txt")):
        try:
            f = read_gpx(p)
        except ET.ParseError as e:
            notes.append(f"{p.name}: not readable ({e})"); continue
        if f["route"] is None:
            notes.append(f"{p.name}: no route in the file"); continue
        for kind in ("pts", "taps"):
            for x in f[kind]:
                ses = session_of(x[2])
                if ses is None:
                    continue
                r = runs.setdefault((f["route"], x[2].date().isoformat(), ses),
                                    dict(pts=[], taps=[], files=set(), labels=set(), creators=set()))
                r[kind].append(x)
                r["files"].add(p.name); r["labels"].add(f["label"]); r["creators"].add(f["creator"])
    out = {}
    for k, r in runs.items():
        for kind in ("pts", "taps"):
            seen, u = set(), []
            for x in sorted(r[kind], key=lambda x: x[2]):
                if x[2] not in seen:
                    seen.add(x[2]); u.append(x)
            r[kind] = u
        pts = r["pts"]
        if len(pts) < MIN_RUN_PTS:
            continue
        la = np.array([x[0] for x in pts]); lo = np.array([x[1] for x in pts])
        if dist_arr(la, lo, *school).max() < MIN_RUN_AWAY_M:
            notes.append(f"R{k[0]} {k[1]} {k[2]}: never left school (test, or parked) - ignored")
            continue
        r["files"] = sorted(r["files"]); r["labels"] = sorted(l for l in r["labels"] if l)
        if r["labels"] != [k[2]]:
            notes.append(f"R{k[0]} {k[1]} {k[2]}: file(s) labelled {'/'.join(r['labels'])} - read by clock time")
        out[k] = r
    return out, notes


# --------------------------------------------------------------------------
# One run -> standing spells and taps
# --------------------------------------------------------------------------

def per_second(pts, max_gap=30):
    """Per-second positions; seconds inside a gap longer than max_gap are NaN."""
    t0 = pts[0][2]
    T = np.array([(p[2] - t0).total_seconds() for p in pts])
    la = np.array([p[0] for p in pts]); lo = np.array([p[1] for p in pts])
    sec = np.arange(0, int(T[-1]) + 1)
    LA, LO = np.interp(sec, T, la), np.interp(sec, T, lo)
    idx = np.searchsorted(T, sec)
    gap = (T[np.clip(idx, 0, len(T) - 1)] - T[np.clip(idx - 1, 0, len(T) - 1)]) > max_gap
    LA[gap] = np.nan; LO[gap] = np.nan
    return t0, LA, LO


def _rolling_median(x, w=5):
    h = w // 2
    pad = np.concatenate([np.repeat(x[:1], h), x, np.repeat(x[-1:], h)])
    return np.nanmedian(np.lib.stride_tricks.sliding_window_view(pad, w), axis=1)


def standing_spells(pts):
    """Spells where the bus stood (smoothed speed < V_STOP), short creeps merged."""
    t0, la, lo = per_second(pts)
    ok = ~np.isnan(la)
    sla, slo = la.copy(), lo.copy()
    if ok.sum() > 5:
        sla[ok] = _rolling_median(la[ok]); slo[ok] = _rolling_median(lo[ok])
    n = len(la)
    v = np.full(n, np.nan)
    if n > 4:
        a, b = np.arange(0, n - 4), np.arange(4, n)
        d = 2 * 6371000 * np.arcsin(np.sqrt(np.sin(np.radians(sla[b] - sla[a]) / 2) ** 2 + np.cos(np.radians(sla[a]))
                                            * np.cos(np.radians(sla[b])) * np.sin(np.radians(slo[b] - slo[a]) / 2) ** 2))
        v[2:n - 2] = d / 4.0
    stopped = v < V_STOP
    spells, i = [], 0
    while i < n:
        if stopped[i]:
            j = i
            while j + 1 < n and stopped[j + 1]:
                j += 1
            spells.append([i, j]); i = j + 1
        else:
            i += 1
    merged = []
    for s, e in spells:
        if merged:
            ps, pe = merged[-1]
            c1 = (np.nanmedian(sla[ps:pe + 1]), np.nanmedian(slo[ps:pe + 1]))
            c2 = (np.nanmedian(sla[s:e + 1]), np.nanmedian(slo[s:e + 1]))
            if s - pe <= MERGE_GAP_S and hav_m(*c1, *c2) < MERGE_M:
                merged[-1][1] = e; continue
        merged.append([s, e])
    out = []
    for s, e in merged:
        if e - s + 1 < MIN_SPELL_S:
            continue
        lat, lon = float(np.nanmedian(la[s:e + 1])), float(np.nanmedian(lo[s:e + 1]))
        out.append(dict(kind="spell", t=t0 + dt.timedelta(seconds=int(s)), end=t0 + dt.timedelta(seconds=int(e)),
                        dur=int(e - s + 1), lat=lat, lon=lon, ntap=0))
    return out


def trim(pts, ses, school):
    """Morning: up to arriving at school. Afternoon: from leaving it. (pts, school time)"""
    la = np.array([p[0] for p in pts]); lo = np.array([p[1] for p in pts])
    ds = dist_arr(la, lo, *school)
    far = np.where(ds > SCHOOL_FAR_M)[0]
    if not len(far):
        return pts, None
    near = np.where(ds < SCHOOL_NEAR_M)[0]
    if ses == "am":
        after = near[near > far[0]]
        return (pts[:after[0] + 1], pts[after[0]][2]) if len(after) else (pts, None)
    before = near[near < far[0]]
    return (pts[before[-1]:], pts[before[-1]][2]) if len(before) else (pts, None)


def run_events(run, ses, school):
    """Candidate stops of one run: standing spells with their taps, plus taps made while moving."""
    pts, sch = trim(run["pts"], ses, school)
    spells = standing_spells(pts)
    lo_t, hi_t = pts[0][2] - dt.timedelta(seconds=60), pts[-1][2] + dt.timedelta(seconds=60)
    moving = []
    for lat, lon, t in (x for x in run["taps"] if lo_t <= x[2] <= hi_t):
        best = None
        for e in spells:
            if e["t"] - dt.timedelta(seconds=TAP_SPELL_S) <= t <= e["end"] + dt.timedelta(seconds=TAP_SPELL_S):
                d = hav_m(lat, lon, e["lat"], e["lon"])
                rank = (0 if e["t"] <= t <= e["end"] else 1, d)
                if d < TAP_SPELL_M and (best is None or rank < best[0]):
                    best = (rank, e)
        if best:
            best[1]["ntap"] += 1
        else:
            moving.append(dict(kind="movetap", t=t, end=t, dur=0, lat=lat, lon=lon, ntap=1))
    ev, last_tap = [], None
    for x in sorted(spells + moving, key=lambda x: x["t"]):
        if x["kind"] == "movetap" and last_tap is not None and (x["t"] - last_tap).total_seconds() < REPEAT_TAP_S:
            continue                                     # the same press repeated while moving
        if x["ntap"]:
            last_tap = x["end"]
        if hav_m(x["lat"], x["lon"], *school) > SCHOOL_NEAR_M:
            ev.append(x)
    return ev, sch, pts


# --------------------------------------------------------------------------
# The roster side: every stop of every route, per session
# --------------------------------------------------------------------------

def _grade_kind(g: str) -> str:
    g = ts.clean_text(g)
    m = re.search(r"Grade\D{0,2}(\d{1,2})", g, re.I)
    if m:
        return "senior" if int(m.group(1)) >= 9 else "junior"
    if re.search(r"kinder|nursery", g, re.I):
        return "junior"
    if re.fullmatch(r"primary|secondary|middle|admin", g, re.I):
        return "staff"                                   # teachers and staff ride too (numeric IDs)
    return "unknown"


def roster_riders(path: Path) -> pd.DataFrame:
    """Every rider row, staff included (transport_spine keeps students only)."""
    raw = pd.read_excel(path, header=None, dtype=object)
    rows, cur = [], None
    for _, r in raw.iterrows():
        cells = [ts.clean_text(r.get(i)) for i in range(raw.shape[1])]
        mk = next((c for c in cells if re.match(r"^ROUTE\s*#\s*\d+", c, re.I)), "")
        if mk:
            cur = int(re.search(r"\d+", mk).group()); continue
        if cur is None or not re.fullmatch(r"\d+", cells[0] or "") or len(cells) < 7 or not cells[4]:
            continue
        rows.append(dict(route=cur, kind=_grade_kind(cells[3]), stop=cells[4],
                         pick=ts.parse_time_to_minutes(cells[5]), drop=ts.parse_time_to_minutes(cells[6])))
    return pd.DataFrame(rows)


def pin_class(p: dict | None) -> str:
    if not p or p.get("lat") is None:
        return "none"
    if p.get("verified") or p.get("source") == "override":
        return "verified"
    if p.get("approx") or p.get("source") in ("photon", "nominatim", "arcgis") or len(p.get("agree") or []) <= 1:
        return "weak"
    return "strong"


def stop_lists(inputs: Path, outputs: Path) -> tuple[dict, tuple]:
    """{(route, session): [stop]} in printed order, with riders by kind, the planner's pin and card history."""
    rl = ts.parse_route_list(inputs / "Route List Area Wise with Timings.xlsx")
    rl["route"] = rl.route_key.map(lambda k: int(re.search(r"\d+", k).group()))
    ros = roster_riders(inputs / "Route Wise Student list (for council).xlsx")
    d = json.loads((outputs / "routes_data.json").read_text(encoding="utf-8"))
    cards = {}
    dash = outputs / "dashboard_data.json"
    if dash.exists():
        for r in json.loads(dash.read_text(encoding="utf-8"))["routes"]:
            for ses in ("am", "pm"):
                for s in r[ses]:
                    obs = [o[1] for o in (s.get("obs") or []) if o[1] is not None]
                    if len(obs) >= MIN_CARD_DAYS:
                        cards[(r["route_num"], ses, s["stop"])] = float(np.median(obs)) - CARD_LAG[ses]
    # compare with the pins the planner had BEFORE campaign pins were applied (else a re-run after
    # --apply-pins and a rebuild would measure GPS against itself); new stops are added as first seen
    bp_path = outputs.parent / "cache" / "gps_campaign_basepins.json"
    base = json.loads(bp_path.read_text(encoding="utf-8"))["pins"] if bp_path.exists() else {}
    grew = False
    out = {}
    for r in d["routes"]:
        n = r["num"]
        for ses in ("am", "pm"):
            pins = {}
            for p in r[ses]["points"]:
                k = f"{n}|{ses}|{p['name']}"
                if k not in base and p.get("lat") is not None and not str(p.get("source")) == "override":
                    base[k] = {f: p.get(f) for f in ("lat", "lon", "source", "agree", "verified", "approx")}; grew = True
                pins[p["name"]] = base.get(k, p)
            tcol, rcol = ("promised_pickup_min", "pick") if ses == "am" else ("promised_drop_min", "drop")
            rows = {}
            for x in rl[rl.route == n].itertuples():
                t = getattr(x, tcol)
                rows[x.stop_name] = dict(name=x.stop_name, seq=int(x.stop_seq), printed=None if pd.isna(t) else float(t))
            for st, g in ros[ros.route == n].groupby("stop"):
                row = rows.setdefault(st, dict(name=st, seq=None, printed=None))
                if row["printed"] is None and g[rcol].notna().any():
                    row["printed"] = float(g[rcol].dropna().median())
                for k in ("junior", "senior", "staff", "unknown"):
                    row[k] = int((g.kind == k).sum())
            for row in rows.values():
                for k in ("junior", "senior", "staff", "unknown"):
                    row.setdefault(k, 0)
                p_text, d_text = geo.split_pickup_drop(row["name"])
                row["place"] = p_text if ses == "am" else d_text
                row["key"] = row["name"] if p_text == d_text else f"{row['name']}||{'P' if ses == 'am' else 'D'}"
                pin = pins.get(row["name"])
                row.update(lat=pin["lat"] if pin else None, lon=pin["lon"] if pin else None, pin=pin_class(pin),
                           cards=cards.get((n, ses, row["name"])))
            out[(n, ses)] = sorted(rows.values(), key=lambda x: (x["printed"] if x["printed"] is not None else 9e9,
                                                                  (x["seq"] or 999) * (1 if ses == "am" else -1)))
    if grew:
        bp_path.write_text(json.dumps({"_README": ["The planner's pins before GPS campaign pins were applied."],
                                       "pins": base}, indent=0, ensure_ascii=False), encoding="utf-8")
    return out, (d["school"]["lat"], d["school"]["lon"])


def _norm_place(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def group_places(stops: list[dict]) -> list[dict]:
    """Roster stops that are one place in this session (a P/D stop's pickup is a stop of that name) -> one group."""
    groups = {}
    for p in stops:
        groups.setdefault(_norm_place(p["place"]), []).append(p)
    out = []
    for mem in groups.values():
        lead = max(mem, key=lambda p: (PIN_RANK[p["pin"]], p["junior"] + p["staff"] + p["senior"]))
        g = dict(lead, members=mem)
        g["name"] = " / ".join(sorted({m["name"] for m in mem}, key=len))
        for k in ("junior", "senior", "staff", "unknown"):
            g[k] = sum(m[k] for m in mem)
        t = [m["printed"] for m in mem if m["printed"] is not None]
        c = [m["cards"] for m in mem if m["cards"] is not None]
        g["printed"] = min(t) if t else None
        g["cards"] = min(c) if c else None
        g["senior_only"] = g["senior"] > 0 and g["junior"] + g["staff"] + g["unknown"] == 0
        g["empty"] = g["senior"] + g["junior"] + g["staff"] + g["unknown"] == 0
        out.append(g)
    return out


# --------------------------------------------------------------------------
# Name check (Google Places around the GPS stop)
# --------------------------------------------------------------------------

class NameCheck:
    """
    Places text search for a stop's name, biased to a 250 m circle around the GPS stop. A place named
    like the stop (a distinctive word, not just the area) within 150 m confirms the name. Searches
    are cached, so a re-run costs nothing; without --places only cached answers are used.
    """

    def __init__(self, path: Path, live: bool):
        self.path, self.live = path, live
        self.cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.new = 0

    @staticmethod
    def tokens(s):
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3 and w not in GENERIC and not w.isdigit()}

    @classmethod
    def area_words(cls, places):
        c = Counter(t for n in places for t in cls.tokens(n))
        return {t for t, k in c.items() if k >= 3}

    def lookup(self, q, lat, lon, ask=False):
        k = f"{q}|{lat:.4f},{lon:.4f}"
        if k not in self.cache and ask and self.live:
            import gmaps
            try:
                r = gmaps.places_text_search(q, circle=(lat, lon, 250), page_size=5)
            except gmaps.GoogleUnavailable as e:
                print(f"  name check stopped: {e}", file=sys.stderr); self.live = False; return None
            except gmaps.GoogleRequestError as e:
                print(f"  name check failed for {q!r}: {e}", file=sys.stderr); return None
            self.cache[k] = [{"name": (p.get("displayName") or {}).get("text", ""), "addr": p.get("formattedAddress", ""),
                              "lat": p["location"]["latitude"], "lon": p["location"]["longitude"]}
                             for p in r if p.get("location")]
            self.new += 1
            self.path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=0), encoding="utf-8")
        return self.cache.get(k)

    def verdict(self, q, lat, lon, area, ask=False):
        r = self.lookup(q, lat, lon, ask)
        if r is None:
            return None
        qt = self.tokens(q); own = qt - area
        best = None
        for p in r:
            d = hav_m(lat, lon, p["lat"], p["lon"])
            nt, at = self.tokens(p["name"]), self.tokens(p["addr"])
            # the place's own name must carry the stop's distinctive words (two, when it has two); a name
            # made only of area words (Model Town, Sector 50) cannot be confirmed this way
            strong = bool(own) and len(own & nt) >= min(2, len(own))
            weak = bool(qt & (nt | at))
            rank = (0 if strong else 1 if weak else 2, d)
            if best is None or rank < best[0]:
                best = (rank, p, d, strong, weak)
        if best is None:
            return {"v": "no result", "d": None, "hit": None}
        _, p, d, strong, weak = best
        v = ("confirmed" if d <= 150 else "nearby" if d <= 400 else "far") if strong else \
            ("area" if weak and d <= 300 else "no match")
        return {"v": v, "d": round(d), "hit": p["name"]}


# --------------------------------------------------------------------------
# Matching places to roster stops
# --------------------------------------------------------------------------

def _prox(d, cls):
    if d is None or cls == "none":
        return 0.0
    for lim, sc in PROX[cls]:
        if d <= lim:
            return sc
    return PROX_FAR[cls]


def _cluster(events):
    """Greedy: the strongest events (tapped, long) seed places; others join the nearest within SITE_M."""
    sites = []
    for e in sorted(events, key=lambda e: (-e["ntap"], -e["dur"])):
        best = min(((hav_m(e["lat"], e["lon"], s["lat"], s["lon"]), s) for s in sites), default=None, key=lambda x: x[0])
        if best and best[0] < SITE_M:
            best[1]["members"].append(e)
        else:
            sites.append(dict(lat=e["lat"], lon=e["lon"], members=[e]))
    return sites


def _site_stats(s, nr):
    mem = s["members"]
    s["days"] = sorted({m["day"] for m in mem})
    s["tapdays"] = sorted({m["day"] for m in mem if m["ntap"]})
    s["byday"] = {}
    for m in sorted(mem, key=lambda m: (-m["ntap"], -m["dur"])):
        s["byday"].setdefault(m["day"], m)
    sp = [m for m in mem if m["kind"] == "spell"]
    s["dur"] = float(np.median([m["dur"] for m in sp])) if sp else 0.0
    s["like"] = 1.5 * len(s["tapdays"]) / nr + 1.0 * len(s["days"]) / nr + (0.5 if s["dur"] >= 25 else 0)
    pos = [m for m in sp if m["ntap"]] or sp or mem
    s["lat"] = float(np.median([m["lat"] for m in pos])); s["lon"] = float(np.median([m["lon"] for m in pos]))
    s["spread"] = max(hav_m(m["lat"], m["lon"], s["lat"], s["lon"]) for m in pos)
    return s


def _dp(order, seq, score):
    """Best one-to-one, order-keeping pairing of places (time order) with stops (printed order)."""
    S, N = len(order), len(seq)
    sc = np.full((S, N), -np.inf)
    for a, s in enumerate(order):
        for b, p in enumerate(seq):
            v = score(s, p)
            if v > MATCH_MIN:
                sc[a, b] = v
    best = np.zeros((S + 1, N + 1)); back = {}
    for a in range(1, S + 1):
        for b in range(1, N + 1):
            opts = [(best[a - 1, b], 0), (best[a, b - 1], 1)]
            if sc[a - 1, b - 1] > -np.inf:
                opts.append((best[a - 1, b - 1] + sc[a - 1, b - 1], 2))
            best[a, b], back[(a, b)] = max(opts, key=lambda o: o[0])
    a, b, pairs = S, N, []
    while a > 0 and b > 0:
        k = back[(a, b)]
        if k == 2:
            pairs.append((a - 1, b - 1)); a -= 1; b -= 1
        elif k == 0:
            a -= 1
        else:
            b -= 1
    return pairs[::-1], best[S, N]


def match_route(route, ses, runs, stops, school, no_seniors, names: NameCheck, net_area=frozenset()):
    days = sorted(d for (r, d, s) in runs if r == route and s == ses)
    if not days:
        return None
    nr = len(days)
    groups = group_places(stops)
    area = NameCheck.area_words([m["place"] for g in groups for m in g["members"]]) | set(net_area)
    perday, events = {}, []
    for d in days:
        ev, sch, pts = run_events(runs[(route, d, ses)], ses, school)
        r = runs[(route, d, ses)]
        perday[d] = dict(school=sch, start=pts[0][2], end=pts[-1][2], pts=pts, events=ev, files=r["files"],
                         labels=r["labels"], taps=len(r["taps"]))
        for x in ev:
            x["day"] = d
            if x["kind"] == "movetap" or x["ntap"] or x["dur"] >= KEEP_SPELL_S:
                events.append(x)
    sites = [_site_stats(s, nr) for s in _cluster(events)]
    sites = [s for s in sites if s["tapdays"] or len(s["days"]) >= 2]
    off = {d: 0.0 for d in days}
    early = [d for d in days if d in no_seniors]

    def t_of(s):
        return float(np.median([mins(m["t"]) - off[d] for d, m in s["byday"].items()]))

    def namev(s, g, ask=False):
        best = None
        for m in g["members"]:
            v = names.verdict(m["place"], s["lat"], s["lon"], area, ask)
            if v and (best is None or NAME_SCORE.get(v["v"], 0) > NAME_SCORE.get(best["v"], 0)):
                best = dict(v, member=m["name"])
        return best

    def score(s, g):
        dd = hav_m(s["lat"], s["lon"], g["lat"], g["lon"]) if g["lat"] is not None else None
        v = s["like"] + _prox(dd, g["pin"])
        t = t_of(s)
        if g["printed"] is None:
            v -= 0.5
        else:
            dl = abs(t - g["printed"])
            v += 0.0 if dl <= TOL[ses] else max(-0.3 * (dl - TOL[ses]), -4.0)
            if dd is None and dl > TOL[ses] + 1:
                v -= 2
        if g["cards"] is not None:
            dc = abs(t - g["cards"])
            v += 0.6 if dc <= 3 else 0.3 if dc <= 6 else 0.0
        if g["senior_only"]:                            # Grade 9-12 may still have come in (exams), so only a nudge
            hit = [d for d in s["tapdays"] if d in early]
            if hit:
                v -= 0.4 * len(hit) / max(1, len(early))
            elif early and all(d not in early for d in s["days"]):
                v += 0.6
        if g["empty"]:
            v -= 0.4
        nv = namev(s, g)
        if nv:
            v += NAME_SCORE.get(nv["v"], 0.0)
        return v

    def printed_order():
        """Printed order; stops sharing a printed minute go in the order of their best place."""
        seq = []
        for _, grp in itertools.groupby(sorted(groups, key=lambda g: g["printed"] if g["printed"] is not None else 9e9),
                                        key=lambda g: g["printed"]):
            grp = list(grp)
            if len(grp) > 1:
                def best_t(g):
                    c = [(score(s, g), t_of(s)) for s in sites]
                    c = [x for x in c if x[0] > MATCH_MIN]
                    return max(c)[1] if c else 9e9
                grp.sort(key=best_t)
            seq.extend(grp)
        return seq

    # each run's clock offset vs the printed times: the best-scoring shift, run by run
    for d in days:
        one = [_site_stats(dict(lat=x["lat"], lon=x["lon"], members=[x]), 1)
               for x in perday[d]["events"] if x["kind"] == "movetap" or x["ntap"] or x["dur"] >= KEEP_SPELL_S]
        if len(one) < 2:
            continue
        seq = sorted(groups, key=lambda g: g["printed"] if g["printed"] is not None else 9e9)
        best = (0.0, -1.0)
        for o in range(OFFSET_SEARCH[ses][0], OFFSET_SEARCH[ses][1] + 1):
            off[d] = float(o)
            _, v = _dp(sorted(one, key=t_of), seq, score)
            if v > best[1] + 1e-9 or (abs(v - best[1]) < 1e-9 and abs(o) < abs(best[0])):
                best = (float(o), v)
        off[d] = best[0]
    searched = dict(off)
    for _ in range(3):                                   # refine offsets on confident pairs
        order, seq = sorted(sites, key=t_of), printed_order()
        pairs, _ = _dp(order, seq, score)
        good = [(order[a], seq[b]) for a, b in pairs if seq[b]["printed"] is not None and seq[b]["lat"] is not None
                and hav_m(order[a]["lat"], order[a]["lon"], seq[b]["lat"], seq[b]["lon"]) < 300
                and len(order[a]["tapdays"]) + len(order[a]["days"]) >= 3]
        for d in days:
            r_ = [mins(s["byday"][d]["t"]) - g["printed"] for s, g in good if d in s["byday"]]
            if len(r_) >= 2:
                off[d] = float(np.clip(np.median(r_), searched[d] - 3, searched[d] + 3))
    order, seq = sorted(sites, key=t_of), printed_order()
    pairs, _ = _dp(order, seq, score)
    match = {id(seq[b]): order[a] for a, b in pairs}
    used = {id(s) for s in match.values()}
    for s in list(match.values()):                       # same stop, stood a little apart on other days
        for o in sites:
            if id(o) in used or hav_m(o["lat"], o["lon"], s["lat"], s["lon"]) > ABSORB_M:
                continue
            if set(o["days"]) & set(s["days"]) or abs(t_of(o) - t_of(s)) > 4:
                continue
            used.add(id(o))
            s["members"] += o["members"]
            for d, m in o["byday"].items():
                s["byday"].setdefault(d, m)
            s["days"] = sorted(set(s["days"]) | set(o["days"])); s["tapdays"] = sorted(set(s["tapdays"]) | set(o["tapdays"]))
    out = []
    for g in seq:
        s = match.get(id(g))
        row = dict(g, site=s)
        if s is not None:
            row.update(d_pin=hav_m(s["lat"], s["lon"], g["lat"], g["lon"]) if g["lat"] is not None else None,
                       t_site=t_of(s), dt_printed=(t_of(s) - g["printed"]) if g["printed"] is not None else None,
                       namev=namev(s, g, ask=True) if names.live else namev(s, g))
        cov = []
        for d in days:
            pts = perday[d]["pts"]
            exp = (g["printed"] + off[d]) if g["printed"] is not None else None
            near = None
            if g["lat"] is not None:
                near = float(dist_arr(np.array([x[0] for x in pts]), np.array([x[1] for x in pts]), g["lat"], g["lon"]).min())
            cov.append(dict(day=d, in_time=exp is not None and mins(pts[0][2]) - 2 <= exp <= mins(pts[-1][2]) + 2,
                            near_pin=near))
        row["cover"] = cov
        out.append(row)
    unnamed = [s for s in sites if id(s) not in used and (len(s["tapdays"]) >= 2 or (len(s["days"]) >= 3 and s["dur"] >= 25))]
    for s in unnamed:
        s["t_site"] = t_of(s)                            # on the printed clock (for pairing with stops)
        s["t_raw"] = float(np.median([mins(m["t"]) for m in s["byday"].values()]))   # what the bus did
        if names.live:                                   # does a doubtful / missing stop's name sit here?
            for g in out:
                if g["printed"] is not None and abs(g["printed"] - s["t_site"]) <= 12 and \
                        (g["site"] is None or g.get("name_conf") != "high"):
                    namev(s, g, ask=True)
    return dict(route=route, session=ses, days=days, perday=perday, offsets=off, stops=out, unnamed=unnamed,
                no_seniors_days=early)


def grade(res):
    """Confidence that a bus stops here (location) and that this is the roster stop (name), with reasons."""
    ses, early = res["session"], res["no_seniors_days"]
    for g in res["stops"]:
        s, flags = g["site"], []
        if s is None:
            g["loc_conf"] = g["name_conf"] = None
            if g["senior_only"]:
                g["status"] = ("not seen: only Grade 9-12 riders, not on board " + ", ".join(d[5:] for d in early)
                               if all(c["day"] in early for c in g["cover"]) or early else "not seen: only Grade 9-12 riders")
            elif g["empty"]:
                g["status"] = "not seen: nobody on the student list rides from here"
            elif not any(c["in_time"] for c in g["cover"]):
                g["status"] = "not seen: outside the logged part of every run"
            else:
                n_in = sum(c["in_time"] for c in g["cover"])
                passed = sum(1 for c in g["cover"] if c["near_pin"] is not None and c["near_pin"] < 150)
                g["status"] = (f"NOT SEEN on {n_in} run(s) covering its time"
                               + (f"; the bus passed within 150 m of its pin on {passed}" if g["lat"] is not None else "; no pin"))
            g["flags"] = []
            continue
        # where the bus STOOD counts; presses made while moving (start of run, after pulling away) do not
        stood = {m["day"] for m in s["members"] if m["kind"] == "spell"}
        nt = len({m["day"] for m in s["members"] if m["kind"] == "spell" and m["ntap"]})
        ns = len(stood)
        loc = "high" if (ns >= 2 and nt >= 1) or (ns >= 3 and s["dur"] >= 20) else \
              "medium" if ns >= 2 or (ns == 1 and len(s["tapdays"]) >= 2) else "low"
        if loc == "low":
            flags.append(f"bus stood here on {ns} run(s), taps on {len(s['tapdays'])}"
                         + (" (all pressed while moving)" if ns == 0 else ""))
        if s["spread"] > 35:
            flags.append(f"the bus stood up to {s['spread']:.0f} m apart on different days")
        d, cls = g["d_pin"], g["pin"]
        tdev = abs(g["dt_printed"]) if g["dt_printed"] is not None else None
        t_ok = tdev is not None and tdev <= TOL[ses] + 1
        if d is not None and ((cls == "strong" and d <= 150) or (cls in ("weak", "verified") and d <= 100)) and t_ok:
            name = "high"
        elif d is not None and d <= 400 and (t_ok or tdev is None):
            name = "medium"
        elif d is None and t_ok:
            name = "medium"; flags.append("no pin to compare: named by printed order and time")
        else:
            name = "low"
        if d is not None and d > 400:
            flags.append(f"{d:.0f} m from the planner's pin")
        if tdev is not None and not t_ok:
            flags.append(f"{g['dt_printed']:+.0f} min from the printed time")
        nv = g.get("namev")
        if nv and nv["v"] == "confirmed" and (tdev is None or tdev <= 8):
            name = "high"
            g["name_note"] = f"Google: '{nv['hit']}' {nv['d']} m away"
        elif nv and nv["v"] == "far":
            if name == "medium" and d is not None and d > 400:
                name = "low"
            flags.append(f"Google puts the name {nv['d']} m away")
        if g["senior_only"] and [x for x in s["tapdays"] if x in early]:
            g["name_note"] = ((g.get("name_note") or "") + "; " if g.get("name_note") else "") +                 "only Grade 9-12 listed here; they may have come in for exams on " + ", ".join(x[5:] for x in s["tapdays"] if x in early)
        if g["empty"]:
            flags.append("nobody on the student list rides from here")
        g.update(loc_conf=loc, name_conf=name, flags=flags, status="matched")


def suggest(res):
    """Pair each unlisted GPS stop with the missing or doubtful roster stops due at about that time."""
    win = 8 if res["session"] == "am" else 10
    for u in res["unnamed"]:
        u["maybe"] = [g["name"] for g in res["stops"] if g["printed"] is not None and abs(g["printed"] - u["t_site"]) <= win
                      and (g["site"] is None or g["name_conf"] == "low") and not g["empty"]]
    for g in res["stops"]:
        if g["site"] is None and g["status"].startswith("NOT SEEN"):
            near = [u for u in res["unnamed"] if g["name"] in u["maybe"]]
            if near:
                u = min(near, key=lambda u: abs(u["t_site"] - g["printed"]))
                g["status"] += f"; probably the unlisted GPS stop at {hm(u['t_raw'])} ({u['lat']:.5f}, {u['lon']:.5f})"


def stop_times(res):
    """Per matched stop: arrival / departure / dwell on every run, and their medians."""
    for g in res["stops"]:
        s = g["site"]
        if s is None:
            continue
        rows = []
        for d in res["days"]:
            m = s["byday"].get(d)
            if m:
                rows.append(dict(day=d, arr=round(mins(m["t"]), 2), dep=round(mins(m["end"]), 2), dwell_s=m["dur"],
                                 tapped=bool(m["ntap"]), lat=round(m["lat"], 6), lon=round(m["lon"], 6)))
        g["obs"] = rows
        arr = [r["arr"] for r in rows]
        g["arr_med"] = float(np.median(arr)) if arr else None
        dw = [r["dwell_s"] for r in rows if r["dwell_s"]]
        g["dwell_med"] = float(np.median(dw)) if dw else None


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

TRACK_STEP_S = 15         # the representative track keeps one fix this often


def representative_track(r) -> dict | None:
    """
    One run's track, thinned, on the printed clock (each fix's minute minus that run's offset):
    the run that saw the most matched stops, ordinary-dismissal days first in the afternoon.
    The network optimiser uses it to time a stop whose pin is off the bus's real path.
    """
    seen = {d: sum(1 for g in r["stops"] if g["site"] is not None and d in g["site"]["byday"]) for d in r["days"]}
    normal = {d for d in r["days"] if r["session"] == "am" or (r["perday"][d]["school"] is not None
                                                               and r["perday"][d]["school"].hour * 60
                                                               + r["perday"][d]["school"].minute >= 14 * 60 + 45)}
    if not seen:
        return None
    day = max(r["days"], key=lambda d: (d in normal, seen[d], len(r["perday"][d]["pts"])))
    pts, last, out = r["perday"][day]["pts"], None, []
    for p in pts:
        if last is None or (p[2] - last).total_seconds() >= TRACK_STEP_S:
            out.append([round(p[0], 5), round(p[1], 5), round(mins(p[2]) - r["offsets"][day], 2)])
            last = p[2]
    return {"day": day, "points": out}


def to_json(results, notes, folder):
    out = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "source": str(folder), "notes": notes, "routes": []}
    for (route, ses), r in sorted(results.items()):
        runs = [dict(day=d, logged=[f"{v['start']:%H:%M:%S}", f"{v['end']:%H:%M:%S}"], files=v["files"],
                     labels=v["labels"], taps=v["taps"], offset_min=round(r["offsets"][d], 1),
                     school=v["school"].strftime("%H:%M:%S") if v["school"] else None)
                for d, v in r["perday"].items()]
        stops = []
        for g in r["stops"]:
            s = g["site"]
            stops.append(dict(
                name=g["name"], members=[dict(name=m["name"], key=m["key"], place=m["place"], printed=m["printed"],
                                              junior=m["junior"], senior=m["senior"], staff=m["staff"]) for m in g["members"]],
                printed=g["printed"], riders=dict(junior=g["junior"], senior=g["senior"], staff=g["staff"], unknown=g["unknown"]),
                senior_only=g["senior_only"], pin=g["pin"], pin_ll=[g["lat"], g["lon"]] if g["lat"] is not None else None,
                status=g["status"], loc_conf=g["loc_conf"], name_conf=g["name_conf"], flags=g["flags"],
                name_note=g.get("name_note"), google=g.get("namev"),
                lat=round(s["lat"], 6) if s else None, lon=round(s["lon"], 6) if s else None,
                d_pin_m=None if not s or g["d_pin"] is None else round(g["d_pin"]),
                days_seen=s["days"] if s else [], days_tapped=s["tapdays"] if s else [],
                spread_m=round(s["spread"]) if s else None, arr_med=g.get("arr_med"), dwell_med_s=g.get("dwell_med"),
                dt_printed_min=None if not s or g["dt_printed"] is None else round(g["dt_printed"], 1), obs=g.get("obs", [])))
        unnamed = [dict(lat=round(s["lat"], 6), lon=round(s["lon"], 6), days_seen=s["days"], days_tapped=s["tapdays"],
                        t_med=round(s["t_raw"], 1), dwell_s=round(s["dur"]), maybe=s.get("maybe", []),
                        obs=[dict(day=d, arr=round(mins(m["t"]), 2), dep=round(mins(m["end"]), 2), dwell_s=m["dur"])
                             for d, m in sorted(s["byday"].items())]) for s in r["unnamed"]]
        out["routes"].append(dict(route=route, session=ses, runs=runs, stops=stops, unnamed=unnamed,
                                  track=representative_track(r)))
    return out


def write_csv(data, path: Path):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["route", "session", "stop", "students (Gr 1-8 / Gr 9-12)", "staff", "printed", "GPS time (median)",
                    "per-day times", "dwell (s)", "status", "name confidence", "location confidence", "what to check",
                    "lat", "lon", "map", "metres from old pin"])
        for r in data["routes"]:
            for s in r["stops"]:
                per = ", ".join(f"{o['day'][5:]} {hm(o['arr'])}" for o in s["obs"])
                link = f"https://www.google.com/maps/search/?api=1&query={s['lat']},{s['lon']}" if s["lat"] is not None else ""
                for m in s["members"]:
                    w.writerow([r["route"], r["session"], m["name"], f"{m['junior']} / {m['senior']}", m["staff"],
                                hm(m["printed"]), hm(s["arr_med"]), per, "" if s["dwell_med_s"] is None else round(s["dwell_med_s"]),
                                s["status"], s["name_conf"] or "", s["loc_conf"] or "", "; ".join(s["flags"]),
                                s["lat"] or "", s["lon"] or "", link, "" if s["d_pin_m"] is None else s["d_pin_m"]])
            for u in r["unnamed"]:
                w.writerow([r["route"], r["session"], "(GPS stop not on the list)", "", "", "", hm(u["t_med"]),
                            ", ".join(f"{o['day'][5:]} {hm(o['arr'])}" for o in u["obs"]), u["dwell_s"],
                            f"bus stood here on {len(u['days_seen'])} run(s), tapped on {len(u['days_tapped'])}", "", "",
                            ("maybe: " + " / ".join(u["maybe"])) if u["maybe"] else "",
                            u["lat"], u["lon"], f"https://www.google.com/maps/search/?api=1&query={u['lat']},{u['lon']}", ""])


def write_html(results, data, path: Path):
    import gmaps
    gmaps.load_env()
    view = []
    for (route, ses), r in sorted(results.items()):
        tracks = []
        for d in r["days"]:
            keep, last = [], None
            for p in r["perday"][d]["pts"]:
                if last is None or (p[2] - last).total_seconds() >= 4:
                    keep.append([round(p[0], 5), round(p[1], 5)]); last = p[2]
            tracks.append({"day": d[5:], "pts": keep})
        jr = next(x for x in data["routes"] if x["route"] == route and x["session"] == ses)
        view.append({"key": f"R{route} {'morning' if ses == 'am' else 'afternoon'}", "tracks": tracks, "runs": jr["runs"],
                     "stops": jr["stops"], "unnamed": jr["unnamed"]})
    tpl = (Path(__file__).resolve().parent / "gps_check_template.html").read_text(encoding="utf-8")
    path.write_text(tpl.replace("__GMAP_TAGS__", gmaps.browser_map_tags("light"))
                    .replace("__DATA__", json.dumps(view, separators=(",", ":")).replace("</", "<\\/")), encoding="utf-8")


CHANGE_MIN = 3             # a printed time this far from the GPS time gets a new time ...
CHANGE_RUNS = 2            # ... when the bus was seen there on at least this many runs


def timetable(data, normal_days=None) -> list[dict]:
    """
    New pickup / drop times. A stop keeps its printed time unless the bus is CHANGE_MIN or more
    minutes off it; then it gets the typical GPS time (morning: median arrival, rounded down so no
    child misses the bus; afternoon: median arrival on an ordinary dismissal, rounded). Only stops
    whose name is confirmed are changed - a time put on the wrong stop is worse than an old one.
    Afternoon runs are lined up by each run's clock offset, then set to the route's offset on
    ordinary days (an early dismissal like 22 Oct would otherwise drag every time earlier).
    """
    rows = []
    for r in data["routes"]:
        ses = r["session"]
        off = {x["day"]: x["offset_min"] for x in r["runs"]}
        norm = [off[d] for d in off if normal_days is None or d in normal_days]
        ref = float(np.median(norm)) if (ses == "pm" and norm) else 0.0
        for st in r["stops"]:
            obs = [o for o in st["obs"] if o["dwell_s"]] or st["obs"]
            gps = None
            if obs:
                gps = float(np.median([o["arr"] - (off[o["day"]] - ref if ses == "pm" else 0) for o in obs]))
            for m in st["members"]:
                pr = m["printed"]
                new, why = pr, ""
                if gps is None:
                    why = "not seen on GPS - keep" if st["status"] != "matched" else ""
                elif ses == "pm" and not norm:
                    why = "only early-dismissal runs logged - keep"
                elif st["name_conf"] != "high":
                    why = f"GPS {hm(gps)}, but confirm the stop before changing"
                elif len(obs) < CHANGE_RUNS:
                    why = f"GPS {hm(gps)} on one run only - keep until seen again"
                elif pr is None:
                    new, why = (math.floor(gps) if ses == "am" else round(gps)), "no printed time"
                elif abs(gps - pr) >= CHANGE_MIN:
                    new = math.floor(gps) if ses == "am" else round(gps)
                    why = f"bus is {gps - pr:+.0f} min off the printed time"
                rows.append(dict(route=r["route"], session=ses, stop=m["name"], printed=pr, gps=gps, new=new,
                                 changed=new is not None and pr is not None and round(new) != round(pr), note=why,
                                 runs=len(st["obs"]), name_conf=st["name_conf"]))
    return rows


def write_timetable(rows, path: Path):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["route", "session", "stop", "printed", "GPS typical", "new time", "changed", "runs seen", "note"])
        for x in sorted(rows, key=lambda x: (x["route"], x["session"], x["new"] if x["new"] is not None else 9e9)):
            w.writerow([x["route"], "morning" if x["session"] == "am" else "afternoon", x["stop"], hm(x["printed"]),
                        hm(x["gps"]), hm(x["new"]), "yes" if x["changed"] else "", x["runs"], x["note"]])


def apply_pins(data, cache_dir: Path) -> tuple[int, list[str]]:
    """
    Stops whose position (bus stood there on 2+ runs) and name (pin/time/Google agree) are both
    confirmed become verified pins. A plain stop name is one pin for both sessions: the morning
    position wins; afternoon only when the morning was not confirmed.
    """
    ov_path = cache_dir / "geocode_overrides.json"
    raw = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    picked, notes = {}, []
    for r in sorted(data["routes"], key=lambda r: r["session"]):          # "am" before "pm"
        for s in r["stops"]:
            if s["name_conf"] != "high" or s["loc_conf"] not in ("high", "medium"):
                continue
            for m in s["members"]:
                k = m["key"]
                if k in picked:
                    a = picked[k]
                    if hav_m(a["lat"], a["lon"], s["lat"], s["lon"]) > 100:
                        notes.append(f"{k}: morning and afternoon GPS stops {hav_m(a['lat'], a['lon'], s['lat'], s['lon']):.0f} m apart - kept the morning one")
                    continue
                picked[k] = dict(lat=s["lat"], lon=s["lon"], route=r["route"], ses=r["session"],
                                 runs=len(s["days_seen"]), days=s["days_seen"])
    for k in [k for k, v in raw.items() if isinstance(v, dict) and str(v.get("note", "")).startswith("GPS campaign")
              and k not in picked]:
        del raw[k]                                       # an earlier campaign pin this run no longer confirms
        notes.append(f"{k}: earlier campaign pin removed (no longer confirmed)")
    n = 0
    for k, p in picked.items():
        cur = raw.get(k)
        if cur and not str(cur.get("note", "")).startswith("GPS"):
            notes.append(f"{k}: kept the hand-placed pin"); continue
        raw[k] = {"lat": p["lat"], "lon": p["lon"], "label": k.split("||")[0],
                  "note": f"GPS campaign: bus stood here on {p['runs']} run(s) of Route {p['route']} "
                          f"{p['ses'].upper()} ({', '.join(d[5:] for d in p['days'])} 2026)",
                  "gps_runs": p["runs"]}
        n += 1
    if ov_path.exists():
        shutil.copy2(ov_path, ov_path.with_suffix(f".backup-{dt.datetime.now():%Y%m%d-%H%M%S}.json"))
    ov_path.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    return n, notes


def as_gps_runs(cache_dir: Path) -> list[dict]:
    """
    Every campaign run in gps_runs.py's shape (stops in driving order with arrive/leave minutes), so
    build_routes.py can calibrate Google's times on them. Places the bus stood that are not on the
    list are kept as stops too: leaving one out would add its standing time to a driving leg.
    """
    p = Path(cache_dir) / "gps_campaign.json"
    if not p.exists():
        return []
    out = []
    for r in json.loads(p.read_text(encoding="utf-8"))["routes"]:
        for run in r["runs"]:
            day, st = run["day"], []
            for s in r["stops"]:
                for o in (o for o in s["obs"] if o["day"] == day):
                    st.append(dict(name=s["name"], lat=o["lat"], lon=o["lon"], printed=s["printed"], arr_min=o["arr"],
                                   dep_min=o["dep"], dwell_s=o["dwell_s"], moving_at_tap=not o["dwell_s"]))
            for u in r["unnamed"]:
                for o in (o for o in u["obs"] if o["day"] == day):
                    st.append(dict(name="(not on the list)", lat=u["lat"], lon=u["lon"], printed=None, arr_min=o["arr"],
                                   dep_min=o["dep"], dwell_s=o["dwell_s"], moving_at_tap=not o["dwell_s"]))
            st.sort(key=lambda x: x["arr_min"])
            sch = None
            if run["school"]:
                h, m_, sec = map(int, run["school"].split(":"))
                t = h * 60 + m_ + sec / 60
                sch = {"arr_min": t, "dep_min": None} if r["session"] == "am" else {"arr_min": None, "dep_min": t}
            out.append(dict(file=f"campaign R{r['route']} {r['session']} {day}", route=f"Route {r['route']}",
                            num=r["route"], session=r["session"], date=day, stops=st, school=sch))
    return out


def run(folder: Path, no_seniors: set[str], places: bool, inputs: Path, outputs: Path, cache_dir: Path):
    stops, school = stop_lists(inputs, outputs)
    runs, notes = collect_runs(folder, school)
    names = NameCheck(cache_dir / "gps_namecheck.json", live=places)
    if places:
        import gmaps
        gmaps.load_env()
    # words in 4+ stop names across the network are localities (Vihar, Jaypee, Mayur), not a stop's own name
    net_area = frozenset(t for t, k in Counter(t for n in {p["place"] for v in stops.values() for p in v}
                                              for t in NameCheck.tokens(n)).items() if k >= 4)
    results = {}
    for passno in (1, 2) if places else (1,):            # pass 2 re-matches with the names pass 1 looked up
        results = {}
        for key in sorted(stops):
            r = match_route(*key, runs, stops[key], school, no_seniors, names, net_area)
            if r:
                grade(r); suggest(r); stop_times(r); results[key] = r
    for k in sorted({(r, s) for (r, d, s) in runs} - set(results)):
        notes.append(f"R{k[0]} {k[1]}: GPS runs but the route is not in the planner")
    return results, notes, names


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="folder with the tracker's .gpx files (any sub-folders)")
    ap.add_argument("--no-seniors", default="", help="dates (YYYY-MM-DD,...) when Grade 9-12 were not on the buses")
    ap.add_argument("--places", action="store_true", help="check stop names with Google Places (cached, free-tier capped)")
    ap.add_argument("--apply-pins", action="store_true", help="write confirmed stop positions into geocode_overrides.json")
    ap.add_argument("--inputs", default=str(ROOT / "inputs"))
    ap.add_argument("--outputs", default=str(ROOT / "outputs"))
    ap.add_argument("--cache", default=str(ROOT / "cache"))
    a = ap.parse_args()
    results, notes, names = run(Path(a.folder), {d.strip() for d in a.no_seniors.split(",") if d.strip()},
                                a.places, Path(a.inputs), Path(a.outputs), Path(a.cache))
    data = to_json(results, notes, a.folder)
    Path(a.cache, "gps_campaign.json").write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    write_csv(data, Path(a.outputs, "gps_stops.csv"))
    tt = timetable(data, normal_days={x["day"] for r in data["routes"] for x in r["runs"]
                                      if r["session"] == "pm" and x["school"] and x["school"] >= "14:45"} or None)
    write_timetable(tt, Path(a.outputs, "gps_timetable.csv"))
    write_html(results, data, Path(a.outputs, "gps_check.html"))
    allst = [s for r in data["routes"] for s in r["stops"]]
    c = Counter((s["name_conf"] or s["status"].split(":")[0].split(" on ")[0]) for s in allst)
    print(f"{len({(r['route']) for r in data['routes']})} routes, {sum(len(r['runs']) for r in data['routes'])} runs, "
          f"{len(allst)} stops: " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
    ch = [x for x in tt if x["changed"]]
    print(f"timetable: {len(ch)} of {len(tt)} stop times change "
          f"({sum(x['session'] == 'am' for x in ch)} morning, {sum(x['session'] == 'pm' for x in ch)} afternoon)")
    print(f"{sum(len(r['unnamed']) for r in data['routes'])} recurring GPS stops not on any list; "
          f"{names.new} new Google name searches")
    for n in notes:
        print("  note:", n)
    if a.apply_pins:
        n, pin_notes = apply_pins(data, Path(a.cache))
        print(f"\n{n} confirmed stop positions written to geocode_overrides.json (rebuild: python pipeline/build_routes.py)")
        for x in pin_notes:
            print("  ", x)
    print("Check page: python pipeline/serve.py, then http://localhost:8777/gps_check.html")
