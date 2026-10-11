"""
Every tunable number the optimiser uses, in one place.

Defaults live here; `optimiser_config.json` at the project root overrides any
of them without touching code. Values marked ASSUMPTION are not in the source
data and must be confirmed with the transport office before a plan is used.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class Weights:
    """
    The objective is one weighted sum, in "minute-equivalents". Changing a
    weight changes what "optimal" means - these are policy, not physics.
    """
    bus_fixed: float = 60.0          # cost of putting one more bus on the road
    drive_min: float = 1.0           # per bus-minute driven (fuel, driver time)
    km: float = 0.5                  # per km driven (wear, fuel)
    student_ride_min: float = 0.05   # per student-minute on board (child welfare)
    reassigned_student: float = 2.0  # per child moved to a different bus (stability)
    violation: float = 1000.0        # per unit of capacity or time-limit breach


@dataclass
class OptimiserConfig:
    # --- fleet ------------------------------------------------------------
    default_capacity: int = 40            # ASSUMPTION: seats per bus, not in any file
    capacity_by_route: dict = field(default_factory=dict)   # {"R07": 32, ...}
    extra_vehicles: int = 0               # spare buses the solver may add
    # --- time rules -------------------------------------------------------
    max_ride_min: float = 75.0            # ASSUMPTION: longest a child may ride
    earliest_pickup_min: float = 6 * 60   # ASSUMPTION: no pickup before 06:00
    dismissal_min: float = 14 * 60 + 50   # ASSUMPTION: buses leave school 14:50
    latest_drop_min: float = 16 * 60 + 45 # ASSUMPTION: last drop by 16:45
    dwell_base_min: float = 0.75          # bus stops, doors open
    dwell_per_student_min: float = 0.25   # each child boarding / alighting
    school_dwell_min: float = 5.0         # loading at school before the PM run
    # --- travel times -----------------------------------------------------
    matrix_provider: str = "haversine"    # haversine | osrm | google | google_static
    use_calibration: bool = True          # scale times with cache/calibration.json when present
    time_multiplier_am: float = 1.0       # scale free-flow times for traffic (if no calibration)
    time_multiplier_pm: float = 1.0
    haversine_detour: float = 1.35        # road km per straight-line km
    haversine_speed_kmh: float = 22.0     # NCR peak-hour average, rough
    neighbours: int = 15                  # real road times only for k nearest
    # --- data readiness ---------------------------------------------------
    require_verified: bool = True         # only human-verified coordinates...
    allow_corroborated: bool = False      # ...or ones 2+ independent sources agree on (draft)
    min_confidence: str = "medium"        # when unverified points are allowed
    # --- stop consolidation ----------------------------------------------
    merge_radius_m: float = 250.0         # walk distance for a merge proposal
    apply_stop_merges: bool = False       # proposals only unless switched on
    # --- search -----------------------------------------------------------
    time_limit_s: float = 30.0
    seed: int = 7
    weights: Weights = field(default_factory=Weights)

    @classmethod
    def load(cls, path: Path | None) -> "OptimiserConfig":
        cfg = cls()
        if path is None or not Path(path).exists():
            return cfg
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            if k == "weights":
                wk = {f.name for f in fields(Weights)}
                bad = set(v) - wk
                if bad:
                    raise ValueError(f"unknown weight(s) in {path}: {sorted(bad)}")
                cfg.weights = Weights(**{**asdict(cfg.weights), **v})
            elif k in known:
                setattr(cfg, k, v)
            else:
                raise ValueError(f"unknown optimiser setting in {path}: {k!r}")
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
