"""
Prometheus School Noida - Transport data spine (Phase 0).

Builds a tidy long-format table from three monthly Excel exports:
  1. Route list (stops + promised times per route)
  2. Student roster (student -> route, stop, promised times, reporting time)
  3. RFID attendance logs (per-student tap times, four legs per day)

Design constraints:
  - Reruns end to end on a fresh monthly export with no manual edits.
  - No hardcoded stop names, route names or dates in any logic.
  - Every filter and exclusion is logged with a count.

MEASUREMENT CAVEAT (set by the transport head, 2026-09):
  The AM boarding tap is recorded when the attendant scans a child who is
  already seated, not when the bus reaches the stop. AM boarding timestamps
  therefore carry an unknown positive lag. Consequences, applied throughout:
    - AM lateness is an UPPER BOUND on true bus lateness.
    - AM ride duration is a LOWER BOUND on true ride time.
    - The first tap at a stop is the least-lagged observation available, so
      stop-level bus-arrival timing uses the first tap, not per-student taps.
  The other three legs (school arrival, PM board, PM drop) are taken as accurate.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

RFID_HEADER_ROW = 7          # 0-indexed; real header sits on Excel row 8
AM_DELTA_WINDOW = (-90, 120)  # minutes
PM_DELTA_WINDOW = (-90, 180)
TAP_WINDOW_MIN = (5 * 60, 20 * 60)   # 05:00 - 20:00
MAX_RIDE_MIN = 150
MIN_OBS_FOR_STOP = 15

LEGS = ["am_pickup", "am_school_arrival", "pm_school_board", "pm_dropoff"]

SPINE_COLUMNS = [
    "date", "route_key", "route_label", "vehicle_number", "admission_no",
    "student_name", "grade", "stop_name", "leg", "promised_time_min",
    "actual_time_min", "delta_min", "scan_present",
]


# --------------------------------------------------------------------------
# Audit log - every exclusion is recorded here
# --------------------------------------------------------------------------

@dataclass
class AuditLog:
    entries: list = field(default_factory=list)

    def record(self, stage: str, action: str, count: int, detail: str = "") -> None:
        self.entries.append(
            {"stage": stage, "action": action, "rows": int(count), "detail": detail}
        )

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.entries, columns=["stage", "action", "rows", "detail"])

    def __str__(self) -> str:
        return self.to_frame().to_string(index=False)


# --------------------------------------------------------------------------
# Normalisation primitives
# --------------------------------------------------------------------------

def clean_text(value) -> str:
    """Strip non-breaking spaces and collapse whitespace. '' for missing."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    s = str(value)
    s = unicodedata.normalize("NFKC", s)          # turns \xa0 into a plain space
    s = s.replace("\xa0", " ").replace("\u200b", "")
    s = re.sub(r"\s+", " ", s).strip()
    return "" if s.lower() in {"nan", "none"} else s


def is_missing_marker(value) -> bool:
    """Missing values in the RFID export are a dash padded with \\xa0."""
    return clean_text(value) in {"", "-", "--", "N/A", "NA"}


_TIME_HM = re.compile(r"^(\d{1,2})[:.\s](\d{2})\s*([APap]\.?[Mm]\.?)?$")
_TIME_HMS = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")


def parse_time_to_minutes(value) -> float:
    """
    Minutes since midnight, or NaN.

    Handles every format observed across the three files:
      '06:55 AM'   RFID logs
      '6:30am'     route list / roster
      '3.20pm'     dot separator (24 roster cells, 7 route-list cells)
      '15:45:00'   24h with seconds
      datetime.time / pandas Timestamp (Excel may type a cell as a time)
    Cross-reference text such as 'Pickup R-15' or 'Drop only' returns NaN;
    those are handled separately as split AM/PM assignments.
    """
    if value is None:
        return np.nan
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return float(value.hour * 60 + value.minute)

    s = clean_text(value)
    if not s or is_missing_marker(s):
        return np.nan

    m = _TIME_HMS.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return float(h * 60 + mi) if 0 <= h <= 23 and 0 <= mi <= 59 else np.nan

    m = _TIME_HM.match(s)
    if not m:
        return np.nan
    h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if mi > 59:
        return np.nan
    if ampm:
        ap = ampm.lower().replace(".", "")
        if h > 12:
            return np.nan
        if ap == "pm" and h != 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
    if not (0 <= h <= 23):
        return np.nan
    return float(h * 60 + mi)


def minutes_to_clock(minutes) -> str:
    """Inverse of parse_time_to_minutes, for human-readable output."""
    if minutes is None or (isinstance(minutes, float) and np.isnan(minutes)):
        return ""
    total = int(round(float(minutes))) % (24 * 60)
    h, mi = divmod(total, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mi:02d} {suffix}"


_ROUTE_NUM = re.compile(r"(\d+)")


def canonical_route_key(value) -> str | None:
    """
    'ROUTE # 1', 'Route 1', 'Route23', '7' -> 'R01', 'R23', 'R07'.
    Non-operational markers such as 'Testing' return None.
    """
    s = clean_text(value)
    if not s:
        return None
    if not re.search(r"\d", s):
        return None                      # 'Testing', 'Test' and similar
    if re.search(r"\btest", s, re.I):
        return None
    m = _ROUTE_NUM.search(s)
    if not m:
        return None
    return f"R{int(m.group(1)):02d}"


def route_label(route_key: str | None) -> str:
    return "" if not route_key else f"Route {int(route_key[1:])}"


_CROSSREF = re.compile(r"(pickup|drop)\s*(only|in)?\s*(r[-\s]?\d+)?", re.I)


def parse_crossref(value) -> str | None:
    """
    Roster time cells sometimes carry an assignment note instead of a time:
      'Pickup R-15', 'Pickup in R7', 'Drop in R26', 'Drop only', 'Pickup only'
    Returns the referenced canonical route key, or 'SPLIT' when the note marks
    the row as one half of a split assignment without naming the other route.
    """
    s = clean_text(value)
    if not s or parse_time_to_minutes(s) == parse_time_to_minutes(s) and not np.isnan(
        parse_time_to_minutes(s)
    ):
        return None
    if not _CROSSREF.search(s):
        return None
    key = canonical_route_key(s)
    return key or "SPLIT"


# --------------------------------------------------------------------------
# File 1 - route list (block structure, 'ROUTE # N' marker in column 1)
# --------------------------------------------------------------------------

def parse_route_list(path: str | Path, audit: AuditLog | None = None) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None, dtype=object)
    current: str | None = None
    rows: list[dict] = []
    seq = 0

    for _, r in raw.iterrows():
        cells = [clean_text(r.get(i)) for i in range(min(4, raw.shape[1]))]
        c0, c1 = cells[0], cells[1] if len(cells) > 1 else ""

        marker = next((c for c in cells if re.match(r"^ROUTE\s*#\s*\d+", c, re.I)), "")
        if marker:
            current = canonical_route_key(marker)
            seq = 0
            continue
        if re.match(r"^s\.?no\.?$", c0, re.I) or c1.lower() == "bus stop":
            continue
        if not c0.replace(".", "").isdigit() or not c1:
            continue
        seq += 1
        rows.append(
            {
                "route_key": current,
                "route_label": route_label(current),
                "stop_seq": seq,
                "stop_name": c1,
                "promised_pickup_min": parse_time_to_minutes(cells[2] if len(cells) > 2 else None),
                "promised_drop_min": parse_time_to_minutes(cells[3] if len(cells) > 3 else None),
                "pickup_raw": cells[2] if len(cells) > 2 else "",
                "drop_raw": cells[3] if len(cells) > 3 else "",
            }
        )

    df = pd.DataFrame(rows)
    if audit is not None:
        audit.record("parse_route_list", "stop rows parsed", len(df))
        audit.record(
            "parse_route_list", "stops with unparseable pickup time",
            int(df.promised_pickup_min.isna().sum()),
            "cross-reference notes or malformed values",
        )
        audit.record(
            "parse_route_list", "stops with unparseable drop time",
            int(df.promised_drop_min.isna().sum()),
        )
    return df


# --------------------------------------------------------------------------
# File 2 - student roster (block structure, 'ROUTE # N' marker in column 0)
# --------------------------------------------------------------------------

_ADM = re.compile(r"^PRO\d+", re.I)
_REPORTING = re.compile(
    r"Bus\s+Reporting\s+Time.*?:\s*(\d{1,2}[:.]\d{2}\s*[APap]\.?[Mm]\.?)", re.I
)


def parse_roster(path: str | Path, audit: AuditLog | None = None) -> pd.DataFrame:
    """
    Long roster: one row per (student, route block) as printed. Students who are
    picked up by one route and dropped by another appear twice; that is resolved
    downstream in build_student_dim.
    """
    raw = pd.read_excel(path, header=None, dtype=object)
    current: str | None = None
    reporting: float = np.nan
    rows: list[dict] = []

    for _, r in raw.iterrows():
        cells = [clean_text(r.get(i)) for i in range(raw.shape[1])]
        joined = " ".join(cells)

        marker = next((c for c in cells if re.match(r"^ROUTE\s*#\s*\d+", c, re.I)), "")
        if marker:
            current = canonical_route_key(marker)
            reporting = np.nan
            continue

        m = _REPORTING.search(joined)
        if m:
            reporting = parse_time_to_minutes(m.group(1))
            continue

        adm = next((c for c in cells if _ADM.match(c)), "")
        if not adm:
            continue
        i = cells.index(adm)
        get = lambda k: cells[i + k] if i + k < len(cells) else ""

        rows.append(
            {
                "route_key": current,
                "route_label": route_label(current),
                "reporting_time_min": reporting,
                "admission_no": adm.upper(),
                "student_name": get(1),
                "grade": get(2),
                "stop_name": get(3),
                "promised_pickup_min": parse_time_to_minutes(get(4)),
                "promised_drop_min": parse_time_to_minutes(get(5)),
                "pickup_crossref": parse_crossref(get(4)),
                "drop_crossref": parse_crossref(get(5)),
            }
        )

    df = pd.DataFrame(rows)
    if audit is not None:
        audit.record("parse_roster", "roster rows parsed", len(df))
        audit.record("parse_roster", "distinct students", df.admission_no.nunique())
        audit.record(
            "parse_roster", "rows with a cross-reference note instead of a time",
            int((df.pickup_crossref.notna() | df.drop_crossref.notna()).sum()),
            "split AM/PM route assignments",
        )
        audit.record(
            "parse_roster", "rows missing a reporting time",
            int(df.reporting_time_min.isna().sum()),
        )
    return df


def build_student_dim(roster: pd.DataFrame, audit: AuditLog | None = None) -> pd.DataFrame:
    """
    One row per student. Resolves split AM/PM assignments: the AM route is the
    block carrying a real pickup time, the PM route the block carrying a real
    drop time. Falls back to the student's only block when there is just one.
    """
    out: list[dict] = []
    split_count = 0

    for adm, g in roster.groupby("admission_no", sort=False):
        am = g[g.promised_pickup_min.notna()]
        pm = g[g.promised_drop_min.notna()]
        am_row = am.iloc[0] if len(am) else g.iloc[0]
        pm_row = pm.iloc[0] if len(pm) else g.iloc[0]
        if len(g) > 1:
            split_count += 1

        rep = g.reporting_time_min.dropna()
        out.append(
            {
                "admission_no": adm,
                "student_name": am_row.student_name or pm_row.student_name,
                "grade": am_row.grade or pm_row.grade,
                "roster_am_route_key": am_row.route_key,
                "am_stop_name": am_row.stop_name,
                "promised_pickup_min": am_row.promised_pickup_min,
                "roster_pm_route_key": pm_row.route_key,
                "pm_stop_name": pm_row.stop_name,
                "promised_drop_min": pm_row.promised_drop_min,
                "reporting_time_min": float(rep.iloc[0]) if len(rep) else np.nan,
                "split_am_pm_route": len(g) > 1,
                "roster_blocks": len(g),
            }
        )

    df = pd.DataFrame(out)
    if audit is not None:
        audit.record("build_student_dim", "distinct students", len(df))
        audit.record(
            "build_student_dim", "students with split AM/PM routes", split_count,
            "picked up by one route, dropped by another - not duplicates",
        )
        audit.record(
            "build_student_dim", "students with no parseable pickup time",
            int(df.promised_pickup_min.isna().sum()),
        )
        audit.record(
            "build_student_dim", "students with no parseable drop time",
            int(df.promised_drop_min.isna().sum()),
        )
    return df


# --------------------------------------------------------------------------
# File 3 - RFID attendance logs
# --------------------------------------------------------------------------

LEG_SOURCE = {
    "am_pickup": "Boarding Time",
    "am_school_arrival": "School Arrival Time",
    "pm_school_board": "Disparsal Boarding Time",
    "pm_dropoff": "Disparsal De-Boarding Time",
}


def parse_logs(path: str | Path, audit: AuditLog | None = None) -> pd.DataFrame:
    df = pd.read_excel(path, header=RFID_HEADER_ROW, dtype=object)
    df.columns = [clean_text(c) for c in df.columns]
    n_raw = len(df)
    if audit is not None:
        audit.record("parse_logs", "raw log rows", n_raw)

    df["route_raw"] = df["Route Name"].map(clean_text)
    df["route_key"] = df["route_raw"].map(canonical_route_key)

    dropped = df[df.route_key.isna()]
    if audit is not None:
        audit.record(
            "parse_logs", "rows dropped: non-operational route label",
            len(dropped),
            f"labels: {sorted(dropped.route_raw.unique())}",
        )
    df = df[df.route_key.notna()].copy()

    df["route_label"] = df.route_key.map(route_label)
    df["vehicle_number"] = df["Vehicle Number"].map(clean_text)
    df["admission_no"] = df["Admission No."].map(lambda v: clean_text(v).upper())
    df["student_name_log"] = df["Student Name"].map(clean_text)
    df["date"] = pd.to_datetime(
        df["Attendance Date"].map(clean_text), format="%d-%m-%Y", errors="coerce"
    )

    bad_dates = int(df.date.isna().sum())
    if bad_dates and audit is not None:
        audit.record("parse_logs", "rows dropped: unparseable date", bad_dates)
    df = df[df.date.notna()].copy()

    for leg, col in LEG_SOURCE.items():
        df[leg] = df[col].map(
            lambda v: np.nan if is_missing_marker(v) else parse_time_to_minutes(v)
        )

    if audit is not None:
        audit.record("parse_logs", "clean log rows", len(df))
        audit.record("parse_logs", "distinct students in logs", df.admission_no.nunique())
        audit.record("parse_logs", "distinct routes in logs", df.route_key.nunique())
        audit.record("parse_logs", "distinct dates in logs", df.date.nunique())
        for leg in LEGS:
            audit.record(
                "parse_logs", f"scans present: {leg}", int(df[leg].notna().sum()),
                f"{100 * df[leg].notna().mean():.1f}% of {len(df)} rows",
            )
    return df


# --------------------------------------------------------------------------
# Observed route regime
# --------------------------------------------------------------------------

def attach_observed_route(logs: pd.DataFrame, audit: AuditLog | None = None) -> pd.DataFrame:
    """
    Route assignments changed during the observation window, so the roster (a
    single current snapshot) disagrees with the logs for some students. The
    dominant observed route - the route a student actually travels on most
    often - is the operational truth. Rows on any other route are out-of-regime
    and are excluded from timetable derivation.

    Fully data-driven: no route names or change dates are hardcoded, so this
    re-derives itself on next month's export.
    """
    df = logs.copy()
    modal = (
        df.groupby(["admission_no", "route_key"]).size()
        .rename("n").reset_index()
        .sort_values(["admission_no", "n"], ascending=[True, False])
        .drop_duplicates("admission_no")
        .set_index("admission_no")["route_key"]
    )
    df["dominant_route_key"] = df.admission_no.map(modal)
    df["in_regime"] = df.route_key == df.dominant_route_key

    if audit is not None:
        movers = df.groupby("admission_no").route_key.nunique()
        audit.record(
            "attach_observed_route", "students seen on more than one route",
            int((movers > 1).sum()),
            "mid-period route restructure",
        )
        audit.record(
            "attach_observed_route", "rows out of regime (non-dominant route)",
            int((~df.in_regime).sum()),
            f"{100 * (~df.in_regime).mean():.1f}% - excluded from timetable derivation only",
        )
    return df


# --------------------------------------------------------------------------
# Spine
# --------------------------------------------------------------------------

def build_spine(
    logs: pd.DataFrame, students: pd.DataFrame, audit: AuditLog | None = None
) -> pd.DataFrame:
    """One row per student per date per leg."""
    j = logs.merge(students, on="admission_no", how="left", validate="many_to_one")

    unmatched = j[j.student_name.isna()]
    if audit is not None:
        audit.record(
            "build_spine", "log rows not matching any roster student", len(unmatched),
            f"students: {sorted(unmatched.admission_no.unique())}",
        )
        audit.record(
            "build_spine", "log rows joined to a roster stop",
            int(j.student_name.notna().sum()),
            f"{100 * j.student_name.notna().mean():.2f}%",
        )

    promised = {
        "am_pickup": "promised_pickup_min",
        "am_school_arrival": "reporting_time_min",
        "pm_school_board": None,
        "pm_dropoff": "promised_drop_min",
    }
    stop_col = {
        "am_pickup": "am_stop_name",
        "am_school_arrival": None,
        "pm_school_board": None,
        "pm_dropoff": "pm_stop_name",
    }

    frames = []
    for leg in LEGS:
        f = pd.DataFrame(
            {
                "date": j.date,
                "route_key": j.route_key,
                "route_label": j.route_label,
                "vehicle_number": j.vehicle_number,
                "admission_no": j.admission_no,
                "student_name": j.student_name.fillna(j.student_name_log),
                "grade": j.grade,
                "stop_name": j[stop_col[leg]] if stop_col[leg] else "SCHOOL",
                "leg": leg,
                "promised_time_min": j[promised[leg]] if promised[leg] else np.nan,
                "actual_time_min": j[leg],
                "dominant_route_key": j.dominant_route_key,
                "in_regime": j.in_regime,
                "roster_am_route_key": j.roster_am_route_key,
            }
        )
        frames.append(f)

    spine = pd.concat(frames, ignore_index=True)
    spine["delta_min"] = spine.actual_time_min - spine.promised_time_min
    spine["scan_present"] = spine.actual_time_min.notna()

    if audit is not None:
        audit.record("build_spine", "spine rows (students x dates x 4 legs)", len(spine))
    return spine


# --------------------------------------------------------------------------
# Quarantine and sanity filters
# --------------------------------------------------------------------------

def quarantine(spine: pd.DataFrame, audit: AuditLog | None = None):
    """
    Split implausible observations out rather than deleting them.
    Returns (clean_spine, exceptions).
    """
    s = spine.copy()
    s["exception_reason"] = ""

    def flag(mask, reason):
        hit = mask & (s.exception_reason == "")
        s.loc[hit, "exception_reason"] = reason
        if audit is not None:
            audit.record("quarantine", f"flagged: {reason}", int(hit.sum()))

    outside = s.actual_time_min.notna() & ~s.actual_time_min.between(*TAP_WINDOW_MIN)
    flag(outside, "tap outside 05:00-20:00")

    # ride duration is a per-student-day property of the AM legs
    wide = s[s.leg.isin(["am_pickup", "am_school_arrival"])].pivot_table(
        index=["admission_no", "date"], columns="leg", values="actual_time_min",
        aggfunc="first",
    )
    if {"am_pickup", "am_school_arrival"}.issubset(wide.columns):
        ride = wide.am_school_arrival - wide.am_pickup
        bad_neg = set(ride[ride < 0].index)
        bad_long = set(ride[ride > MAX_RIDE_MIN].index)
        idx = pd.MultiIndex.from_arrays([s.admission_no, s.date])
        flag(pd.Series(idx.isin(bad_neg), index=s.index) & s.leg.str.startswith("am_"),
             "negative AM ride duration")
        flag(pd.Series(idx.isin(bad_long), index=s.index) & s.leg.str.startswith("am_"),
             f"AM ride duration over {MAX_RIDE_MIN} min")

    exceptions = s[s.exception_reason != ""].copy()
    clean = s[s.exception_reason == ""].drop(columns=["exception_reason"]).copy()
    if audit is not None:
        audit.record("quarantine", "rows quarantined in total", len(exceptions))
        audit.record("quarantine", "rows retained", len(clean))
    return clean, exceptions


def apply_sanity_filter(spine: pd.DataFrame, audit: AuditLog | None = None) -> pd.DataFrame:
    """Restrict to analysable deltas. AM and PM use different windows."""
    s = spine.copy()
    is_am = s.leg.str.startswith("am_")
    lo = np.where(is_am, AM_DELTA_WINDOW[0], PM_DELTA_WINDOW[0])
    hi = np.where(is_am, AM_DELTA_WINDOW[1], PM_DELTA_WINDOW[1])

    has_delta = s.delta_min.notna()
    keep = ~has_delta | ((s.delta_min >= lo) & (s.delta_min <= hi))

    if audit is not None:
        for label, mask in [("AM", is_am), ("PM", ~is_am)]:
            drop = int((has_delta & ~keep & mask).sum())
            tot = int((has_delta & mask).sum())
            window = AM_DELTA_WINDOW if label == "AM" else PM_DELTA_WINDOW
            audit.record(
                "sanity_filter", f"{label} deltas dropped outside {window}", drop,
                f"of {tot} {label} deltas ({100 * drop / tot:.2f}%)" if tot else "",
            )
    s["passes_sanity"] = keep
    return s


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def build_all(route_list_path, roster_path, logs_path):
    audit = AuditLog()
    routes = parse_route_list(route_list_path, audit)
    roster = parse_roster(roster_path, audit)
    students = build_student_dim(roster, audit)
    logs = parse_logs(logs_path, audit)
    logs = attach_observed_route(logs, audit)
    spine = build_spine(logs, students, audit)
    spine, exceptions = quarantine(spine, audit)
    spine = apply_sanity_filter(spine, audit)
    return {
        "routes": routes,
        "roster": roster,
        "students": students,
        "logs": logs,
        "spine": spine,
        "exceptions": exceptions,
        "audit": audit,
    }
