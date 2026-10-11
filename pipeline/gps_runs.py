"""
Read a bus run logged on a phone (GPX) and turn it into ground truth.

    python pipeline/gps_runs.py "Self Tracking R11.gpx" --route 11 --session am
    python pipeline/gps_runs.py run.gpx --route 11 --session am --apply-pins

The phone logs a continuous track and the person on the bus taps once at each
stop (a waypoint: "Add waypoint" in Open GPX Tracker, Annotate in GPSLogger).
From that this script measures, per stop:

  arrival    the bus came within STOP_RADIUS_M of where it stood at the tap
  departure  the bus left that circle again
  dwell      departure - arrival
  position   median of where the bus stood while stopped (the real pickup point)

Taps are matched to the route's printed stops in order (stops may be skipped,
logging may start part-way down the route) by the smallest total distance to
the planner's pins. A morning tap near school is the school arrival; an
afternoon tap near school is the departure.

Writes cache/gps_runs.json (one entry per file; re-running a file replaces its
entry). build_routes.py feeds these legs into the calibration. --apply-pins also
writes each measured stop position into cache/geocode_overrides.json, which
makes it a verified pin everywhere; several runs of the same stop are averaged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo

IST = dt.timedelta(hours=5, minutes=30)
STOP_RADIUS_M = 25        # inside this of the tap position the bus counts as at the stop
SCHOOL_RADIUS_M = 600     # a tap this close to the school pin is the school end of the run
SKIP_PENALTY_M = 1500     # cost of leaving a tap unmatched in the sequence alignment
MATCH_MAX_M = 1500        # a tap further than this from its matched pin is reported, not trusted
MOVING_KMH = 10           # a tap made faster than this was probably not at a stop
REPEAT_TAP_M = 30         # a tap this close to the previous one is the same stop tapped twice


def _hav_m(a, b, c, d) -> float:
    return geo.haversine_km(a, b, c, d) * 1000.0


def _time(s: str) -> dt.datetime:
    """GPX time (UTC) -> naive IST."""
    t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t + IST


def read_gpx(path: Path) -> tuple[list, list, str]:
    """(track [(lat, lon, t)], taps [(lat, lon, t)], creator)."""
    root = ET.parse(path).getroot()
    ns = {"g": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {"g": ""}
    q = (lambda tag: f"g:{tag}") if ns["g"] else (lambda tag: tag)

    def pt(e):
        t = e.find(q("time"), ns)
        return (float(e.get("lat")), float(e.get("lon")), _time(t.text)) if t is not None else None

    track = [p for e in root.iterfind(f".//{q('trkpt')}", ns) if (p := pt(e))]
    # stop taps: Open GPX Tracker "Add waypoint" and GPSLogger annotations are both <wpt>
    # (GPSLogger writes it at the next fix after the tap)
    taps = [p for e in root.iterfind(q("wpt"), ns) if (p := pt(e))]
    track.sort(key=lambda p: p[2])
    taps = sorted(set(taps), key=lambda p: p[2])
    return track, taps, root.get("creator", "")


def per_second(track):
    """Track resampled to one point a second (phones skip points while standing still)."""
    t0 = track[0][2]
    T = np.array([(p[2] - t0).total_seconds() for p in track])
    keep = np.concatenate([[True], np.diff(T) > 0])
    T = T[keep]
    la = np.array([p[0] for p in track])[keep]
    lo = np.array([p[1] for p in track])[keep]
    sec = np.arange(0, int(T[-1]) + 1)
    return t0, sec, np.interp(sec, T, la), np.interp(sec, T, lo)


def stop_events(track, taps) -> list[dict]:
    t0, sec, la, lo = per_second(track)
    lat_r, lon_r = np.radians(la), np.radians(lo)
    out = []
    for lat, lon, t in taps:
        dist = 2 * 6371000 * np.arcsin(np.sqrt(
            np.sin((lat_r - math.radians(lat)) / 2) ** 2
            + math.cos(math.radians(lat)) * np.cos(lat_r) * np.sin((lon_r - math.radians(lon)) / 2) ** 2))
        i = int(min(max((t - t0).total_seconds(), 0), sec[-1]))
        a = i
        while a > 0 and dist[a - 1] < STOP_RADIUS_M:
            a -= 1
        b = i
        while b < sec[-1] and dist[b + 1] < STOP_RADIUS_M:
            b += 1
        j, k = max(i - 5, 0), min(i + 5, int(sec[-1]))
        moved = sum(_hav_m(la[x], lo[x], la[x + 1], lo[x + 1]) for x in range(j, k))
        kmh = moved / max(k - j, 1) * 3.6
        at = lambda s: t0 + dt.timedelta(seconds=int(s))
        out.append({
            "tap": t, "arr": at(a) if a > 0 else None,            # logging began here
            "dep": at(b) if b < sec[-1] else None,                 # logging ended here
            "lat": float(np.median(la[a:b + 1])), "lon": float(np.median(lo[a:b + 1])),
            "moving_at_tap": kmh > MOVING_KMH,
        })
    return out


def align(events, pins) -> list[int | None]:
    """
    Match taps to pins in order (both lists in driving order). Each tap takes a
    later pin than the one before, or is left unmatched at SKIP_PENALTY_M.
    """
    m, n = len(events), len(pins)
    INF = float("inf")
    cost = [[_hav_m(e["lat"], e["lon"], p["lat"], p["lon"]) for p in pins] for e in events]
    # best[j][s]: cheapest matching of taps[:j] with the last used pin < s
    best = [[INF] * (n + 1) for _ in range(m + 1)]
    back = [[None] * (n + 1) for _ in range(m + 1)]
    for s in range(n + 1):
        best[0][s] = 0.0
    for j in range(1, m + 1):
        for s in range(n + 1):
            opts = [(best[j - 1][s] + SKIP_PENALTY_M, ("skip", s))]
            if s > 0:
                opts.append((best[j][s - 1], ("carry", s - 1)))
                opts.append((best[j - 1][s - 1] + cost[j - 1][s - 1], ("use", s - 1)))
            best[j][s], back[j][s] = min(opts, key=lambda o: o[0])
    match: list[int | None] = [None] * m
    j, s = m, n
    while j > 0:
        kind, s2 = back[j][s]
        if kind == "use":
            match[j - 1] = s2
            j -= 1
        elif kind == "skip":
            j -= 1
        s = s2
    return match


def planner_pins(outputs: Path, route: int, session: str) -> tuple[list[dict], tuple]:
    """The route's stops in driving order, with the pins the planner currently uses."""
    d = json.loads((outputs / "routes_data.json").read_text(encoding="utf-8"))
    r = next((x for x in d["routes"] if x["num"] == route), None)
    if r is None:
        raise SystemExit(f"Route {route} is not in routes_data.json")
    plan = r[session]["plan"]
    pts = {p["name"]: p for p in r[session]["points"]}
    seq = [dict(pts[s["name"]], printed=s["printed"]) for s in plan["current"]["stops"]]
    return seq, (d["school"]["lat"], d["school"]["lon"])


def minute(t: dt.datetime | None) -> float | None:
    return None if t is None else round(t.hour * 60 + t.minute + t.second / 60, 2)


def ingest(path: Path, route: int, session: str, outputs: Path) -> dict:
    track, taps, creator = read_gpx(path)
    if len(track) < 10:
        raise SystemExit(f"{path.name}: only {len(track)} track points")
    if not taps:
        raise SystemExit(f"{path.name}: no stop taps (waypoints) in the file")
    pins, school = planner_pins(outputs, route, session)
    ev = stop_events(track, taps)
    # a second tap at the same stop (tapped twice) would otherwise be matched to the next stop
    ev = [e for i, e in enumerate(ev)
          if i == 0 or _hav_m(e["lat"], e["lon"], ev[i - 1]["lat"], ev[i - 1]["lon"]) > REPEAT_TAP_M]
    # the school end of the run
    sch = None
    k = -1 if session == "am" else 0
    if _hav_m(ev[k]["lat"], ev[k]["lon"], *school) < SCHOOL_RADIUS_M:
        sch = ev.pop(k)
    match = align(ev, pins)
    stops, unmatched = [], []
    for e, mi in zip(ev, match):
        off = None if mi is None else _hav_m(e["lat"], e["lon"], pins[mi]["lat"], pins[mi]["lon"])
        row = {"tap": e["tap"].strftime("%H:%M:%S"),
               "arr_min": minute(e["arr"]), "dep_min": minute(e["dep"]),
               "dwell_s": (None if e["arr"] is None or e["dep"] is None
                           else int((e["dep"] - e["arr"]).total_seconds())),
               "lat": round(e["lat"], 6), "lon": round(e["lon"], 6),
               "moving_at_tap": e["moving_at_tap"]}
        if mi is None or off > MATCH_MAX_M:
            unmatched.append(row)
            continue
        p = pins[mi]
        stops.append(dict(row, name=p["name"], key=p["key"], printed=p["printed"],
                          riders=p["riders"], pin_offset_m=round(off)))
    seen = {s["name"] for s in stops}
    first = pins.index(next(p for p in pins if p["name"] == stops[0]["name"])) if stops else 0
    last = pins.index(next(p for p in pins if p["name"] == stops[-1]["name"])) if stops else -1
    skipped = [p["name"] for p in pins[first:last + 1] if p["name"] not in seen]
    km = sum(_hav_m(*track[i][:2], *track[i + 1][:2]) for i in range(len(track) - 1)) / 1000
    return {
        "file": path.name, "route": f"Route {route}", "num": route, "session": session,
        "date": track[0][2].date().isoformat(), "creator": creator,
        "logged_from": track[0][2].strftime("%H:%M:%S"), "logged_to": track[-1][2].strftime("%H:%M:%S"),
        "track_km": round(km, 2),
        "not_logged": [p["name"] for p in pins[:first]] if session == "am" else
                      [p["name"] for p in pins[last + 1:]],
        "stops": stops, "skipped": skipped, "unmatched_taps": unmatched,
        "school": None if sch is None else {
            "arr_min": minute(sch["arr"]), "dep_min": minute(sch["dep"]),
            "lat": round(sch["lat"], 6), "lon": round(sch["lon"], 6),
            "pin_offset_m": round(_hav_m(sch["lat"], sch["lon"], *school))},
    }


def save_run(run: dict, cache_dir: Path) -> Path:
    p = cache_dir / "gps_runs.json"
    runs = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    # pin offsets are measured against the pins at first import; once --apply-pins has
    # moved those pins, a re-import would read 0 m, so keep the first figures
    old = next((r for r in runs if r["file"] == run["file"]), None)
    if old:
        first = {s["name"]: s["pin_offset_m"] for s in old["stops"]}
        for s in run["stops"]:
            s["pin_offset_m"] = first.get(s["name"], s["pin_offset_m"])
    runs = [r for r in runs if r["file"] != run["file"]] + [run]
    p.write_text(json.dumps(runs, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def load_runs(cache_dir: Path) -> list[dict]:
    p = Path(cache_dir) / "gps_runs.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def apply_pins(cache_dir: Path) -> int:
    """
    Every stop measured by GPS becomes a verified pin: the mean of all its runs.
    A pin someone placed by hand (any note not starting "GPS") is left alone.
    """
    ov_path = cache_dir / "geocode_overrides.json"
    raw = json.loads(ov_path.read_text(encoding="utf-8")) if ov_path.exists() else {}
    obs: dict[str, list] = {}
    for r in load_runs(cache_dir):
        for s in r["stops"]:
            if not s["moving_at_tap"]:
                obs.setdefault(s["key"], []).append((s["lat"], s["lon"], r["date"], r["route"], r["session"]))
    n = 0
    for key, o in obs.items():
        cur = raw.get(key)
        if cur and not str(cur.get("note", "")).startswith("GPS"):
            continue
        runs = sorted({f"{x[3]} {x[4].upper()} {x[2]}" for x in o})
        raw[key] = {"lat": round(float(np.mean([x[0] for x in o])), 6),
                    "lon": round(float(np.mean([x[1] for x in o])), 6),
                    "label": key.split("||")[0],
                    "note": f"GPS stop position, mean of {len(o)} run(s): {', '.join(runs)}",
                    "gps_runs": len(o)}
        n += 1
    if ov_path.exists():
        shutil.copy2(ov_path, ov_path.with_suffix(f".backup-{dt.datetime.now():%Y%m%d-%H%M%S}.json"))
    ov_path.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    return n


def _hm(m):
    return "  -  " if m is None else f"{int(m // 60):02d}:{int(m % 60):02d}"


def report(run: dict) -> str:
    L = [f"{run['route']} {run['session'].upper()} {run['date']}  ({run['file']}, {run['creator']})",
         f"  logged {run['logged_from']} to {run['logged_to']}, {run['track_km']} km",
         f"  {'stop':44s} printed arrive  leave  dwell  pin off"]
    for s in run["stops"]:
        L.append(f"  {s['name'][:44]:44s} {_hm(s['printed'])}   {_hm(s['arr_min'])}  {_hm(s['dep_min'])}"
                 f"  {'' if s['dwell_s'] is None else str(s['dwell_s']) + ' s':>5s}  {s['pin_offset_m']:>5d} m"
                 + ("  (tap while moving)" if s["moving_at_tap"] else ""))
    if run["school"]:
        L.append(f"  {'SCHOOL':44s}         {_hm(run['school']['arr_min'])}  {_hm(run['school']['dep_min'])}"
                 f"         {run['school']['pin_offset_m']:>5d} m")
    if run["not_logged"]:
        L.append(f"  not logged (outside the recording): {', '.join(run['not_logged'])}")
    if run["skipped"]:
        L.append(f"  no tap (bus did not stop, or tap missed): {', '.join(run['skipped'])}")
    if run["unmatched_taps"]:
        L.append(f"  taps not near any stop of this route: {len(run['unmatched_taps'])}")
    return "\n".join(L)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx", nargs="+")
    ap.add_argument("--route", type=int, required=True)
    ap.add_argument("--session", choices=["am", "pm"], required=True)
    ap.add_argument("--outputs", default=str(root / "outputs"))
    ap.add_argument("--cache", default=str(root / "cache"))
    ap.add_argument("--apply-pins", action="store_true",
                    help="write measured stop positions into geocode_overrides.json")
    a = ap.parse_args()
    for f in a.gpx:
        run = ingest(Path(f), a.route, a.session, Path(a.outputs))
        save_run(run, Path(a.cache))
        print(report(run))
    if a.apply_pins:
        print(f"\n{apply_pins(Path(a.cache))} stop pins written to geocode_overrides.json "
              f"(rebuild with python pipeline/build_routes.py)")
