"""
Network-wide route optimisation: re-assigns stops between buses and re-orders
every run, then compares the result with today's plan on the same metrics.

    python pipeline/run_optimiser.py --export-coord-template   # 1. sheet to fill in Google Maps
    python pipeline/run_optimiser.py --import-coords FILE.csv  # 2. load the filled-in sheet
    python pipeline/run_optimiser.py                           # 3. optimise (verified stops only)

    python pipeline/run_optimiser.py --demo                    # synthetic network, no real data
    python pipeline/run_optimiser.py --allow-unverified        # DRAFT on geocoder guesses

Outputs go to outputs/optimiser/ (outputs/optimiser_demo/ for --demo).
Settings: optimiser_config.json at the project root (see optimiser/config.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gmaps
from optimiser import build_problem, coords, demo, report
from optimiser.config import OptimiserConfig
from optimiser.evaluate import Evaluator
from optimiser.solver import Solver


def optimise(built, cfg, time_limit, log=print) -> dict:
    ev = Evaluator(built.problem, cfg)
    solver = Solver(built.problem, ev, k=cfg.neighbours, seed=cfg.seed)
    starts = [("today", built.current), ("savings", solver.savings())]
    best, history = solver.solve(starts, time_limit_s=time_limit, log=log)
    return {"built": built, "ev": ev, "best": best, "history": history}


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inputs", default=str(root / "inputs"))
    ap.add_argument("--outputs", default=str(root / "outputs"))
    ap.add_argument("--cache", default=str(root / "cache"))
    ap.add_argument("--config", default=str(root / "optimiser_config.json"))
    ap.add_argument("--offline", action="store_true", help="no travel-time API calls")
    ap.add_argument("--time-limit", type=float, help="seconds of search per direction")
    ap.add_argument("--direction", choices=["am", "pm", "both"], default="both")
    ap.add_argument("--demo", action="store_true", help="run on a synthetic network")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="route geocoder guesses too (output is marked DRAFT)")
    ap.add_argument("--export-coord-template", action="store_true")
    ap.add_argument("--import-coords", metavar="FILE")
    a = ap.parse_args(argv)

    cfg = OptimiserConfig.load(Path(a.config))
    if a.allow_unverified:
        cfg.require_verified = False
    limit = a.time_limit if a.time_limit is not None else cfg.time_limit_s
    inputs, outputs, cache = Path(a.inputs), Path(a.outputs), Path(a.cache)
    directions = ["am", "pm"] if a.direction == "both" else [a.direction]

    if a.import_coords:
        st = coords.import_file(Path(a.import_coords), cache)
        print(f"imported {st['imported']} coordinates, {st['skipped_blank']} blank rows skipped")
        for key, why in st["rejected"]:
            print(f"  rejected {key!r}: {why}")
        return 0 if not st["rejected"] else 1

    if a.demo:
        results = {}
        for d in directions:
            print(f"{d.upper()}  synthetic network")
            results[d] = optimise(demo.make(d, cfg), cfg, limit)
        path = report.write(outputs / "optimiser_demo", results, cfg.to_dict(), draft=True,
                            label="SYNTHETIC DEMO")
        print(f"\nwrote {path}")
        return 0

    routes, students = build_problem.load_sources(inputs)
    gc = build_problem.make_geocoder(routes, cache)

    if a.export_coord_template:
        path = outputs / "optimiser" / "stop_coordinates_template.csv"
        n = coords.export_template(routes, students, gc, path)
        print(f"wrote {n} locations to {path}\n"
              f"Fill `google_maps` (right-click a pin in Google Maps, copy '28.57, 77.39') "
              f"then run --import-coords on it.")
        return 0

    results = {}
    for d in directions:
        try:
            built = build_problem.build(d, routes, students, gc, cfg, cache, a.offline)
        except RuntimeError as exc:
            print(f"cannot optimise: {exc}")
            return 2
        n, h = built.problem.n, len(built.held)
        print(f"{d.upper()}  {n} stops routable, {h} held")
        if n < 2:
            print(f"  too few stops with usable coordinates to optimise. Run "
                  f"--export-coord-template, fill it in, and --import-coords it.")
            continue
        results[d] = optimise(built, cfg, limit)
    if not results:
        return 2
    path = report.write(outputs / "optimiser", results, cfg.to_dict(),
                        draft=not cfg.require_verified or cfg.allow_corroborated,
                        label="Prometheus School")
    print(f"\nwrote {path}")
    print(gmaps.usage_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
