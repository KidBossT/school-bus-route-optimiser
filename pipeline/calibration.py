"""
Scale Google's drive times to how long these buses actually take.

Two kinds of leg, calibrated separately:

1. Legs between UNVERIFIED pins (geocoder guesses: most of the network).
   Google's times here come out about twice the printed timetable on short hops
   and close to it on long legs, so the factor depends on how far apart the two
   points are:

       calibrated leg = Google leg (traffic) x factor(straight-line km)

   Targets, by weight (the school's instruction):
     80%  the PRINTED timetable - every consecutive pair of printed stop times
          gives one leg (printed gap minus dwell). The most accurate source.
     20%  whole-run TAP durations (first stop -> school in the morning, first ->
          last drop in the afternoon), start and end taps only, so per-stop
          scanning logistics mostly drop out. Taps only nudge the timetable fit.

   Fit: factor per distance band = trimmed ratio of summed printed driving time
   to summed Google time, sparse bands pooled with a neighbour, forced to rise
   (or stay flat) with distance. Taps then scale every band by the 20%-weighted
   geometric share of their disagreement.

2. Legs between VERIFIED pins (both ends at a confirmed position, e.g. measured
   by a GPS run, gps_runs.py). The first GPS run (Route 11, 2026-10-05) showed
   Google's raw traffic times between correct pins match the bus to within 3%
   over a whole run: most of the ~0.5 factor above corrects for pins sitting
   inside societies, where Google routes round the internal lanes. So these
   legs get one factor measured from GPS legs alone (summed GPS driving time /
   summed Google time), used once MIN_GPS_LEGS legs exist; until then they fall
   back to the band factors. Sessions with too few GPS legs borrow the other
   session's. Printed legs between verified pins are left out of the band fit,
   since the pin error they would be correcting is not there.

Accuracy is checked by fitting on all routes but one and predicting that one
(leave-one-route-out), against the printed times (primary), the taps
(secondary) and any GPS runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

BAND_EDGES_KM = [1.0, 2.0, 4.0, 8.0]          # bands: <1, 1-2, 2-4, 4-8, >8 km
BAND_MID_KM = [0.5, 1.5, 3.0, 6.0, 12.0]      # interpolation points
TAP_WEIGHT = 0.2                              # printed timetable gets the other 0.8
MIN_LEGS_PER_BAND = 8
MIN_GPS_LEGS = 5              # GPS legs needed before verified legs get their own factor
RATIO_TRIM = (0.1, 3.0)       # a printed gap outside this x Google is not driving time
FACTOR_CLAMP = (0.25, 1.25)
TAP_CLAMP = (0.5, 1.5)
MIN_TAP_DAYS = 5
PASS_STOP_MIN = 3.0           # median error per stop, minutes
PASS_RUN_MIN = 5.0            # median error per whole run, minutes


def band_of(km: float) -> int:
    return sum(km >= e for e in BAND_EDGES_KM)


@dataclass
class Leg:
    km: float            # straight-line distance
    google: float        # Google minutes (traffic)
    printed: float | None = None   # printed driving minutes (gap minus dwell)
    gps: float | None = None       # measured driving minutes (GPS departure -> next arrival)
    verified: bool = False         # both ends at confirmed positions


@dataclass
class Run:
    route: str
    session: str         # am | pm
    legs: list[Leg]      # stop-to-stop legs, in driving order
    dwell: float         # total dwell between first and last stop
    school_leg: Leg | None = None   # morning run-in, not in the printed data
    tap_min: float | None = None    # observed whole run (median over days)
    tap_days: int = 0
    printed_offsets: list[float] = field(default_factory=list)  # printed time - first printed
    gps_offsets: list[float] = field(default_factory=list)      # GPS arrival - first GPS departure
    gps_dwell_s: list[int] = field(default_factory=list)        # measured dwell at each stop


@dataclass
class Model:
    factors: dict[str, list[float]]            # session -> factor per band (unverified pins)
    tap_adjust: dict[str, float]
    legs_per_band: dict[str, list[int]]
    verified_factor: dict[str, float | None] = field(default_factory=dict)
    gps_legs: dict[str, int] = field(default_factory=dict)

    def factor(self, session: str, km: float, verified: bool = False) -> float:
        if verified and self.verified_factor.get(session) is not None:
            return self.verified_factor[session]
        f = self.factors[session]
        if km <= BAND_MID_KM[0]:
            return f[0]
        if km >= BAND_MID_KM[-1]:
            return f[-1]
        for i in range(len(BAND_MID_KM) - 1):
            a, b = BAND_MID_KM[i], BAND_MID_KM[i + 1]
            if a <= km <= b:
                t = (km - a) / (b - a)
                return f[i] + t * (f[i + 1] - f[i])
        return f[-1]

    def of(self, session: str, leg: Leg) -> float:
        return self.factor(session, leg.km, leg.verified)

    def to_dict(self) -> dict:
        return {"bands_km": ["<1", "1-2", "2-4", "4-8", ">8"], "factors": self.factors,
                "tap_adjust": self.tap_adjust, "legs_per_band": self.legs_per_band,
                "verified_factor": self.verified_factor, "gps_legs": self.gps_legs,
                "min_gps_legs": MIN_GPS_LEGS, "tap_weight": TAP_WEIGHT}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Model | None":
        if not Path(path).exists():
            return None
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(d["factors"], d.get("tap_adjust", {}), d.get("legs_per_band", {}),
                   d.get("verified_factor", {}), d.get("gps_legs", {}))


def _band_factors(legs: list[Leg]) -> tuple[list[float], list[int]]:
    num, den, cnt = [0.0] * 5, [0.0] * 5, [0] * 5
    for l in legs:
        if l.printed is None or l.verified or l.google <= 0.5:
            continue
        r = l.printed / l.google
        if not (RATIO_TRIM[0] <= r <= RATIO_TRIM[1]):
            continue
        b = band_of(l.km)
        num[b] += l.printed; den[b] += l.google; cnt[b] += 1
    # pool sparse bands into the nearest populated neighbour (towards the middle)
    groups = [[i] for i in range(5)]
    changed = True
    while changed:
        changed = False
        for gi, g in enumerate(groups):
            if sum(cnt[i] for i in g) < MIN_LEGS_PER_BAND and len(groups) > 1:
                j = gi - 1 if gi == len(groups) - 1 else gi + 1 if gi == 0 else (
                    gi - 1 if sum(cnt[i] for i in groups[gi - 1]) <= sum(cnt[i] for i in groups[gi + 1]) else gi + 1)
                groups[min(gi, j)] = sorted(groups[gi] + groups[j])
                del groups[max(gi, j)]
                changed = True
                break
    vals = [0.0] * 5
    for g in groups:
        d = sum(den[i] for i in g)
        v = sum(num[i] for i in g) / d if d else 1.0
        for i in g:
            vals[i] = v
    # rise (or stay flat) with distance: pool adjacent violators, weighted by legs
    blocks = [[vals[i], max(cnt[i], 1), [i]] for i in range(5)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-9:
            a, b = blocks[i], blocks[i + 1]
            w = a[1] + b[1]
            blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / w, w, a[2] + b[2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    out = [0.0] * 5
    for v, _, idx in blocks:
        for k in idx:
            out[k] = round(min(max(v, FACTOR_CLAMP[0]), FACTOR_CLAMP[1]), 3)
    return out, cnt


def _gps_factor(legs: list[Leg]) -> tuple[float | None, int]:
    """Summed GPS driving time over summed Google time, verified legs only."""
    use = [l for l in legs if l.gps is not None and l.google > 0.5
           and RATIO_TRIM[0] <= l.gps / l.google <= RATIO_TRIM[1]]
    if len(use) < MIN_GPS_LEGS:
        return None, len(use)
    v = sum(l.gps for l in use) / sum(l.google for l in use)
    return round(min(max(v, FACTOR_CLAMP[0]), FACTOR_CLAMP[1]), 3), len(use)


def _predict_run(run: Run, fn) -> float:
    t = sum(fn(l) * l.google for l in run.legs) + run.dwell
    if run.school_leg is not None:
        t += fn(run.school_leg) * run.school_leg.google
    return t


def fit(runs: list[Run]) -> Model:
    factors, adjust, counts, vf, gl = {}, {}, {}, {}, {}
    all_legs = [l for r in runs for l in r.legs]
    gps_all = [l for l in all_legs if l.gps is not None]
    for s in ("am", "pm"):
        legs = [l for r in runs if r.session == s for l in r.legs]
        fp, cnt = _band_factors(legs if len(legs) >= 3 * MIN_LEGS_PER_BAND else all_legs)
        v, n = _gps_factor([l for l in legs if l.gps is not None])
        if v is None:                        # borrow the other session's GPS legs
            v, n = _gps_factor(gps_all)
        m0 = Model({s: fp}, {}, {}, {s: v})
        ratios = [r.tap_min / p for r in runs if r.session == s and r.tap_min
                  and r.tap_days >= MIN_TAP_DAYS and (p := _predict_run(r, lambda l: m0.of(s, l))) > 5]
        g = float(np.median(ratios)) if len(ratios) >= 3 else 1.0
        g = min(max(g, TAP_CLAMP[0]), TAP_CLAMP[1])
        mult = g ** TAP_WEIGHT
        factors[s] = [round(min(max(x * mult, FACTOR_CLAMP[0]), FACTOR_CLAMP[1]), 3) for x in fp]
        adjust[s] = round(mult, 3)
        counts[s] = cnt
        vf[s], gl[s] = v, n
    return Model(factors, adjust, counts, vf, gl)


def _gps_errors(r: Run, fn, dwell: float) -> list[float]:
    """Arrival error at each GPS-logged stop, predicted from the first departure."""
    t, errs = 0.0, []
    for i, l in enumerate(r.legs):
        t += fn(l) * l.google + (dwell if i else 0.0)
        errs.append(abs(t - r.gps_offsets[i]))
    return errs


def validate(runs: list[Run], dwell: float = 1.0) -> dict:
    """
    Leave-one-route-out. For each route, fit on every other route, then predict
    its printed stop times from its first printed stop, its tap run time, and
    the arrival times of any GPS run of it (from the first logged departure,
    with the planner's dwell). GPS errors are also given for the model fitted on
    everything (its own GPS run included) - in-sample, so not a test.
    """
    out = {}
    routes = sorted({r.route for r in runs})
    full = fit(runs)
    one = lambda l: 1.0
    for s in ("am", "pm"):
        stop_raw, stop_cal, run_raw, run_cal, tap_raw, tap_cal = [], [], [], [], [], []
        gps_raw, gps_cal, gps_in, run_g_raw, run_g_cal, run_g_in = [], [], [], [], [], []
        for rt in routes:
            test = [r for r in runs if r.route == rt and r.session == s]
            if not test:
                continue
            m = fit([r for r in runs if r.route != rt])
            f = lambda l: m.of(s, l)
            for r in test:
                if len(r.printed_offsets) == len(r.legs) + 1 and r.legs:
                    for fn, stops, whole in ((one, stop_raw, run_raw), (f, stop_cal, run_cal)):
                        t, dw = 0.0, r.dwell / len(r.legs)
                        errs = []
                        for i, l in enumerate(r.legs):
                            t += fn(l) * l.google + dw
                            errs.append(abs(t - r.printed_offsets[i + 1]))
                        stops.extend(errs)
                        whole.append(errs[-1])
                if r.tap_min and r.tap_days >= MIN_TAP_DAYS:
                    tap_raw.append(abs(_predict_run(r, one) - r.tap_min))
                    tap_cal.append(abs(_predict_run(r, f) - r.tap_min))
                if r.gps_offsets and len(r.gps_offsets) == len(r.legs):
                    for fn, stops, whole in ((one, gps_raw, run_g_raw), (f, gps_cal, run_g_cal),
                                             (lambda l: full.of(s, l), gps_in, run_g_in)):
                        e = _gps_errors(r, fn, dwell)
                        stops.extend(e)
                        whole.append(e[-1])
        med = lambda v: round(float(np.median(v)), 1) if v else None
        out[s] = {
            "routes": len({r.route for r in runs if r.session == s}),
            "stops_tested": len(stop_cal),
            "printed_stop_error_min": {"google": med(stop_raw), "calibrated": med(stop_cal)},
            "printed_run_error_min": {"google": med(run_raw), "calibrated": med(run_cal)},
            "tap_run_error_min": {"google": med(tap_raw), "calibrated": med(tap_cal),
                                  "runs": len(tap_cal)},
            "gps_stop_error_min": {"google": med(gps_raw), "calibrated_without_gps": med(gps_cal),
                                   "calibrated_with_gps": med(gps_in), "stops": len(gps_cal)},
            "gps_run_error_min": {"google": med(run_g_raw), "calibrated_without_gps": med(run_g_cal),
                                  "calibrated_with_gps": med(run_g_in), "runs": len(run_g_cal)},
            "gps_dwell_s": {"median": med([d for r in runs if r.session == s for d in r.gps_dwell_s]),
                            "stops": sum(len(r.gps_dwell_s) for r in runs if r.session == s)},
        }
        out[s]["passes"] = bool(stop_cal and run_cal
                                and np.median(stop_cal) <= PASS_STOP_MIN
                                and np.median(run_cal) <= PASS_RUN_MIN)
    out["thresholds"] = {"stop_min": PASS_STOP_MIN, "run_min": PASS_RUN_MIN}
    return out
