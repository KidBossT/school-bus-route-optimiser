"""
One command regenerates everything from a fresh monthly export:

    python pipeline/build_dashboard.py

    --inputs   directory holding the three source workbooks   (default: ./inputs)
    --outputs  where the dashboard and data files are written (default: ./outputs)
    --cache    geocode and road-time caches                   (default: ./cache)
    --offline  use the caches only; make no network calls

The three source workbooks are matched by pattern, not by exact filename, so a
re-issued export drops straight in. With a warm cache the whole build makes zero
network calls, which is why the caches are meant to be kept alongside the code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_data

PLACEHOLDER = "__DASHBOARD_DATA__"


def inject(template: Path, data: dict, out: Path) -> Path:
    html = template.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise ValueError(f"{template.name} has no {PLACEHOLDER} placeholder")

    # The payload sits in <script type="application/json">, so the only sequence
    # that can break out of it is a literal "</script>". Escaping the slash keeps
    # the JSON byte-identical to what JSON.parse expects.
    payload = json.dumps(build_data.json_safe(data), separators=(",", ":"),
                         default=str, allow_nan=False)
    payload = payload.replace("</", "<\\/")

    html = html.replace("__GMAP_TAGS__", build_data.geo.gmaps.browser_map_tags("default"))
    out.write_text(html.replace(PLACEHOLDER, payload), encoding="utf-8")
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=str(root / "inputs"))
    ap.add_argument("--outputs", default=str(root / "outputs"))
    ap.add_argument("--cache", default=str(root / "cache"))
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()

    inputs, outputs, cache = Path(a.inputs), Path(a.outputs), Path(a.cache)

    print("Building transport dashboard")
    print("-" * 74)
    res = build_data.build(inputs, outputs, cache, offline=a.offline)
    data = res["data"]

    template = Path(__file__).resolve().parent / "template.html"
    html = inject(template, data, outputs / "dashboard.html")
    print(f"  wrote {html.name} ({html.stat().st_size / 1024 / 1024:.1f} MB)")

    build_data.print_acceptance(data)
    print()
    print(build_data.geo.gmaps.usage_report())

    print("\nOutputs in", outputs)
    for f in sorted(outputs.iterdir()):
        print(f"  {f.name:28s} {f.stat().st_size / 1024:8.0f} KB")
    print("\nCaches in", cache, "- commit these so the build works offline")
    for f in sorted(cache.iterdir()):
        if f.suffix == ".json":
            print(f"  {f.name:28s} {f.stat().st_size / 1024:8.0f} KB")

    t = data["totals"]
    print(f"\nOpen {html} in a browser. "
          f"{t['geo_unverified']} of {t['geo_points']} stop locations are unverified - "
          f"see outputs/geocode_review.csv.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
