# School bus route optimiser

[![tests](https://github.com/KidBossT/school-bus-route-optimiser/actions/workflows/tests.yml/badge.svg)](https://github.com/KidBossT/school-bus-route-optimiser/actions/workflows/tests.yml)

A data pipeline and route planner for the 23-bus fleet of Prometheus School, Noida.
It turns the school's spreadsheets (printed timetables, a student roster and 10,000
bus-card taps) into mapped routes, times them on Google's traffic predictions
calibrated to how these buses really drive, and proposes a faster stop order for
every run.

**Live planner:** [GitHub Pages](https://kidbosst.github.io/school-bus-route-optimiser/) ·
[Vercel](https://school-bus-route-optimiser.vercel.app/), password-protected
because it shows children's pickup points. Ask the owner for access.

![Fleet view: every route's proposed order, one colour per bus, with the yearly savings estimate](docs/img/fleet.png)

## Results

On the 23 routes that run today (408 children, 46 morning and afternoon runs):

| | |
|---|---|
| Driving saved | **191 min a day**, 8.7% less drive time; 36 of 46 runs get faster |
| Distance saved | **109 km a day**, about 24,000 km a school year |
| Fuel money saved | **₹4.2–5.1 lakh a year** (all-diesel to all-CNG fleet)* |
| CO₂ avoided | **13–16 tonnes a year***|
| Timing model error | 14.1 → **3.6 min per stop** against the printed timetable (route-by-route cross-validation) |
| Checked against GPS | Google's times matched a GPS-logged bus run to **within 3%** (41.3 vs 40.1 min) |

\* Assumes 220 school days, CNG 4 km/kg at ₹85/kg, diesel 5 km/litre at ₹88/litre,
2.7 kg CO₂ per kg CNG and 2.68 kg per litre diesel. Only distance is counted. All of
these can be edited on the planner's Fleet tab. The screenshot's Fleet totals (250
min, 147 km) also count two routes that are not running this term.

## How it works

```mermaid
flowchart LR
  A[School spreadsheets<br/>timetable, roster, taps] --> B[Parse and clean<br/>transport_spine.py]
  B --> C[Geocode stops<br/>geo.py]
  C -->|Google Places + Geocoding,<br/>OpenStreetMap fallback| D[Stop pins]
  E[GPS runs<br/>gps_runs.py] -->|measured pins| D
  D --> F[Traffic-aware travel times<br/>Google Routes + Route Matrix]
  F --> G[Calibrate to these buses<br/>calibration.py]
  E --> G
  G --> H[Search stop order<br/>build_routes.py]
  H --> I[Planner page<br/>routes.html]
  I --> J[Encrypt<br/>build_site.py] --> K[GitHub Pages / Vercel]
```

### The maths

- **Geocoding informal addresses** ("K-42 near Kwality store bazar"). Each word in a
  stop name is weighted by how rare it is across all 254 stop names, an inverse
  document frequency `log(N / (1 + df)) + 0.1`, so "metro" and "sector" count for
  almost nothing and a rare place name decides the match. A result must match 34% of
  the weighted name or it is rejected however confident the geocoder is. Distances
  use the haversine formula, and a pin counts as corroborated when independent
  sources agree within 300 m.
- **Stop order** is an open-path travelling salesman problem ending (morning) or
  starting (afternoon) at school. Runs of up to 13 stops are solved exactly with
  Held-Karp dynamic programming, O(n²·2ⁿ); longer runs use nearest-neighbour then
  2-opt. Travel times come from a sparse matrix: each stop to its 7 nearest stops on
  Google's traffic model, every other pair estimated at the run's median Google pace
  per straight-line km. Each stop is priced at the time the bus reaches it, and a
  proposal is kept only if Google's full-route timing confirms it is faster.
- **Calibration.** Google prices cars, not 40-seat buses stopping every few hundred
  metres. Each leg is scaled by a factor for its distance band (under 1, 1–2, 2–4,
  4–8, over 8 km), a ratio estimator fitted 80% on the printed timetable and 20% on
  observed tap times, then made non-decreasing with distance by the
  pool-adjacent-violators algorithm (isotonic regression). It is validated
  leave-one-route-out. Legs between GPS-measured pins use a separate factor fitted on
  GPS legs alone (0.97).
- **GPS runs.** A logged track is resampled to one point a second; a stop is the
  time spent inside a 25 m circle and its true position the median of those points.
  Stops are matched to the planned sequence by dynamic-programming alignment (like
  edit distance) with a penalty for skipped stops.
- **Measurement bias.** Morning card taps happen after the bus has left the stop, so
  they are late by an unknown lag. The analysis never fixes a value for it: the
  evidence dashboard recomputes every morning statistic live for any lag the reader
  picks. Times are reported as medians and p10–p90 ranges, always with the sample size.
- **Network optimiser** (built and tested, waiting for verified pins): a capacitated
  vehicle-routing solver with time windows: Clarke-Wright savings construction,
  relocate / swap / 2-opt / 2-opt\* local search, then ruin-and-recreate.

### Engineering

- Every Google call goes through one client that charges a monthly ledger before
  calling, stops at 80% of the free allowance, trips a circuit breaker on the first
  denial and falls back to free OpenStreetMap services. A normal month costs nothing.
- Google results expire from the cache after 30 days, as Google's terms require.
- The server key never reaches a web page; the pages use a separate browser key
  restricted to the site's addresses and the Maps JavaScript API.
- 32 unit tests, run on every push by GitHub Actions.

## Privacy

The school's spreadsheets, the student roster, the cache and every generated page
stay on the project computer and are not in this repository. The published planner
is encrypted with [StatiCrypt](https://github.com/robinmoisson/staticrypt)
(AES-256, key from the password by PBKDF2) and is excluded from search engines. No
student names or home locations appear anywhere in the planner; it shows stops only.

## Running it

Python 3.12 and `pip install -r requirements.txt`. The school's three workbooks go in
`inputs/` and a Google Maps key in `.env` (see [docs/TECHNICAL.md](docs/TECHNICAL.md)).

```bash
python pipeline/build_routes.py      # data, routes and outputs/routes.html
python pipeline/serve.py             # view it at http://localhost:8777
python pipeline/gps_runs.py run.gpx --route 11 --session am --apply-pins
python -m unittest discover tests
```

### Publishing

```bash
python pipeline/build_site.py        # encrypts the planner into site/ (needs Node.js)
```

Commit `site/` and push: a GitHub Actions workflow deploys it to Pages, and Vercel
redeploys from the same folder. The password and salt live in `.env` as
`SITE_PASSWORD` and `SITE_SALT`. The Google Maps browser key must list the site's
addresses under *Website restrictions*:
`https://kidbosst.github.io/*` and `https://school-bus-route-optimiser.vercel.app/*`.

## Layout

```
pipeline/
  transport_spine.py   spreadsheet parsers and audit log
  geo.py               geocoding and road times, provider-abstracted
  gmaps.py             Google client: usage caps, circuit breaker, 30-day expiry
  calibration.py       distance-band factors, isotonic fit, cross-validation
  build_routes.py      stop-order search and the planner page
  gps_runs.py          GPX ingestion and stop alignment
  build_site.py        encrypted copy for the web
  optimiser/           network-wide vehicle-routing solver
  routes_template.html planner front end (Google Maps, no framework)
tests/                 unit tests
site/                  the published, encrypted planner
docs/TECHNICAL.md      full design notes, constants and known limits
```
