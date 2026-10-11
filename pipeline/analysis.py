"""
Phase 1 reliability analysis for the Prometheus School transport spine.

Stop-level AM timing uses the FIRST tap at a stop on a route-day as the proxy
for bus arrival. Two reasons, both material:

  1. Scan lag. The AM tap is recorded when the attendant reaches a seated
     child, not when the bus arrives. The first tap is the least-lagged
     observation available.
  2. Independence. Five children boarding at one stop on one morning are five
     records of a SINGLE bus arrival, not five observations. Treating them as
     five inflates n roughly fivefold and makes confidence intervals look far
     tighter than the evidence supports.

Per-student figures are computed alongside for continuity with earlier runs,
but the first-tap basis is what drives the corrected timetable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from transport_spine import MIN_OBS_FOR_STOP, minutes_to_clock

CLASS_SYSTEMATIC = "SYSTEMATIC"
CLASS_VOLATILE = "VOLATILE"
CLASS_OK = "OK"
CLASS_REVIEW = "REVIEW"


def _q(s, p):
    return s.quantile(p)


def _describe(g: pd.core.groupby.SeriesGroupBy) -> pd.DataFrame:
    return g.agg(
        n="count",
        median="median",
        p10=lambda s: _q(s, 0.10),
        p25=lambda s: _q(s, 0.25),
        p75=lambda s: _q(s, 0.75),
        p90=lambda s: _q(s, 0.90),
        std="std",
    )


def classify(median: float, iqr: float) -> str:
    if pd.isna(median) or pd.isna(iqr):
        return CLASS_REVIEW
    if abs(median) >= 5 and iqr <= 8:
        return CLASS_SYSTEMATIC
    if iqr > 12:
        return CLASS_VOLATILE
    if abs(median) < 5 and iqr <= 8:
        return CLASS_OK
    return CLASS_REVIEW


# --------------------------------------------------------------------------
# Bus-arrival trace: first tap per route-day-stop
# --------------------------------------------------------------------------

def bus_arrival_trace(spine: pd.DataFrame, regime_only: bool = True) -> pd.DataFrame:
    am = spine[
        (spine.leg == "am_pickup")
        & spine.scan_present
        & spine.passes_sanity
        & spine.promised_time_min.notna()
    ]
    if regime_only:
        am = am[am.in_regime]

    grp = am.groupby(["dominant_route_key", "stop_name", "date"], sort=False)
    trace = grp.agg(
        arrival_min=("actual_time_min", "min"),
        last_tap_min=("actual_time_min", "max"),
        boarders=("admission_no", "nunique"),
        promised_min=("promised_time_min", "first"),
    ).reset_index()
    trace["delta_min"] = trace.arrival_min - trace.promised_min
    trace["dwell_min"] = trace.last_tap_min - trace.arrival_min
    trace = trace.rename(columns={"dominant_route_key": "route_key"})
    return trace


# --------------------------------------------------------------------------
# 1. AM pickup deviation by stop
# --------------------------------------------------------------------------

def stop_deviation(trace: pd.DataFrame, min_n: int = MIN_OBS_FOR_STOP) -> pd.DataFrame:
    g = _describe(trace.groupby(["route_key", "stop_name"]).delta_min)
    g["iqr"] = g.p75 - g.p25
    g["classification"] = [classify(m, i) for m, i in zip(g["median"], g.iqr)]
    g.loc[g.n < min_n, "classification"] = "LOW_DATA"
    return g.reset_index().sort_values(["route_key", "median"], ascending=[True, False])


def stop_deviation_per_student(spine: pd.DataFrame, min_n: int = MIN_OBS_FOR_STOP) -> pd.DataFrame:
    """Legacy basis: one observation per student per day. Retained for comparison."""
    am = spine[
        (spine.leg == "am_pickup") & spine.scan_present & spine.passes_sanity
        & spine.promised_time_min.notna()
    ]
    g = _describe(am.groupby(["roster_am_route_key", "stop_name"]).delta_min)
    g["iqr"] = g.p75 - g.p25
    g["classification"] = [classify(m, i) for m, i in zip(g["median"], g.iqr)]
    g.loc[g.n < min_n, "classification"] = "LOW_DATA"
    return g.reset_index()


# --------------------------------------------------------------------------
# 2. Route scorecard
# --------------------------------------------------------------------------

def route_scorecard(spine: pd.DataFrame) -> pd.DataFrame:
    def leg_stats(df, prefix):
        d = df.delta_min
        return pd.Series(
            {
                f"{prefix}_n": d.count(),
                f"{prefix}_median": d.median(),
                f"{prefix}_p90": _q(d, 0.90),
                f"{prefix}_std": d.std(),
                f"{prefix}_pct_gt10_late": 100 * (d > 10).mean(),
                f"{prefix}_pct_gt20_late": 100 * (d > 20).mean(),
                f"{prefix}_pct_early_gt3": 100 * (d < -3).mean(),
            }
        )

    ok = spine.scan_present & spine.passes_sanity & spine.promised_time_min.notna()
    am = spine[ok & (spine.leg == "am_pickup")]
    pm = spine[ok & (spine.leg == "pm_dropoff")]

    a = am.groupby("route_key").apply(leg_stats, "am", include_groups=False)
    p = pm.groupby("route_key").apply(leg_stats, "pm", include_groups=False)
    out = a.join(p, how="outer")
    out["route_label"] = ["Route " + str(int(k[1:])) for k in out.index]
    return out.sort_values("am_median", ascending=False).reset_index()


# --------------------------------------------------------------------------
# 3. School arrival vs reporting time
# --------------------------------------------------------------------------

def school_arrival(spine: pd.DataFrame) -> pd.DataFrame:
    arr = spine[
        (spine.leg == "am_school_arrival") & spine.scan_present & spine.passes_sanity
    ]
    g = arr.groupby(["route_key", "date"]).agg(
        arrival_min=("actual_time_min", "max"),
        reporting_min=("promised_time_min", "first"),
        n_scans=("admission_no", "nunique"),
    ).reset_index()
    g["delta_min"] = g.arrival_min - g.reporting_min
    g["late"] = g.delta_min > 0
    return g


# --------------------------------------------------------------------------
# 4. Dispersal (PM)
# --------------------------------------------------------------------------

def pm_deviation(spine: pd.DataFrame) -> pd.DataFrame:
    pm = spine[
        (spine.leg == "pm_dropoff") & spine.scan_present & spine.passes_sanity
        & spine.promised_time_min.notna()
    ]
    g = _describe(pm.groupby(["dominant_route_key", "stop_name"]).delta_min)
    g.index = g.index.rename(["route_key", "stop_name"])
    g["iqr"] = g.p75 - g.p25
    g["classification"] = [classify(m, i) for m, i in zip(g["median"], g.iqr)]
    g.loc[g.n < MIN_OBS_FOR_STOP, "classification"] = "LOW_DATA"
    return g.reset_index()


def bell_to_rolling(spine: pd.DataFrame) -> pd.DataFrame:
    """Gap between first and last PM boarding tap on a route-day: bus idle time."""
    b = spine[
        (spine.leg == "pm_school_board") & spine.scan_present & spine.passes_sanity
    ]
    g = b.groupby(["route_key", "date"]).agg(
        first_board=("actual_time_min", "min"),
        last_board=("actual_time_min", "max"),
        n_students=("admission_no", "nunique"),
    ).reset_index()
    g["spread_min"] = g.last_board - g.first_board
    return g


# --------------------------------------------------------------------------
# 5. Ride duration and equity
# --------------------------------------------------------------------------

def ride_durations(spine: pd.DataFrame) -> pd.DataFrame:
    am = spine[spine.leg.isin(["am_pickup", "am_school_arrival"]) & spine.passes_sanity]
    w = am.pivot_table(
        index=["admission_no", "date"], columns="leg", values="actual_time_min",
        aggfunc="first",
    ).dropna()
    if w.empty:
        return pd.DataFrame()
    w["ride_min"] = w.am_school_arrival - w.am_pickup
    w = w.reset_index()

    meta = (
        spine[spine.leg == "am_pickup"]
        .groupby("admission_no")
        .agg(
            student_name=("student_name", "first"),
            grade=("grade", "first"),
            stop_name=("stop_name", "first"),
            route_key=("dominant_route_key", "first"),
        )
    )
    out = w.groupby("admission_no").ride_min.agg(
        n="count", median_ride="median", p90_ride=lambda s: _q(s, 0.90)
    ).join(meta)
    return out.reset_index().sort_values("median_ride", ascending=False)


# --------------------------------------------------------------------------
# 6. Stop and route load
# --------------------------------------------------------------------------

def load_profile(students: pd.DataFrame, spine: pd.DataFrame):
    stop_load = (
        students.groupby(["roster_am_route_key", "am_stop_name"])
        .admission_no.nunique().rename("students").reset_index()
        .rename(columns={"roster_am_route_key": "route_key", "am_stop_name": "stop_name"})
        .sort_values("students")
    )
    observed = (
        spine[spine.leg == "am_pickup"]
        .groupby("dominant_route_key").admission_no.nunique()
        .rename("students_observed")
    )
    route_load = (
        students.groupby("roster_am_route_key").admission_no.nunique()
        .rename("students_roster").to_frame()
        .join(observed).reset_index().rename(columns={"roster_am_route_key": "route_key"})
        .sort_values("students_roster")
    )
    return stop_load, route_load


# --------------------------------------------------------------------------
# 7. Temporal patterns
# --------------------------------------------------------------------------

def temporal(spine: pd.DataFrame):
    am = spine[
        (spine.leg == "am_pickup") & spine.scan_present & spine.passes_sanity
        & spine.promised_time_min.notna()
    ].copy()
    am["dow"] = am.date.dt.day_name()
    am["week"] = am.date.dt.to_period("W").dt.start_time

    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    dow = am.groupby("dow").delta_min.agg(n="count", mean="mean", median="median")
    dow = dow.reindex([d for d in order if d in dow.index]).reset_index()
    wk = am.groupby("week").delta_min.agg(
        n="count", mean="mean", median="median"
    ).reset_index()
    return dow, wk


# --------------------------------------------------------------------------
# 8. Stop dwell time (loading bottleneck)
# --------------------------------------------------------------------------

def dwell_profile(trace: pd.DataFrame, min_n: int = MIN_OBS_FOR_STOP) -> pd.DataFrame:
    """
    Spread between first and last tap at a stop. With multiple boarders this is
    a direct measure of how long the bus is stationary loading, and it is the
    one bottleneck measure the scan-lag caveat does not weaken.
    """
    multi = trace[trace.boarders > 1]
    if multi.empty:
        return pd.DataFrame()
    g = multi.groupby(["route_key", "stop_name"]).agg(
        n=("dwell_min", "count"),
        median_boarders=("boarders", "median"),
        median_dwell=("dwell_min", "median"),
        p90_dwell=("dwell_min", lambda s: _q(s, 0.90)),
        max_dwell=("dwell_min", "max"),
    ).reset_index()
    return g[g.n >= min_n].sort_values("median_dwell", ascending=False)


# --------------------------------------------------------------------------
# Scan compliance
# --------------------------------------------------------------------------

def scan_compliance(spine: pd.DataFrame):
    by_leg = spine.groupby("leg").scan_present.agg(
        student_days="count", present="sum"
    )
    by_leg["pct_present"] = 100 * by_leg.present / by_leg.student_days
    by_leg["pct_missing"] = 100 - by_leg.pct_present

    by_route = spine.pivot_table(
        index="route_key", columns="leg", values="scan_present", aggfunc="mean"
    ).mul(100).round(1)
    by_date = spine.pivot_table(
        index="date", columns="leg", values="scan_present", aggfunc="mean"
    ).mul(100).round(1)
    return by_leg.reset_index(), by_route.reset_index(), by_date.reset_index()


# --------------------------------------------------------------------------
# Corrected timetable
# --------------------------------------------------------------------------

def recommend_window(median: float, p10: float, p90: float, min_width: int = 5):
    """
    Centred on the observed median, as wide as the middle 80% of observations.

    Half-width is (p90 - p10) / 2, NOT max(median - p10, p90 - median). The
    latter takes the longer tail and mirrors it to both sides, which roughly
    doubles the window on any skewed stop - and pickup deltas are strongly
    right-skewed, because a bus can be an hour late but never an hour early.
    """
    if pd.isna(median):
        return (np.nan, np.nan)
    spread = p90 - p10
    half = 0.0 if pd.isna(spread) else spread / 2.0
    half = max(np.ceil(half), np.ceil(min_width / 2))
    return (median - half, median + half)


def build_timetable(
    stop_stats: pd.DataFrame,
    routes: pd.DataFrame,
    students: pd.DataFrame,
    trace: pd.DataFrame,
    spine: pd.DataFrame,
    min_n: int = MIN_OBS_FOR_STOP,
) -> pd.DataFrame:
    """
    One row per route-stop. Where n < min_n the published time is retained and
    marked low confidence rather than guessed at.
    """
    # Published times must be keyed on the route the bus ACTUALLY runs, not the
    # route the roster records. Where students moved mid-period, the roster key
    # points at a route that no longer carries them - keying on it produces
    # empty sheets for the live routes and phantom sheets for retired ones.
    am = spine[(spine.leg == "am_pickup") & spine.promised_time_min.notna()]
    published = (
        am.groupby(["dominant_route_key", "stop_name"])
        .agg(
            published_min=("promised_time_min", "median"),
            distinct_published=("promised_time_min", "nunique"),
        )
        .reset_index()
        .rename(columns={"dominant_route_key": "route_key"})
    )
    df = published.merge(stop_stats, on=["route_key", "stop_name"], how="left")

    rows = []
    for _, r in df.iterrows():
        n = 0 if pd.isna(r.get("n")) else int(r["n"])
        enough = n >= min_n
        cls = r.classification if enough else "LOW_DATA"
        if enough:
            lo, hi = recommend_window(r["median"] + r.published_min,
                                      r.p10 + r.published_min,
                                      r.p90 + r.published_min)
            rec_centre = r["median"] + r.published_min
            conf = "High" if n >= 25 else "Medium"
            if cls == "VOLATILE":
                conf = "Volatile - operational fix needed, not a timetable fix"
            elif cls == "REVIEW":
                conf = f"{conf} - mixed signal, review before publishing"
        else:
            lo = hi = rec_centre = np.nan
            conf = "Low - insufficient data, published time retained"
        if r.distinct_published > 1:
            conf += f" | {int(r.distinct_published)} published times in use at this stop"
        rows.append(
            {
                "route_key": r.route_key,
                "Stop": r.stop_name,
                "Current Published Pickup": minutes_to_clock(r.published_min),
                "Observed Median Pickup": minutes_to_clock(rec_centre) if enough else "",
                "Recommended Window": (
                    f"{minutes_to_clock(lo)} - {minutes_to_clock(hi)}" if enough
                    else minutes_to_clock(r.published_min)
                ),
                "Classification": cls,
                "n": n,
                "Confidence": conf,
                "_centre_min": rec_centre if enough else r.published_min,
                "_published_min": r.published_min,
                "_shift_min": (rec_centre - r.published_min) if enough else 0.0,
            }
        )
    return pd.DataFrame(rows)


def feasibility_check(
    timetable: pd.DataFrame, trace: pd.DataFrame, spine: pd.DataFrame
) -> pd.DataFrame:
    """
    Does the corrected timetable still let each bus make its reporting time?

    Travel allowance per stop is the observed median gap between that stop's
    bus-arrival tap and the bus's school-arrival tap on the same route-day, so
    it reflects what the bus actually achieves rather than a planning estimate.
    """
    arr = spine[
        (spine.leg == "am_school_arrival") & spine.scan_present & spine.passes_sanity
    ].groupby(["route_key", "date"]).agg(
        school_arrival=("actual_time_min", "max"),
        reporting=("promised_time_min", "first"),
    ).reset_index()

    t = trace.merge(arr, on=["route_key", "date"], how="inner")
    t["leg_to_school"] = t.school_arrival - t.arrival_min
    t = t[t.leg_to_school.between(0, 150)]
    travel = t.groupby(["route_key", "stop_name"]).leg_to_school.median().rename("travel_min")
    reporting = arr.groupby("route_key").reporting.first()

    tt = timetable.merge(travel, left_on=["route_key", "Stop"],
                         right_index=True, how="left")
    tt["implied_arrival"] = tt._centre_min + tt.travel_min
    tt["baseline_arrival"] = tt._published_min + tt.travel_min

    out = []
    for rk, g in tt.groupby("route_key"):
        rep = reporting.get(rk, np.nan)
        implied = g.implied_arrival.max()
        baseline = g.baseline_arrival.max()
        known = pd.notna(implied) and pd.notna(rep)
        slack = (rep - implied) if known else np.nan
        base_slack = (rep - baseline) if pd.notna(baseline) and pd.notna(rep) else np.nan
        if not known:
            status = "UNKNOWN - no matched arrival observations"
        elif slack >= 2:
            status = "OK"
        elif slack >= 0:
            status = "TIGHT - under 2 min spare"
        elif pd.notna(base_slack) and base_slack < 0:
            status = "FLAG - already infeasible before correction"
        else:
            status = "FLAG - correction pushes past reporting time"
        out.append(
            {
                "route_key": rk,
                "route_label": "Route " + str(int(rk[1:])),
                "reporting_time": minutes_to_clock(rep),
                "implied_arrival": minutes_to_clock(implied) if known else "",
                "slack_min": slack,
                "slack_before_correction_min": base_slack,
                "stops_shifted_later": int((g._shift_min > 0).sum()),
                "max_shift_min": g._shift_min.max(),
                "status": status,
            }
        )
    return pd.DataFrame(out).sort_values("slack_min", na_position="last")


def build_pm_timetable(
    pm_stats: pd.DataFrame, spine: pd.DataFrame, min_n: int = MIN_OBS_FOR_STOP
) -> pd.DataFrame:
    pm = spine[(spine.leg == "pm_dropoff") & spine.promised_time_min.notna()]
    published = (
        pm.groupby(["dominant_route_key", "stop_name"])
        .agg(
            published_min=("promised_time_min", "median"),
            distinct_published=("promised_time_min", "nunique"),
        )
        .reset_index()
        .rename(columns={"dominant_route_key": "route_key"})
    )
    df = published.merge(pm_stats, on=["route_key", "stop_name"], how="left")

    rows = []
    for _, r in df.iterrows():
        n = 0 if pd.isna(r.get("n")) else int(r["n"])
        enough = n >= min_n
        cls = r.classification if enough else "LOW_DATA"
        if enough:
            centre = r["median"] + r.published_min
            lo, hi = recommend_window(centre, r.p10 + r.published_min, r.p90 + r.published_min)
            conf = "High" if n >= 25 else "Medium"
            if cls == "VOLATILE":
                conf = "Volatile - operational fix needed, not a timetable fix"
        else:
            centre = lo = hi = np.nan
            conf = "Low - insufficient data, published time retained"
        if r.distinct_published > 1:
            conf += f" | {int(r.distinct_published)} published times in use at this stop"
        rows.append(
            {
                "route_key": r.route_key,
                "Stop": r.stop_name,
                "Current Published Drop": minutes_to_clock(r.published_min),
                "Observed Median Drop": minutes_to_clock(centre) if enough else "",
                "Recommended Window": (
                    f"{minutes_to_clock(lo)} - {minutes_to_clock(hi)}" if enough
                    else minutes_to_clock(r.published_min)
                ),
                "Classification": cls,
                "n": n,
                "Confidence": conf,
            }
        )
    return pd.DataFrame(rows)
