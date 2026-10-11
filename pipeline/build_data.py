"""
Builds dashboard_data.json for the Prometheus School transport dashboard.

Sits on top of the validated parsers in transport_spine.py. Nothing here
re-parses the source workbooks.

THE ONE RULE THAT SHAPES THIS FILE
----------------------------------
The morning boarding tap is recorded after the bus has left the stop, so every
morning observation is late by an unknown lag L. L has never been measured and
its plausible range (0-8 min) inverts the entire picture: at L=0 fifty stops
look systematically late, at L=6 forty-seven look systematically early.

Therefore this module ships RAW per-stop observation arrays to the browser and
classifies NOTHING in the morning. Every morning verdict - late, early,
on time, feasible, infeasible - is recomputed in JavaScript at the reader's
chosen L. Baking a classification in here would hardcode an unmeasured
constant into the findings.

Afternoon legs (leaving school, drop-off) are bias-free and are summarised
normally.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import geo
import transport_spine as ts

# --------------------------------------------------------------------------
# Arbitrary constants. Every one of these is a judgement call, is labelled as
# such in the interface, and is listed in the README with its sensitivity.
# --------------------------------------------------------------------------

MIN_OBS = 15                 # below this a stop is shown but never classified
LATE_THRESHOLD = 5           # minutes; |median - L| beyond this is "running late/early"
TIGHT_IQR = 8                # minutes; a verdict needs spread at or below this
UNPREDICTABLE_IQR = 12       # minutes; above this the stop is "unpredictable"
ONTIME_BAND = 5              # minutes; +/- this counts as on time for the on-time rate
LOW_COVERAGE = 0.70          # leg coverage below this triggers the suspect-data banner
ORDER_VIOLATION_RATE = 0.20  # share of days out of sequence that triggers the warning
RATIO_LOW, RATIO_HIGH = 0.40, 2.50   # observed vs road time disagreement band
L_MIN, L_MAX, L_DEFAULT = 0, 10, 3   # scan-lag slider

# Plausibility window for an observed afternoon leg between consecutive stops,
# in minutes. Outside this the pair is not used for congestion calibration.
LEG_MIN_MIN, LEG_MAX_MIN = 0.5, 90.0

LEG_LABEL = {
    "am_pickup": "Morning boarding",
    "am_school_arrival": "Morning school arrival",
    "pm_school_board": "Afternoon leaving school",
    "pm_dropoff": "Afternoon drop-off",
}
LEG_BIAS = {
    "am_pickup": "Biased late by L - tap happens after the bus has left the stop",
    "am_school_arrival": "Unknown - reader technology unconfirmed",
    "pm_school_board": "Low",
    "pm_dropoff": "Low - tap happens as the child steps off the bus",
}


def _f(v):
    """JSON-safe float."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else round(v, 3)


def _i(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else int(v)


def json_safe(o):
    """
    Recursively replace NaN/Inf with null.

    Building a table through a DataFrame silently converts None to NaN in any
    float column, and json.dumps emits a bare NaN token that JSON.parse rejects -
    which takes the whole dashboard down with a syntax error rather than a
    missing value. Everything is passed through here, and the writers use
    allow_nan=False so a leak fails loudly at build time instead of in a browser.
    """
    if isinstance(o, dict):
        return {k: json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [json_safe(v) for v in o]
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if o is None or isinstance(o, (str, bool, int)):
        return o
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if o is pd.NaT or (isinstance(o, float) and pd.isna(o)):
        return None
    return str(o)


def pct(num, den):
    return None if not den else round(100.0 * num / den, 1)


def quantile(values, q):
    return None if not len(values) else _f(np.quantile(values, q))


# --------------------------------------------------------------------------
# Stop universe
# --------------------------------------------------------------------------

def build_stop_universe(routes: pd.DataFrame, students: pd.DataFrame) -> pd.DataFrame:
    """
    Every (route, stop) pair the school has on paper.

    Union of the master route list and the roster, because the two disagree:
    the route list carries printed stops that currently have no children, and
    the roster carries a handful of drop-only points the route list omits.
    Both matter - the first is evidence about network sprawl, the second would
    leave holes in the afternoon map.
    """
    rl = routes[["route_key", "stop_name", "stop_seq",
                 "promised_pickup_min", "promised_drop_min"]].copy()
    rl["in_route_list"] = True

    def roster_side(rk_col, sn_col, tcol, kind):
        d = students[[rk_col, sn_col, tcol]].dropna(subset=[rk_col, sn_col]).copy()
        d.columns = ["route_key", "stop_name", "t"]
        d = d[d.stop_name.astype(str).str.len() > 0]
        g = d.groupby(["route_key", "stop_name"], as_index=False).t.median()
        g.columns = ["route_key", "stop_name", f"roster_{kind}_min"]
        return g

    ram = roster_side("roster_am_route_key", "am_stop_name", "promised_pickup_min", "pickup")
    rpm = roster_side("roster_pm_route_key", "pm_stop_name", "promised_drop_min", "drop")

    uni = rl.merge(ram, on=["route_key", "stop_name"], how="outer") \
            .merge(rpm, on=["route_key", "stop_name"], how="outer")
    uni["in_route_list"] = uni.in_route_list.fillna(False)

    # The route list is the authority on printed times; the roster fills gaps.
    uni["printed_pickup_min"] = uni.promised_pickup_min.fillna(uni.roster_pickup_min)
    uni["printed_drop_min"] = uni.promised_drop_min.fillna(uni.roster_drop_min)

    # Rider counts, per leg, so an AM-only or PM-only stop reads correctly.
    am_riders = (students.dropna(subset=["roster_am_route_key", "am_stop_name"])
                 .groupby(["roster_am_route_key", "am_stop_name"]).size()
                 .rename("am_riders").reset_index())
    am_riders.columns = ["route_key", "stop_name", "am_riders"]
    pm_riders = (students.dropna(subset=["roster_pm_route_key", "pm_stop_name"])
                 .groupby(["roster_pm_route_key", "pm_stop_name"]).size()
                 .rename("pm_riders").reset_index())
    pm_riders.columns = ["route_key", "stop_name", "pm_riders"]
    uni = uni.merge(am_riders, on=["route_key", "stop_name"], how="left") \
             .merge(pm_riders, on=["route_key", "stop_name"], how="left")
    uni["am_riders"] = uni.am_riders.fillna(0).astype(int)
    uni["pm_riders"] = uni.pm_riders.fillna(0).astype(int)

    # Stops the roster added have no printed sequence. Order them by printed
    # pickup time and append after the sequenced stops, so a route still draws
    # in a sensible order rather than collapsing to an arbitrary one.
    uni = uni.sort_values(
        ["route_key", "stop_seq", "printed_pickup_min", "stop_name"],
        na_position="last",
    ).reset_index(drop=True)

    # Three stop names are printed twice on the same route. One pair is an
    # outright duplicated row (identical times); the other two carry DIFFERENT
    # printed times, so the route either passes the point twice or the source is
    # ambiguous. Either way the tap data cannot tell the visits apart - every tap
    # at that name collapses to one stop - so keeping both rows would double-count
    # the stop in every fleet total and produce two contradictory verdicts for one
    # physical place. Collapse to one row, keep the earliest printed times, and
    # carry the alternates so the fact is surfaced rather than buried.
    uni["printed_variants"] = ""
    dup_keys = uni[uni.duplicated(["route_key", "stop_name"], keep=False)]
    notes = {}
    for (rk, sn), g in dup_keys.groupby(["route_key", "stop_name"]):
        times = sorted({(ts.minutes_to_clock(p), ts.minutes_to_clock(d))
                        for p, d in zip(g.printed_pickup_min, g.printed_drop_min)})
        if len(times) > 1:
            notes[(rk, sn)] = "; ".join(f"{p} / {d}" for p, d in times)
    uni = uni.sort_values(["route_key", "stop_seq", "printed_pickup_min"],
                          na_position="last")
    agg = {c: "first" for c in uni.columns if c not in ("route_key", "stop_name")}
    agg.update({"printed_pickup_min": "min", "printed_drop_min": "min",
                "am_riders": "max", "pm_riders": "max", "in_route_list": "max"})
    uni = uni.groupby(["route_key", "stop_name"], as_index=False, sort=False).agg(agg)
    uni["printed_variants"] = [notes.get((r, s), "")
                               for r, s in zip(uni.route_key, uni.stop_name)]
    uni["in_route_list"] = uni.in_route_list.fillna(False).astype(bool)

    uni = uni.sort_values(
        ["route_key", "stop_seq", "printed_pickup_min", "stop_name"],
        na_position="last",
    ).reset_index(drop=True)
    uni["seq"] = uni.groupby("route_key").cumcount() + 1
    return uni


# --------------------------------------------------------------------------
# Observations: first tap per route-day-stop
# --------------------------------------------------------------------------

def first_tap_trace(spine: pd.DataFrame, leg: str) -> pd.DataFrame:
    """
    Bus-arrival proxy: the FIRST tap at a route x date x stop.

    Not the mean, and not per-student taps. Later taps at the same stop measure
    how fast the attendant scans, not how the bus moved: within-stop tap spread
    rises with the number of boarders (median 0 min at 2 boarders, 2 min at 6),
    which is scanning throughput. It is never dwell time.
    """
    d = spine[(spine.leg == leg) & spine.scan_present & spine.passes_sanity
              & spine.in_regime & spine.promised_time_min.notna()]
    if d.empty:
        return pd.DataFrame(columns=["route_key", "stop_name", "date",
                                     "arrival_min", "printed_min", "boarders"])
    g = d.groupby(["dominant_route_key", "stop_name", "date"], sort=False).agg(
        arrival_min=("actual_time_min", "min"),
        printed_min=("promised_time_min", "first"),
        boarders=("admission_no", "nunique"),
    ).reset_index().rename(columns={"dominant_route_key": "route_key"})
    return g


def school_arrival_trace(spine: pd.DataFrame) -> pd.DataFrame:
    """First school-arrival tap per route-day: the bus reaching school."""
    d = spine[(spine.leg == "am_school_arrival") & spine.scan_present
              & spine.passes_sanity & spine.in_regime]
    if d.empty:
        return pd.DataFrame(columns=["route_key", "date", "school_arrival_min"])
    return (d.groupby(["dominant_route_key", "date"], sort=False)
            .actual_time_min.min().reset_index()
            .rename(columns={"dominant_route_key": "route_key",
                             "actual_time_min": "school_arrival_min"}))


def gate_load(spine: pd.DataFrame) -> pd.DataFrame:
    """
    Dismissal gate load per route-day: p90 - p10 of leaving-school taps.

    How long it takes to get a bus loaded and away at dismissal. Bias-free leg,
    so this number is actionable today.
    """
    d = spine[(spine.leg == "pm_school_board") & spine.scan_present & spine.in_regime]
    if d.empty:
        return pd.DataFrame(columns=["route_key", "date", "load_min", "n"])
    g = d.groupby(["dominant_route_key", "date"]).actual_time_min.agg(
        p10=lambda s: s.quantile(0.10), p90=lambda s: s.quantile(0.90), n="count"
    ).reset_index().rename(columns={"dominant_route_key": "route_key"})
    g["load_min"] = g.p90 - g.p10
    return g[["route_key", "date", "load_min", "n"]]


# --------------------------------------------------------------------------
# Stop-order consistency
# --------------------------------------------------------------------------

def order_violations(trace: pd.DataFrame, uni: pd.DataFrame) -> dict:
    """
    How often a later stop's first tap lands at or before the previous stop's.

    Tested ONLY between stops genuinely adjacent in the printed route list.
    Stops that exist solely on the roster have no printed position; the universe
    parks them after the sequenced ones, and pairing those with their neighbours
    manufactures violations out of an ordering the school never published.

    When this fires often, per-stop times on that pair are misattributed by
    several minutes: the tap data cannot tell the two stops apart in time. It is
    a data-quality fact about the pair, not a finding about the bus.
    """
    out = {}
    seq = uni.dropna(subset=["stop_seq"]).sort_values(["route_key", "stop_seq"])
    for rk, g in seq.groupby("route_key"):
        names = list(g.stop_name)
        tr = trace[trace.route_key == rk]
        if tr.empty or len(names) < 2:
            continue
        by_day = {d: dict(zip(gg.stop_name, gg.arrival_min))
                  for d, gg in tr.groupby("date")}
        for a, b in zip(names, names[1:]):
            days = [(v[a], v[b]) for v in by_day.values() if a in v and b in v]
            if not days:
                continue
            # Strictly BEFORE, not at-or-before. Tap times resolve to the
            # minute and the attendant scans continuously as the bus moves, so
            # consecutive stops land in the same minute on 17% of day-pairs.
            # A tie means the two stops cannot be ORDERED, which is a resolution
            # limit; counting it as a violation conflates that with the tap
            # sequence actually being wrong, and roughly doubles the figure.
            # Both are reported, separately.
            bad = sum(1 for x, y in days if y < x)
            tie = sum(1 for x, y in days if y == x)
            out[(rk, a, b)] = {"n": len(days), "violations": bad, "ties": tie,
                               "rate": bad / len(days), "tie_rate": tie / len(days)}
    return out


# --------------------------------------------------------------------------
# Road times and congestion calibration
# --------------------------------------------------------------------------

def observed_pm_legs(pm_trace: pd.DataFrame, uni: pd.DataFrame) -> pd.DataFrame:
    """
    Observed leg durations between consecutive stops, from AFTERNOON DROP TAPS
    ONLY.

    That leg is bias-free: the tap happens as the child steps off the bus.
    Morning boarding taps carry L and would corrupt the calibration in a way
    that is invisible afterwards, so they are not used here at all.
    """
    order = (uni.dropna(subset=["printed_drop_min"])
             .sort_values(["route_key", "printed_drop_min", "seq"])
             .assign(pm_seq=lambda d: d.groupby("route_key").cumcount() + 1))
    pos = {(r.route_key, r.stop_name): r.pm_seq for r in order.itertuples()}
    name = {(r.route_key, r.pm_seq): r.stop_name for r in order.itertuples()}

    tr = pm_trace.copy()
    tr["pm_seq"] = [pos.get((r, s)) for r, s in zip(tr.route_key, tr.stop_name)]
    tr = tr.dropna(subset=["pm_seq"])

    rows = []
    for (rk, date), g in tr.groupby(["route_key", "date"]):
        m = dict(zip(g.pm_seq, g.arrival_min))
        for a in sorted(m):
            b = a + 1
            if b not in m:
                continue
            dur = m[b] - m[a]
            if not (LEG_MIN_MIN <= dur <= LEG_MAX_MIN):
                continue
            rows.append({
                "route_key": rk, "date": date,
                "from_stop": name.get((rk, a)), "to_stop": name.get((rk, b)),
                "from_seq": a, "to_seq": b,
                "observed_min": dur, "depart_min": m[a],
            })
    return pd.DataFrame(rows)


def hour_band(minutes: float) -> str:
    h = int(minutes // 60)
    return f"{h:02d}:00-{h:02d}:59"


# --------------------------------------------------------------------------
# Main build
# --------------------------------------------------------------------------

def build(inputs: Path, outputs: Path, cache_dir: Path, offline: bool = False) -> dict:
    outputs.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    def find(pattern):
        m = sorted(inputs.glob(pattern))
        if not m:
            raise FileNotFoundError(f"No file matching {pattern!r} in {inputs}")
        return m[0]

    routes_p = find("Route*List*Area*.xlsx")
    roster_p = find("Route*Wise*Student*.xlsx")
    logs_p = find("*RFID*Attendance*.xlsx")
    print(f"  route list : {routes_p.name}")
    print(f"  roster     : {roster_p.name}")
    print(f"  tap logs   : {logs_p.name}")

    res = ts.build_all(routes_p, roster_p, logs_p)
    spine, students, routes = res["spine"], res["students"], res["routes"]
    logs, audit, exceptions = res["logs"], res["audit"], res["exceptions"]

    uni = build_stop_universe(routes, students)
    n_paper = len(uni)

    # Route assignments were restructured mid-term, so a child rostered on one
    # route taps under another. The observation trace is keyed by the route a
    # child ACTUALLY travels; the paper universe by the route they are rostered
    # on. Any (route, stop) existing only in the trace would carry real
    # observations that appear nowhere in the dashboard - the browser would then
    # count fewer late stops than the pipeline does, from identical data. Fold
    # them in so that what is displayed and what is checked are the same set.
    observed = pd.concat([
        first_tap_trace(spine, "am_pickup")[["route_key", "stop_name", "printed_min"]]
            .rename(columns={"printed_min": "printed_pickup_min"}),
        first_tap_trace(spine, "pm_dropoff")[["route_key", "stop_name", "printed_min"]]
            .rename(columns={"printed_min": "printed_drop_min"}),
    ]).groupby(["route_key", "stop_name"], as_index=False).first()
    have = set(zip(uni.route_key, uni.stop_name))
    mask = [k not in have for k in zip(observed.route_key, observed.stop_name)]
    extra = observed[mask].copy()
    uni["observed_only"] = False
    if len(extra):
        extra["in_route_list"] = False
        extra["observed_only"] = True
        extra["stop_seq"] = np.nan
        extra["printed_variants"] = ""
        extra["am_riders"] = 0
        extra["pm_riders"] = 0
        uni = pd.concat([uni, extra], ignore_index=True)
        uni["observed_only"] = uni.observed_only.fillna(False).astype(bool)
        uni["in_route_list"] = uni.in_route_list.fillna(False).astype(bool)
        uni["am_riders"] = uni.am_riders.fillna(0).astype(int)
        uni["pm_riders"] = uni.pm_riders.fillna(0).astype(int)
        uni["printed_variants"] = uni.printed_variants.fillna("")
        uni = uni.sort_values(["route_key", "stop_seq", "printed_pickup_min", "stop_name"],
                              na_position="last").reset_index(drop=True)
        uni["seq"] = uni.groupby("route_key").cumcount() + 1
    print(f"  stop universe: {len(uni)} route-stop pairs across "
          f"{uni.route_key.nunique()} routes ({n_paper} on paper, "
          f"{len(extra)} observed only under a re-assigned route)")

    # ---------------- geocoding ----------------
    corpus = sorted({t for n in uni.stop_name.dropna().unique()
                     for t in geo.split_pickup_drop(n)})
    corpus.append("Prometheus School Noida")
    gc = geo.Geocoder(cache_dir, offline=offline, corpus=corpus)

    # Override key must not start with "_": geo.Geocoder drops such keys
    # (that is how it skips "_README"). Same key as optimiser.build_problem.SCHOOL_KEY.
    school = gc.resolve("Prometheus School Noida", override_key="Prometheus School Noida")
    if school["lat"] is None:
        school = {"lat": geo.BIAS_LAT, "lon": geo.BIAS_LON,
                  "label": "(fallback: geocode failed)", "confidence": "none",
                  "verified": False, "source": "fallback", "provider": "",
                  "matched_query": "", "raw_type": "", "note": "",
                  "query_text": "Prometheus School Noida"}

    geo_rows: dict[tuple, dict] = {}
    for r in uni.itertuples():
        p_text, d_text = geo.split_pickup_drop(r.stop_name)
        split = p_text != d_text
        for role, text in (("P", p_text), ("D", d_text)):
            key = (r.route_key, r.stop_name, role)
            ok = f"{r.stop_name}||{role}" if split else r.stop_name
            rec = gc.resolve(text, override_key=ok)
            rec.update({"route_key": r.route_key, "stop_name": r.stop_name,
                        "role": role, "split_cell": split, "seq": r.seq})
            geo_rows[key] = rec
    gc.save()
    print(f"  geocoding: {gc.calls} live calls ({gc.google_calls} Google), "
          f"{sum(1 for v in geo_rows.values() if v['lat'] is not None)}/{len(geo_rows)} resolved, "
          f"{sum(1 for v in geo_rows.values() if len(v.get('agree', [])) >= 2)} confirmed by "
          f"2+ independent sources")

    def coord(rk, sn, role):
        rec = geo_rows.get((rk, sn, role))
        return (rec["lat"], rec["lon"]) if rec and rec["lat"] is not None else None

    # ---------------- geocode review flags ----------------
    for (rk, sn, role), rec in geo_rows.items():
        flags = []
        if rec["lat"] is None:
            flags.append("no result - geocoder returned nothing usable")
        else:
            if not geo.in_bbox(rec["lat"], rec["lon"]):
                flags.append("outside Delhi NCR bounding box")
            if rec["confidence"] == "low":
                flags.append("low-confidence match type")
            if not geo.core_text(rec["query_text"]).strip():
                flags.append("stop name carries no locating text")
            for d in rec.get("disagree", []):
                flags.append(f"{d['source']} puts it {d['km']} km away")
            want = geo._distinctive_tokens(geo.core_text(rec["query_text"]))
            got = geo._distinctive_tokens(rec["label"])
            if want:
                w = gc.idf
                tot = sum(w.get(t, 1.0) for t in want)
                mat = sum(w.get(t, 1.0) for t in (want & got))
                rec["name_match"] = round(mat / tot, 3) if tot else 0.0
                if rec["name_match"] < 0.60:
                    flags.append("weak name match - service returned a different place name")
            else:
                rec["name_match"] = 0.0
                flags.append("stop name too generic to verify")
        rec["flags"] = flags

    # distance to the neighbouring stop on the same route
    for rk, g in uni.groupby("route_key"):
        rows = list(g.sort_values("seq").itertuples())
        for i, r in enumerate(rows):
            for role in ("P", "D"):
                c = coord(rk, r.stop_name, role)
                if not c:
                    continue
                nbrs = []
                for j in (i - 1, i + 1):
                    if 0 <= j < len(rows):
                        c2 = coord(rk, rows[j].stop_name, role)
                        if c2:
                            nbrs.append(geo.haversine_km(c[0], c[1], c2[0], c2[1]))
                if nbrs:
                    dmin = min(nbrs)
                    geo_rows[(rk, r.stop_name, role)]["nearest_neighbour_km"] = round(dmin, 2)
                    if dmin > geo.FAR_FROM_NEIGHBOUR_KM:
                        geo_rows[(rk, r.stop_name, role)]["flags"].append(
                            f"{dmin:.1f} km from the next stop on this route")

    # ---------------- observations ----------------
    am_trace = first_tap_trace(spine, "am_pickup")
    pm_trace = first_tap_trace(spine, "pm_dropoff")
    sch = school_arrival_trace(spine)
    gl = gate_load(spine)

    all_dates = sorted(pd.to_datetime(spine.date.dropna().unique()))
    date_index = {d: i for i, d in enumerate(all_dates)}
    date_strings = [d.strftime("%Y-%m-%d") for d in all_dates]

    def obs_array(df):
        return [[date_index[pd.Timestamp(r.date)], _f(r.arrival_min)]
                for r in df.itertuples() if pd.Timestamp(r.date) in date_index]

    am_by_stop = {k: v for k, v in am_trace.groupby(["route_key", "stop_name"])}
    pm_by_stop = {k: v for k, v in pm_trace.groupby(["route_key", "stop_name"])}

    ov = order_violations(am_trace, uni)

    # ---------------- road times ----------------
    rt = geo.Router(cache_dir, offline=offline)
    road_legs: dict[tuple, dict] = {}

    def leg_between(rk, s_from, s_to, role, depart_min):
        a, b = coord(rk, s_from, role), coord(rk, s_to, role)
        if not a or not b:
            return {}
        return rt.leg(a, b, depart_min=None if depart_min is None or pd.isna(depart_min)
                      else float(depart_min))

    am_paths: dict[str, list] = {}
    pm_paths: dict[str, list] = {}

    for rk, g in uni.groupby("route_key"):
        # morning: printed stop sequence, then the run in to school
        am_rows = list(g.sort_values("seq").itertuples())
        path = []
        for a, b in zip(am_rows, am_rows[1:]):
            leg = leg_between(rk, a.stop_name, b.stop_name, "P", a.printed_pickup_min)
            road_legs[(rk, "AM", a.stop_name, b.stop_name)] = leg
            path.extend(leg.get("geometry", []))
        if am_rows:
            last = am_rows[-1]
            c = coord(rk, last.stop_name, "P")
            if c:
                leg = rt.leg(c, (school["lat"], school["lon"]),
                             depart_min=float(last.printed_pickup_min)
                             if pd.notna(last.printed_pickup_min) else None)
                road_legs[(rk, "AM", last.stop_name, "__SCHOOL__")] = leg
                path.extend(leg.get("geometry", []))
        am_paths[rk] = path

        # afternoon: printed drop order, starting from school
        pm_rows = list(g.dropna(subset=["printed_drop_min"])
                       .sort_values(["printed_drop_min", "seq"]).itertuples())
        path = []
        if pm_rows:
            c = coord(rk, pm_rows[0].stop_name, "D")
            if c:
                # The bus leaves school before the first printed drop by roughly
                # the leg itself; a straight-line estimate is close enough to pick
                # the departure time for the traffic prediction.
                est = geo.haversine_km(school["lat"], school["lon"], c[0], c[1]) * 1.35 / 22 * 60
                leg = rt.leg((school["lat"], school["lon"]), c,
                             depart_min=float(pm_rows[0].printed_drop_min) - est
                             if pd.notna(pm_rows[0].printed_drop_min) else None)
                road_legs[(rk, "PM", "__SCHOOL__", pm_rows[0].stop_name)] = leg
                path.extend(leg.get("geometry", []))
        for a, b in zip(pm_rows, pm_rows[1:]):
            leg = leg_between(rk, a.stop_name, b.stop_name, "D", a.printed_drop_min)
            road_legs[(rk, "PM", a.stop_name, b.stop_name)] = leg
            path.extend(leg.get("geometry", []))
        pm_paths[rk] = path
    rt.save()
    n_traffic = sum(1 for v in road_legs.values() if v and v.get("duration_traffic_s"))
    print(f"  road times: {rt.calls} live calls ({rt.google_calls} Google), {rt.failures} failures, "
          f"{sum(1 for v in road_legs.values() if v)}/{len(road_legs)} legs resolved, "
          f"{n_traffic} with a traffic prediction")

    # ---------------- congestion calibration ----------------
    obs_legs = observed_pm_legs(pm_trace, uni)
    calib_rows = []
    if not obs_legs.empty:
        med = (obs_legs.groupby(["route_key", "from_stop", "to_stop"])
               .agg(observed_median_min=("observed_min", "median"),
                    n=("observed_min", "size"),
                    depart_min=("depart_min", "median")).reset_index())
        for r in med.itertuples():
            leg = road_legs.get((r.route_key, "PM", r.from_stop, r.to_stop), {})
            road_min = (leg.get("duration_s") or 0) / 60.0 if leg else None
            ratio = (r.observed_median_min / road_min) if road_min else None
            # Google's own traffic prediction for that departure time. observed /
            # predicted near 1 means the prediction can be used as-is for this
            # fleet; the residual is dwell, driving style and local effects.
            traffic_min = (leg.get("duration_traffic_s") or 0) / 60.0 if leg else None
            ratio_traffic = (r.observed_median_min / traffic_min) if traffic_min else None
            calib_rows.append({
                "route_key": r.route_key, "from_stop": r.from_stop, "to_stop": r.to_stop,
                "observed_median_min": _f(r.observed_median_min), "n": int(r.n),
                "road_min": _f(road_min), "road_km": _f((leg.get("distance_m") or 0) / 1000.0) if leg else None,
                "ratio": _f(ratio), "hour_band": hour_band(r.depart_min),
                "road_traffic_min": _f(traffic_min) if traffic_min else None,
                "ratio_traffic": _f(ratio_traffic),
                "road_provider": leg.get("provider") if leg else None,
            })
    calib = pd.DataFrame(calib_rows)

    fleet_ratio = fleet_ratio_traffic = None
    by_band = []
    if not calib.empty and calib.ratio.notna().any():
        good = calib[calib.ratio.notna() & (calib.n >= 5)]
        fleet_ratio = _f(good.ratio.median()) if not good.empty else _f(calib.ratio.median())
        src = good if not good.empty else calib
        if src.ratio_traffic.notna().any():
            fleet_ratio_traffic = _f(src.ratio_traffic.median())
        for band, g in src.groupby("hour_band"):
            by_band.append({"hour_band": band, "n_legs": int(len(g)),
                            "median_ratio": _f(g.ratio.median()),
                            "median_ratio_traffic": _f(g.ratio_traffic.median())
                            if g.ratio_traffic.notna().any() else None,
                            "observed_median_min": _f(g.observed_median_min.median()),
                            "road_median_min": _f(g.road_min.median()),
                            "road_traffic_median_min": _f(g.road_traffic_min.median())
                            if g.road_traffic_min.notna().any() else None})
        by_band.sort(key=lambda x: x["hour_band"])

    # ---------------- coverage ----------------
    # Measured against the LOGS, not the post-quarantine spine. A quarantined
    # row is still a student-day on which a tap was expected, so excluding it
    # from the denominator would flatter the coverage figure.
    cov_by_leg = []
    for leg in ts.LEGS:
        taps = int(logs[leg].notna().sum())
        cov_by_leg.append({
            "leg": leg, "label": LEG_LABEL[leg], "bias": LEG_BIAS[leg],
            "student_days": len(logs), "taps": taps,
            "coverage_pct": pct(taps, len(logs)),
        })

    cov_route = []
    for rk, g in logs.groupby("route_key"):
        for leg in ts.LEGS:
            taps = int(g[leg].notna().sum())
            cov_route.append({"route_key": rk, "leg": leg, "student_days": len(g),
                              "taps": taps, "coverage_pct": pct(taps, len(g))})

    cov_date = []
    for dt, g in logs.groupby("date"):
        for leg in ts.LEGS:
            cov_date.append({"date": pd.Timestamp(dt).strftime("%Y-%m-%d"), "leg": leg,
                             "coverage_pct": pct(int(g[leg].notna().sum()), len(g)),
                             "student_days": len(g)})

    # ---------------- routes ----------------
    reporting = (students.dropna(subset=["roster_am_route_key"])
                 .groupby("roster_am_route_key").reporting_time_min.median())
    log_routes = set(logs.route_key.dropna().unique())
    # "Operating" means children actually belong to this route and still travel
    # on it, not merely that some tap carries its label. A withdrawn route keeps
    # collecting stray taps from children who board it once; judging by last tap
    # date alone would report it as running. Recency is therefore measured on
    # IN-REGIME rows - those where the route is the child's dominant one.
    last_inregime = logs[logs.in_regime].groupby("dominant_route_key").date.max()
    period_end = max(all_dates) if all_dates else None
    STALE_DAYS = 10

    route_payload = []
    for rk, g in uni.groupby("route_key"):
        if rk is None or (isinstance(rk, float) and pd.isna(rk)):
            continue
        g = g.sort_values("seq")

        am_students = int((students.roster_am_route_key == rk).sum())
        pm_students = int((students.roster_pm_route_key == rk).sum())
        travelled = int(logs[(logs.route_key == rk)].admission_no.nunique())

        # operating status, derived - no route names or dates hardcoded anywhere
        seen_last = last_inregime.get(rk)
        if rk not in log_routes:
            status = "not observed"
            status_note = (f"No tap data at all in this period; {am_students} children "
                           "are rostered on it")
        elif seen_last is None or (period_end is not None
                                   and (period_end - seen_last).days > STALE_DAYS):
            status = "ceased during period"
            when = logs[logs.route_key == rk].date.max()
            status_note = (
                f"No child's usual route any more; last tap "
                f"{pd.Timestamp(when).strftime('%d %b')}. Its children now travel elsewhere")
        else:
            status, status_note = "operating", ""

        glr = gl[gl.route_key == rk]
        gate = {
            "median_min": _f(glr.load_min.median()) if not glr.empty else None,
            "days": int(len(glr)),
            "days_over_30_pct": pct(int((glr.load_min > 30).sum()), len(glr)) if len(glr) else None,
        }

        schr = sch[sch.route_key == rk]
        rows_am, rows_pm = [], []
        for r in g.itertuples():
            gp = geo_rows.get((rk, r.stop_name, "P"), {})
            gd = geo_rows.get((rk, r.stop_name, "D"), {})
            am_obs = obs_array(am_by_stop.get((rk, r.stop_name), pd.DataFrame(
                columns=["date", "arrival_min"])))
            pm_obs = obs_array(pm_by_stop.get((rk, r.stop_name), pd.DataFrame(
                columns=["date", "arrival_min"])))

            prev = None
            idx = list(g.seq).index(r.seq)
            if idx > 0:
                prev = list(g.itertuples())[idx - 1].stop_name
            am_leg = road_legs.get((rk, "AM", prev, r.stop_name), {}) if prev else {}

            base = {
                "seq": int(r.seq), "stop": r.stop_name,
                "in_route_list": bool(r.in_route_list),
                "printed_variants": r.printed_variants or "",
                "order_flag": None,
            }
            if prev:
                v = ov.get((rk, prev, r.stop_name))
                if v and v["rate"] > ORDER_VIOLATION_RATE:
                    base["order_flag"] = {"rate_pct": round(100 * v["rate"], 1), "n": v["n"]}

            rows_am.append({**base,
                "printed_min": _f(r.printed_pickup_min),
                "riders": int(r.am_riders),
                "obs": am_obs,
                "lat": _f(gp.get("lat")), "lon": _f(gp.get("lon")),
                "geo_verified": bool(gp.get("verified")),
                "geo_flags": gp.get("flags", []),
                "geo_label": gp.get("label", ""),
                "road_from_prev_s": _f(am_leg.get("duration_s")),
                "road_from_prev_traffic_s": _f(am_leg.get("duration_traffic_s")),
                "road_from_prev_km": _f((am_leg.get("distance_m") or 0) / 1000.0) if am_leg else None,
            })

        pm_sorted = g.dropna(subset=["printed_drop_min"]).sort_values(
            ["printed_drop_min", "seq"])
        pm_list = list(pm_sorted.itertuples())
        for i, r in enumerate(pm_list):
            gd = geo_rows.get((rk, r.stop_name, "D"), {})
            prev = pm_list[i - 1].stop_name if i > 0 else "__SCHOOL__"
            pm_leg = road_legs.get((rk, "PM", prev, r.stop_name), {})
            c = calib[(calib.route_key == rk) & (calib.from_stop == prev) &
                      (calib.to_stop == r.stop_name)] if not calib.empty else pd.DataFrame()
            rows_pm.append({
                "seq": i + 1, "stop": r.stop_name,
                "printed_variants": r.printed_variants or "",
                "printed_min": _f(r.printed_drop_min),
                "riders": int(r.pm_riders),
                "obs": obs_array(pm_by_stop.get((rk, r.stop_name),
                                                pd.DataFrame(columns=["date", "arrival_min"]))),
                "lat": _f(gd.get("lat")), "lon": _f(gd.get("lon")),
                "geo_verified": bool(gd.get("verified")),
                "geo_flags": gd.get("flags", []),
                "geo_label": gd.get("label", ""),
                "road_from_prev_s": _f(pm_leg.get("duration_s")),
                "road_from_prev_traffic_s": _f(pm_leg.get("duration_traffic_s")),
                "road_from_prev_km": _f((pm_leg.get("distance_m") or 0) / 1000.0) if pm_leg else None,
                "observed_leg_median_min": _f(c.observed_median_min.iloc[0]) if len(c) else None,
                "leg_ratio": _f(c.ratio.iloc[0]) if len(c) else None,
                "leg_n": _i(c.n.iloc[0]) if len(c) else None,
            })

        # Feasibility inputs. The verdict itself is computed in the browser,
        # because it moves with L.
        #
        # Anchored on the last stop in the printed sequence that actually has
        # observations, not simply the last printed stop. On 10 of 25 routes the
        # final printed stop has zero taps - nobody boards there - so anchoring
        # on it blindly leaves those routes with no verdict at all. Falling back
        # up the sequence trades a slightly shorter measured run-in for an
        # assessable route, and the stop used is reported alongside the number.
        candidates = [r for r in g.itertuples()
                      if pd.notna(r.printed_pickup_min)
                      and len(am_by_stop.get((rk, r.stop_name), [])) > 0]
        strong = [r for r in candidates
                  if len(am_by_stop[(rk, r.stop_name)]) >= MIN_OBS]
        final = (strong or candidates or [None])[-1]
        if final is None:
            final = g.iloc[-1] if len(g) else None
        def runin_from(stop_row):
            if stop_row is None or schr.empty:
                return []
            ft = am_by_stop.get((rk, stop_row.stop_name))
            if ft is None or ft.empty:
                return []
            m = ft.merge(schr, on="date", how="inner")
            return [x for x in (m.school_arrival_min - m.arrival_min).tolist()
                    if x is not None and not pd.isna(x) and 0 < x < 150]

        runins = runin_from(final)
        # The brief's literal definition, kept alongside for reconciliation only.
        last_printed = g.iloc[-1] if len(g) else None
        strict_runins = runin_from(last_printed)

        route_payload.append({
            "route_key": rk, "route_num": int(rk[1:]), "label": f"Route {int(rk[1:])}",
            "status": status, "status_note": status_note,
            "am_students": am_students, "pm_students": pm_students,
            "travelled_students": travelled,
            "stops": int(len(g)),
            "reporting_min": _f(reporting.get(rk)),
            "gate_load": gate,
            "final_stop": final.stop_name if final is not None else None,
            "final_printed_min": _f(final.printed_pickup_min) if final is not None else None,
            "final_is_last_printed": bool(final is not None and len(g)
                                          and final.stop_name == g.iloc[-1].stop_name),
            "last_printed_stop": last_printed.stop_name if last_printed is not None else None,
            "strict_final_printed_min": (_f(last_printed.printed_pickup_min)
                                         if last_printed is not None else None),
            "strict_runin_median_min": _f(np.median(strict_runins)) if strict_runins else None,
            "strict_runin_n": len(strict_runins),
            "runin_median_min": _f(np.median(runins)) if runins else None,
            "runin_n": len(runins),
            "am": rows_am, "pm": rows_pm,
            "am_path": am_paths.get(rk, []), "pm_path": pm_paths.get(rk, []),
            "coverage": {c["leg"]: c["coverage_pct"]
                         for c in cov_route if c["route_key"] == rk},
        })
    route_payload.sort(key=lambda r: r["route_num"])

    # ---------------- exceptions ----------------
    wide = spine.pivot_table(index=["admission_no", "student_name", "date"],
                             columns="leg", values="actual_time_min",
                             aggfunc="first").reset_index()
    for leg in ts.LEGS:
        if leg not in wide.columns:
            wide[leg] = np.nan

    am_no_pm = wide[wide.am_pickup.notna() & wide.pm_dropoff.isna()]
    pm_no_am = wide[wide.pm_dropoff.notna() & wide.am_pickup.isna()]

    off = logs[~logs.in_regime]
    off_route = (off.groupby(["admission_no", "student_name_log", "dominant_route_key",
                              "route_key"]).size().rename("days").reset_index()
                 .sort_values("days", ascending=False))

    travelled_routes = logs.groupby("admission_no").route_key.agg(set)
    stale = []
    for s in students.itertuples():
        seen = travelled_routes.get(s.admission_no, set())
        if s.roster_am_route_key and seen and s.roster_am_route_key not in seen:
            stale.append({"admission_no": s.admission_no, "student_name": s.student_name,
                          "rostered": s.roster_am_route_key,
                          "actually_travels": sorted(seen)})

    exceptions_payload = {
        "am_no_pm": {"count": int(len(am_no_pm)),
                     "children": int(am_no_pm.admission_no.nunique()),
                     "rows": [{"student": r.student_name, "admission_no": r.admission_no,
                               "date": pd.Timestamp(r.date).strftime("%Y-%m-%d")}
                              for r in am_no_pm.itertuples()][:400]},
        "pm_no_am": {"count": int(len(pm_no_am)),
                     "children": int(pm_no_am.admission_no.nunique()),
                     "rows": [{"student": r.student_name, "admission_no": r.admission_no,
                               "date": pd.Timestamp(r.date).strftime("%Y-%m-%d")}
                              for r in pm_no_am.itertuples()][:400]},
        "off_route": {"student_days": int(off.shape[0]),
                      "children": int(off.admission_no.nunique()),
                      "rows": [{"student": r.student_name_log, "admission_no": r.admission_no,
                                "usual": r.dominant_route_key, "travelled": r.route_key,
                                "days": int(r.days)} for r in off_route.itertuples()][:400]},
        "stale_roster": {"children": len(stale), "rows": stale[:400]},
    }

    # ---------------- fleet-level figures ----------------
    ov_use = [v for v in ov.values() if v["n"] >= 5]
    ov_rates = [v["rate"] for v in ov_use]
    fleet_order_pct = pct(sum(1 for r in ov_rates if r > ORDER_VIOLATION_RATE), len(ov_rates))
    tie_days = sum(v["ties"] for v in ov_use)
    all_days = sum(v["n"] for v in ov_use)
    fleet_tie_pct = pct(tie_days, all_days)

    am_all = am_trace.assign(dev=am_trace.arrival_min - am_trace.printed_min)
    # Same basis as the rest of the afternoon analysis: in-regime, sanity-passed,
    # against a known printed time.
    pm_all_dev = spine[(spine.leg == "pm_dropoff") & spine.scan_present
                       & spine.passes_sanity & spine.in_regime
                       & spine.promised_time_min.notna()].delta_min.dropna()

    # Distinct physical locations. For the 230-odd ordinary stops the pickup and
    # drop point are the same place and must be counted once; only the stops
    # whose source cell packs two locations contribute two.
    distinct_geo = [v for v in geo_rows.values()
                    if v.get("split_cell") or v.get("role") == "P"]
    n_geo_pts = len(distinct_geo)
    n_resolved = sum(1 for v in distinct_geo if v["lat"] is not None)
    n_verified = sum(1 for v in distinct_geo if v.get("verified"))
    n_flagged = sum(1 for v in distinct_geo if v.get("flags"))

    # ---------------- acceptance checks ----------------
    stops_ge_min = len({(r, s) for (r, s), g in am_by_stop.items() if len(g) >= MIN_OBS}
                       | {(r, s) for (r, s), g in pm_by_stop.items() if len(g) >= MIN_OBS})

    def lcount(L):
        late = early = 0
        for (rk, sn), g in am_by_stop.items():
            if len(g) < MIN_OBS:
                continue
            dev = (g.arrival_min - g.printed_min)
            med = dev.median() - L
            iqr = dev.quantile(.75) - dev.quantile(.25)
            if iqr <= TIGHT_IQR and med >= LATE_THRESHOLD:
                late += 1
            elif iqr <= TIGHT_IQR and med <= -LATE_THRESHOLD:
                early += 1
        return late, early

    acc = []

    def check(name, expected, actual):
        """
        Compare at the precision the brief states. It quotes "+4 min" and
        "18.3"; producing 4.0 and 18.337 is agreement, not divergence, and
        flagging it as a difference would bury the divergences that are real.
        """
        match = str(expected) == str(actual)
        if not match and isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            dp = len(str(expected).split(".")[1]) if "." in str(expected) else 0
            match = round(float(actual), dp) == round(float(expected), dp)
        acc.append({"check": name, "expected": expected, "actual": actual,
                    "match": bool(match)})

    n_raw = next(e["rows"] for e in audit.entries if e["action"] == "raw log rows")
    n_drop = next(e["rows"] for e in audit.entries
                  if e["action"].startswith("rows dropped: non-operational"))
    check("Raw tap rows", 10134, n_raw)
    check("Dropped as Testing", 8, n_drop)
    check("Usable tap rows", 10126, n_raw - n_drop)
    check("Distinct children", 405, int(logs.admission_no.nunique()))
    check("School days", 31, len(all_dates))
    check("Route labels in logs", 24, int(logs.route_key.nunique()))
    check("Roster students", 444, int(len(students)))
    check("Roster stops (on paper)", 198, int(n_paper))
    check("Roster routes", 25, int(uni.route_key.nunique()))
    check("Routes operating", 23,
          sum(1 for r in route_payload if r["status"] == "operating"))
    for leg, want in zip(ts.LEGS, [86.9, 79.5, 95.6, 70.4]):
        check(f"Coverage {LEG_LABEL[leg]}", want,
              next(c["coverage_pct"] for c in cov_by_leg if c["leg"] == leg))
    check("Morning median deviation", 4, _f(am_all.dev.median()))
    check("Afternoon deviation sd", 18.3, _f(pm_all_dev.std()))
    check(f"Stops with n>={MIN_OBS} on a leg", 176, stops_ge_min)
    for L, want in [(0, 50), (3, 18), (6, 7)]:
        check(f"Stops running late at L={L}", want, lcount(L)[0])
    for L, want in [(0, 0), (6, 46)]:
        check(f"Stops running early at L={L}", want, lcount(L)[1])
    def infeasible(L, strict=False):
        """
        strict=True reproduces the brief's definition literally: anchor only on
        the LAST PRINTED stop. On 10 of 25 routes that stop has no tap data, so
        the strict reading silently leaves those routes unassessed. The dashboard
        anchors on the last printed stop that actually has observations, which
        assesses every route and finds more of them over-committed. Both are
        reported so the difference is visible as the definitional choice it is.
        """
        pk, rk_, nk = ("strict_final_printed_min", "strict_runin_median_min",
                       "strict_runin_n") if strict else (
                      "final_printed_min", "runin_median_min", "runin_n")
        n = 0
        for r in route_payload:
            if (r[pk] is None or r[rk_] is None or r["reporting_min"] is None
                    or not r[nk]):
                continue
            if r["reporting_min"] - (r[pk] + r[rk_] + L) < 0:
                n += 1
        return n

    for L, want in [(0, 1), (6, 7), (8, 8)]:
        check(f"Routes cannot reach school, L={L} (brief's strict definition)",
              want, infeasible(L, strict=True))
    for L in (0, 6, 8):
        acc.append({"check": f"  ... same, anchored on last OBSERVED stop (dashboard)",
                    "expected": "n/a", "actual": infeasible(L), "match": True})
    check("Off-route student-days", 781, int(off.shape[0]))
    check("Children travelling off-route", 104, int(off.admission_no.nunique()))
    r7 = gl[gl.route_key == "R07"]
    check("Route 7 gate load (min)", 44.4, _f(r7.load_min.median()) if not r7.empty else None)

    # ---------------- assemble ----------------
    data = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "period_start": date_strings[0] if date_strings else None,
            "period_end": date_strings[-1] if date_strings else None,
            "school_days": len(all_dates),
            "dates": date_strings,
            "school": {"lat": _f(school["lat"]), "lon": _f(school["lon"]),
                       "label": school["label"], "verified": bool(school.get("verified"))},
            "geocoder": gc.provider, "router": rt.provider,
            "router_has_traffic": any(v.get("duration_traffic_s") for v in road_legs.values() if v),
            "constants": {
                "MIN_OBS": MIN_OBS, "LATE_THRESHOLD": LATE_THRESHOLD,
                "TIGHT_IQR": TIGHT_IQR, "UNPREDICTABLE_IQR": UNPREDICTABLE_IQR,
                "ONTIME_BAND": ONTIME_BAND, "LOW_COVERAGE": LOW_COVERAGE,
                "ORDER_VIOLATION_RATE": ORDER_VIOLATION_RATE,
                "RATIO_LOW": RATIO_LOW, "RATIO_HIGH": RATIO_HIGH,
                "L_MIN": L_MIN, "L_MAX": L_MAX, "L_DEFAULT": L_DEFAULT,
            },
        },
        "totals": {
            "children_in_logs": int(logs.admission_no.nunique()),
            "roster_students": int(len(students)),
            "routes_roster": int(uni.route_key.nunique()),
            "routes_in_logs": int(logs.route_key.nunique()),
            "routes_operating": sum(1 for r in route_payload if r["status"] == "operating"),
            "stops": int(len(uni)),
            "taps_raw": int(n_raw),
            "taps_usable": int(n_raw - n_drop),
            "geo_points": n_geo_pts, "geo_resolved": n_resolved,
            "geo_verified": n_verified, "geo_flagged": n_flagged,
            "geo_unverified": n_geo_pts - n_verified,
            "fleet_order_violation_pct": fleet_order_pct,
            "order_pairs_tested": len(ov_rates),
            "fleet_same_minute_pct": fleet_tie_pct,
            "order_day_pairs_tested": all_days,
        },
        "coverage": {"by_leg": cov_by_leg, "by_route": cov_route, "by_date": cov_date},
        "exclusions": audit.to_frame().to_dict("records"),
        "quarantine": (exceptions.groupby("exception_reason").size()
                       .rename("rows").reset_index().to_dict("records")
                       if len(exceptions) else []),
        "routes": route_payload,
        "calibration": {"fleet_ratio": fleet_ratio, "fleet_ratio_traffic": fleet_ratio_traffic,
                        "by_hour_band": by_band,
                        "legs": calib.to_dict("records") if not calib.empty else []},
        "exceptions": exceptions_payload,
        "acceptance": acc,
    }

    data = json_safe(data)
    out_json = outputs / "dashboard_data.json"
    out_json.write_text(
        json.dumps(data, separators=(",", ":"), default=str, allow_nan=False),
        encoding="utf-8")

    # ---------------- geocode_review.csv ----------------
    review = outputs / "geocode_review.csv"
    with review.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["route", "stop_name", "role", "split_cell", "query_text",
                    "lat", "lon", "returned_address", "confidence", "name_match",
                    "provider", "matched_query", "nearest_neighbour_km",
                    "verified", "sources_agreeing", "google_place_id", "flags"])
        rows = sorted((kv for kv in geo_rows.items()
                       if kv[1].get("split_cell") or kv[0][2] == "P"),
                      key=lambda kv: (-len(kv[1].get("flags", [])),
                                      str(kv[0][0]), str(kv[0][1])))
        for (rk, sn, role), r in rows:
            # Non-split cells have one location serving both legs; say so rather
            # than emitting two identical rows for a reviewer to check twice.
            role = role if r.get("split_cell") else "P+D"
            w.writerow([rk, sn, role, r.get("split_cell"), r["query_text"],
                        r["lat"], r["lon"], r["label"], r["confidence"],
                        r.get("name_match"), r["provider"], r["matched_query"],
                        r.get("nearest_neighbour_km"), r["verified"],
                        " + ".join(r.get("agree", [])), r.get("place_id", ""),
                        "; ".join(r.get("flags", []))])

    print(f"\n  wrote {out_json.name} ({out_json.stat().st_size/1024:.0f} KB)")
    print(f"  wrote {review.name} ({n_flagged} of {n_geo_pts} points flagged for review)")
    return {"data": data, "geo_rows": geo_rows, "uni": uni, **res}


def print_acceptance(data: dict):
    print("\n" + "=" * 74)
    print("ACCEPTANCE CHECKS  (section 9 of the brief)")
    print("=" * 74)
    print(f"{'check':<42}{'expected':>11}{'actual':>11}  ")
    print("-" * 74)
    for a in data["acceptance"]:
        mark = "OK " if a["match"] else "DIFF"
        print(f"{a['check']:<42}{str(a['expected']):>11}{str(a['actual']):>11}  {mark}")
    bad = [a for a in data["acceptance"] if not a["match"]]
    print("-" * 74)
    print(f"{len(data['acceptance']) - len(bad)} of {len(data['acceptance'])} reproduce exactly.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--inputs", default=str(root / "inputs"))
    ap.add_argument("--outputs", default=str(root / "outputs"))
    ap.add_argument("--cache", default=str(root / "cache"))
    ap.add_argument("--offline", action="store_true",
                    help="use caches only; make no network calls")
    a = ap.parse_args()
    r = build(Path(a.inputs), Path(a.outputs), Path(a.cache), offline=a.offline)
    print_acceptance(r["data"])
