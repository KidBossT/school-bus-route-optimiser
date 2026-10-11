"""
The hand-off between whoever collects coordinates in Google Maps and the
optimiser.

  export_template  writes one row per physical location the buses use -
                   pickup and drop halves of a combined cell are separate rows -
                   with the geocoder's current guess, so the person filling it
                   in can paste over only what is wrong.
  import_file      reads the filled-in sheet back and writes each row into
                   geocode_overrides.json, which makes it "verified" for the
                   dashboard and the optimiser alike. The old overrides file is
                   backed up first.

A Google Maps right-click gives "28.57012, 77.39105"; paste that into the
`google_maps` column as-is, or fill `lat` and `lon` separately.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from pathlib import Path

import pandas as pd

import geo

from .build_problem import SCHOOL_KEY, SCHOOL_QUERY, location_key

COLUMNS = ["key", "stop_text", "leg", "routes", "children", "status",
           "guess_lat", "guess_lon", "guess_label", "google_maps", "lat", "lon", "note"]
_PAIR = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def export_template(routes: pd.DataFrame, students: pd.DataFrame, gc: geo.Geocoder,
                    path: Path) -> int:
    rows = {}

    def add(key, text, leg, route, kids, direction):
        r = rows.setdefault(key, {"key": key, "stop_text": text, "leg": leg,
                                  "routes": set(), "by_dir": {}})
        if route:
            r["routes"].add(route)
        r["by_dir"][direction] = r["by_dir"].get(direction, 0) + kids

    add(SCHOOL_KEY, SCHOOL_QUERY, "school", None, 0, "school")
    for direction, rcol, scol in (("am", "roster_am_route_key", "am_stop_name"),
                                  ("pm", "roster_pm_route_key", "pm_stop_name")):
        for (rk, stop), g in students.dropna(subset=[rcol]).groupby([rcol, scol]):
            key, text = location_key(stop, direction)
            add(key, text, "pickup+drop" if key == stop else direction, rk, len(g), direction)
        # printed stops nobody rides today still need a point for future plans
        for r in routes.itertuples():
            key, text = location_key(r.stop_name, direction)
            add(key, text, "pickup+drop" if key == r.stop_name else direction, r.route_key, 0, direction)

    out = []
    for r in rows.values():
        rec = gc.resolve(r["stop_text"], override_key=r["key"])
        by_dir = r.pop("by_dir")
        out.append({**r, "routes": " ".join(sorted(r["routes"])),
                    "children": max(by_dir.values()),       # not am + pm: same children
                    "status": "verified" if rec.get("verified") else
                              ("guess" if rec.get("lat") is not None else "missing"),
                    "guess_lat": rec.get("lat"), "guess_lon": rec.get("lon"),
                    "guess_label": rec.get("label", ""),
                    "google_maps": "", "lat": "", "lon": "", "note": ""})
    df = pd.DataFrame(out, columns=COLUMNS)
    # missing first, then the stops that carry the most children
    df["_o"] = df.status.map({"missing": 0, "guess": 1, "verified": 2})
    df = df.sort_values(["_o", "children"], ascending=[True, False]).drop(columns="_o")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return len(df)


def import_file(path: Path, cache_dir: Path, source: str = "google maps") -> dict:
    df = (pd.read_excel(path, dtype=str) if path.suffix.lower() in (".xlsx", ".xls")
          else pd.read_csv(path, dtype=str, encoding="utf-8-sig")).fillna("")
    if "key" not in df.columns:
        raise ValueError(f"{path} has no 'key' column - start from the exported template")
    ov_path = Path(cache_dir) / "geocode_overrides.json"
    raw = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    stats = {"imported": 0, "skipped_blank": 0, "rejected": []}
    today = dt.date.today().isoformat()
    for r in df.itertuples():
        lat, lon = str(getattr(r, "lat", "")).strip(), str(getattr(r, "lon", "")).strip()
        m = _PAIR.match(str(getattr(r, "google_maps", "")))
        if m:
            lat, lon = m.group(1), m.group(2)
        if not lat or not lon:
            stats["skipped_blank"] += 1
            continue
        try:
            la, lo = float(lat), float(lon)
        except ValueError:
            stats["rejected"].append((r.key, "not a number"))
            continue
        if not geo.in_bbox(la, lo):
            # the commonest paste error is lat and lon swapped
            stats["rejected"].append((r.key, f"{la},{lo} is outside the NCR - swapped?"))
            continue
        note = str(getattr(r, "note", "")).strip()
        raw[r.key] = {"lat": round(la, 6), "lon": round(lo, 6),
                      "label": str(getattr(r, "stop_text", "")),
                      "note": note or f"{source}, imported {today}"}
        stats["imported"] += 1
    if ov_path.exists():
        shutil.copy2(ov_path, ov_path.with_suffix(f".backup-{dt.datetime.now():%Y%m%d-%H%M%S}.json"))
    ov_path.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    return stats
