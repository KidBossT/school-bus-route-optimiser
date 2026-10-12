# Technical notes

*The full design record. The project overview is in the [README](../README.md).*

## Prometheus School — transport dashboard v2

A single self-contained HTML page showing everything currently known about the
school's bus routes: the times printed for parents, the times actually observed
from tap data, the road times from a mapping service, and the gap between all
three — per route, on a map and in tables.

This is a **visualisation and evidence layer**. It contains no solver, no
optimiser, no clustering, no live tracking, and no recommendation about which
stops to move or merge. Every feature answers one question: *does this help a
reader see what is currently true?*

---

## Rerun

```bash
python pipeline/build_dashboard.py
```

That is the whole command. It reads the three workbooks from `inputs/`, writes
everything to `outputs/`, and opens with no server.

| Flag | Default | Purpose |
|---|---|---|
| `--inputs` | `./inputs` | directory holding the three source workbooks |
| `--outputs` | `./outputs` | dashboard and data files |
| `--cache` | `./cache` | geocode and road-time caches |
| `--offline` | off | use caches only, make no network calls |

The source workbooks are matched by **pattern, not exact filename**, so next
month's re-issued export drops straight in. No route names, stop names or dates
appear anywhere in the analysis logic.

**With a warm cache the build makes zero network calls.** `cache/` stays on the
project computer and out of the public repository: it holds Google results, which
Google's terms allow to be stored for 30 days only, and the corrected stop pins.
A cold run takes roughly 20
minutes, almost all of it waiting politely between free-tier geocoder requests.

Requires `pandas`, `numpy`, `openpyxl`. The map needs an internet connection at
*view* time for its tiles; every other part of the page works offline.

---

## Files

```
inputs/                      the three monthly workbooks, unmodified
pipeline/
  transport_spine.py         parsers + audit log        (pre-existing, unmodified)
  analysis.py                phase-1 statistics         (pre-existing, unmodified)
  run_pipeline.py            phase-1 Excel reports      (pre-existing, unmodified)
  gmaps.py                   Google client: .env, usage caps, breaker, 30-day expiry
  geo.py                     geocoding + road times, provider-abstracted   NEW
  build_data.py              emits dashboard_data.json                     NEW
  build_dashboard.py         one-command entry point                       NEW
  template.html             the dashboard; all front-end logic lives here  NEW
.env                         API key + provider switches (git-ignored, never commit)
cache/
  api_usage.json             Google calls per SKU per month, checked before each call
  geocode_cache.json         every query ever issued, keyed by query text
  geocode_overrides.json     hand-edited corrections — the only "verified" points
  route_times_cache.json     road durations + geometry, keyed by coords + hour
outputs/
  dashboard.html             the deliverable
  dashboard_data.json        the same data, uninjected, for inspection
  geocode_review.csv         every stop, what the geocoder said, and why it is suspect
```

`transport_spine.py`, `analysis.py` and `run_pipeline.py` are the existing,
validated pipeline. They were **extended, not rewritten** — not one line was
changed. Everything new sits on top of the spine they produce.

---

## The scan lag, and why the whole dashboard is built around it

Children carry BLE proximity cards. (The vendor export is named "RFID"; it is
not. They are tap logs.) There are four taps a day and they are **not of equal
quality**:

| Leg | When the tap happens | Coverage | Bias |
|---|---|---|---|
| Morning boarding | **after the bus has left the stop** — the attendant scans seated children in sequence | 86.9% | large, unmeasured, positive: `L` |
| Morning school arrival | entering the school premises | 79.5% | unknown, reader technology unconfirmed |
| Afternoon leaving school | exiting the premises at dismissal | 95.6% | low |
| Afternoon drop-off | as the child steps off the bus | 70.4% | low |

Morning boarding times are therefore **late by an unknown amount `L`**. Morning
lateness is an *upper bound* on true lateness; morning ride duration is a *lower
bound*. `L` is a level shift, not a drift — it does not accumulate along a route
(correlation between stop position and median deviation is −0.08).

`L` has never been measured, and its plausible range inverts the entire picture.
At `L=0` fifty-odd stops look systematically late; at `L=6` only a handful do and
dozens look systematically **early** instead.

**So no morning verdict is computed in Python.** `build_data.py` ships raw
per-stop first-tap arrival arrays to the browser, and `template.html` recomputes
every morning statistic, classification, and feasibility verdict live at the
reader's chosen `L`. Hardcoding an unmeasured constant into the findings would
be the single worst thing that could be done to this project, so the code makes
it structurally impossible.

Afternoon drop and leaving-school taps are bias-free. The slider does not touch
them, and the dashboard says so on every afternoon view.

**The cheapest thing anyone could do for this project is measure `L`.** One
person, three stops, one week, a stopwatch. It would collapse the slider to a
point and make every morning number in this dashboard actionable.

---

## Geocoding and road times

### Providers

| | Google (selected in `.env`) | Free fallback |
|---|---|---|
| Geocoding | **Places API (New) Text Search** *and* **Geocoding API**, both asked for every stop. Places finds landmarks and buildings ("near Kwality Store", "Gate 4 Ace Aspire"); the geocoder finds addresses. Their candidates are scored together by the same name-match gate. | **Photon** (OSM), Nominatim, Esri. Used when Google finds nothing that passes the gate, or is unavailable. |
| Road times | **Routes API** `computeRoutes`, `TRAFFIC_AWARE_OPTIMAL`, departing at the **printed time on the next Tuesday**. Returns the traffic prediction *and* the free-flow time for every leg. | **OSRM** public server, free-flow only. |

`.env` at the project root holds the key and the switches (`GEOCODER=google`,
`ROUTER=google`). It is git-ignored and loaded by `pipeline/gmaps.py`; a variable
already set in the shell wins. Check the key and see which APIs answer:

```bash
python pipeline/gmaps.py --check     # 4 requests + 1 matrix element, free tier
python pipeline/gmaps.py --usage     # this month's ledger against the caps
```

### Usage limits, enforced in code

Every Google call goes through `pipeline/gmaps.py`, which:

- **charges a monthly ledger before the call** (`cache/api_usage.json`) and refuses
  once a SKU hits its cap. Default caps are **80% of Google's free monthly
  allowance**, so a normal month costs nothing. Raise one with
  `GMAPS_CAP_<SKU>=n` in `.env`.
- **trips a breaker on the first denial** (billing off, API not enabled, quota
  exhausted): no further Google calls that run, the build falls back to the free
  stack, and the end-of-run summary says so.
- **never caches a failure**, so a denied call is retried on the next run rather
  than remembered as "no result".
- **deletes Google results after 30 days** (Google's storage limit for
  coordinates and routes). Place IDs are kept; they may be stored indefinitely.

| SKU | Free / month | Cap | A cold full build uses | Price past free |
|---|---|---|---|---|
| Geocoding | 10,000 | 8,000 | ~260–760 | $5 / 1k |
| Places Text Search Pro | 5,000 | 4,000 | ~260–900 | $32 / 1k |
| Compute Routes Pro (traffic) | 5,000 | 4,000 | ~550–650 | $10 / 1k |
| Route Matrix Pro (traffic, per element) | 5,000 | 4,000 | ~3,800 (route planner's order search) | $10 / 1k |

A warm rerun inside 30 days makes **zero** calls. After 30 days the expired
entries are re-fetched once, which is the same cold-build cost again.

Places Text Search requests only `id, displayName, formattedAddress, location,
types` — all Pro-tier fields. Adding rating, hours or phone would move every call
to the Enterprise SKU (1,000 free, not 5,000).

### Maps on the pages

Both pages draw on **Google's map** (Maps JavaScript API), because Google's terms
do not allow Places, Geocoding or Routes content on a non-Google map.
`pipeline/gmap_shim.js` gives the pages the few Leaflet calls they use, drawn on
Google. Re-drawing a map re-uses it, so the dashboard's slider does not cost a
map load per move. The Stops tab has a **Satellite** switch for finding the real
gate.

This needs a **second, browser key**, because a key inside a web page is visible to
anyone who opens it:

1. Console → APIs & Services → Library → **Maps JavaScript API** → Enable.
2. Credentials → Create credentials → API key → edit it: name `planner-browser-key`;
   *Application restrictions* → **Websites** → add `http://localhost:8777/*` and
   `http://127.0.0.1:8777/*`; *API restrictions* → **Maps JavaScript API** only.
3. Put it in `.env` as `GOOGLE_MAPS_BROWSER_KEY=...` and rebuild. The build refuses
   to put the server key in a page.
4. Open the pages with `python pipeline/serve.py` (`http://localhost:8777`). A page
   opened as a file has no web address, so the key is refused, and OpenStreetMap
   used to block those requests too.

Only creating a map is billed: panning, zooming, satellite, switching tabs or
routes, redrawing, and the dashboard slider all re-use the maps already made.
Cost: Dynamic Maps, 10,000 free map loads a month. The planner uses 2 per open;
the dashboard uses 1 plus 1 per route section opened.

### Source agreement

Each resolved point records which **independent** sources put it within 300 m
(`sources_agreeing` in `geocode_review.csv`) and which put their own best answer
more than 1 km away (a flag). Google Places and Google Geocoding are separate
databases and count separately; Photon and Nominatim are both OpenStreetMap and
count once. Free-provider answers already in the cache are re-read for this at no
cost; they never override a Google answer. Two agreeing sources is the strongest
signal short of a human check. It is **not** verification.

### Departure times

Traffic-aware routing rejects past departure times, and today may be a Sunday.
Every Google road time is therefore requested for the **next Tuesday** at the
printed time, IST. Morning legs depart at the printed pickup of their first stop;
the afternoon leg out of school departs one estimated leg before the first
printed drop. The route planner prices a whole run in one request, and Google
advances the clock along it, so each later stop is priced at the time the bus
reaches it.

The road-time cache is keyed by coordinates **and departure hour**, so Google's
entries sit beside the free-flow OSRM entries without colliding.

### Route planner: Google is what gets optimised

`build_routes.py` searches each run's stop order on **Google's traffic-aware
travel times** (Route Matrix): every stop to its 7 nearest stops and to/from
school, each origin departing at its own printed time. Pairs outside that
neighbourhood are estimated at the run's own measured Google pace. The printed
order and the proposal are then timed with one traffic-aware `computeRoutes`
request each, and a proposal Google says is not faster is dropped. The matrix is
costed for the whole build **before** the first request; if it does not fit the
month's cap the search falls back to OSRM and the log says so.

### Calibration: Google's car times scaled to how these buses run

Google's times run about **twice** the printed timetable on short hops between
nearby stops and close to it on long legs (e.g. the run into school), so one
factor cannot fit. `pipeline/calibration.py` multiplies each Google leg by a factor
that depends on the leg's straight-line length (bands <1, 1–2, 2–4, 4–8, >8 km):

- fitted **80% on the printed timetable** (every consecutive pair of printed stop
  times is one leg) and **20% on whole-run tap times** (first stop → school, first
  → last drop; start and end taps only, so boarding logistics mostly drop out);
- forced to rise (or stay flat) with distance, sparse bands pooled;
- **tested route by route** (fit on all other routes, predict this one). The
  result is printed on every build and shown on the page.

2026-09 data: morning factors 0.50 → 0.73 short → long, afternoon 0.52 → 0.72. Typical
error against the printed times fell from 14.1 to **3.6 min per stop** (morning) and
16.0 to **5.7** (afternoon). That is still above the 3-minute target. Six model shapes
all level off at the same error, and even distance alone does as well. So the
remaining error comes from unconfirmed pins and minute-rounded or inconsistent
printed times, not the model. Confirming pins is what closes it.

Google's `vehicleStopover` waypoint option was tested on 6 routes and changes
nothing, so the gap is not how Google approaches a pin. `--raw-google` shows the
uncalibrated times. The fitted model is saved to `cache/calibration.json`, and the
network optimiser uses it too.

**Verified pins get their own factor, measured by GPS.** The first GPS run
(Route 11 morning, 5 Oct 2026, Cleo County to school) put the stops 53–593 m
(median 174 m) from Google's pins, which often sit inside the society (Cleo
County's was Tower B6). Re-priced between the measured positions, Google's raw
traffic times matched the bus to within 3% over the run (41.3 vs 40.1 min of
driving) and 2.0 min per stop, while the ×0.5 factors made them 16 min too fast.
So most of the scaling corrects for pins in the wrong place, not for buses
beating Google. Legs with both ends at verified pins (the school counts) now use
one factor fitted on GPS legs alone (0.971 from 8 legs), once there are at least
`MIN_GPS_LEGS` (5); afternoon borrows the morning's until it has its own. Printed
legs between verified pins are left out of the band fit. The band factors are
otherwise unchanged in method (80% printed, 20% taps). The printed-timetable error
moved 3.6 → 4.0 min per stop, all of it Route 11, whose printed times are tighter
than what the bus did (6 min behind by Sector 47); excluding Route 11 it is 3.6
before and after.

### GPS runs (`pipeline/gps_runs.py`)

Runs now usually come from the web tracker (`site/track.html`, built from
`pipeline/track_template.html`): its files carry `route=N session=am|pm` in the GPX
metadata, so `python pipeline/gps_runs.py R11_am_2026-10-14_0702.gpx.txt --apply-pins`
needs no `--route`/`--session`. Phone GPS is noisier than the first iPhone run, so stop
detection is robust to it: the stop's centre is the median of the track 5 s either side
of the tap (`CENTRE_S`); the bus has arrived/left only after `EXIT_S` = 4 s in a row
outside the 25 m circle; a bus that pulls forward up to `CREEP_M` = 35 m and stands again
within `CREEP_S` = 8 s (10 s still, `REST_S`/`REST_M`) is still at the same stop; distance
is measured on a 5 s rolling median with 10 m minimum steps, so jitter at rest adds
nothing. On the Route 11 iPhone run these changes move no stop time by more than 1 s.


```bash
python pipeline/gps_runs.py "Self Tracking R11.gpx" --route 11 --session am --apply-pins
python pipeline/build_routes.py
```

Reads a GPX file with a continuous track and one waypoint per stop (Open GPX
Tracker "Add waypoint", GPSLogger Annotate), measures arrival, departure, dwell
and the real stop position at each tap (25 m circle), matches taps to the route's
stops in order (skipped stops and part-route logging are fine), and stores the run
in `cache/gps_runs.json`. `--apply-pins` writes every measured stop into
`geocode_overrides.json` (mean of all runs of that stop; a pin set by hand is not
touched). The next build re-prices those legs on Google and refits the
calibration; the build log prints the error against each GPS run.

### The planner page (`outputs/routes.html`)

- **Routes**: per route and run, *Old route* / *New route* / **Compare**. Compare
  shows the old and new orders side by side on two synced maps, a summary (stops
  that move, distance, first pickup, school arrival, longest ride) and a
  stop-by-stop table: new position, old position, old → new time, and how much
  longer or shorter each child's ride becomes.
- **Stops**: every placed stop coloured by how well its pin is confirmed, with the
  old free-map pin and a line showing how far Google moved it. Merge suggestions
  (stops within 250 m, across buses) and the stops that could not be placed.
  Dragging moves the map. Switch on **Move pins**, then **drag a pin** to where the
  bus really stops, then *Download corrections*, then
  `python pipeline/run_optimiser.py --import-coords stop_corrections.csv`, which
  makes those pins verified for every page.
- **Fleet**: all routes ranked by minutes saved per day, with totals.

### Why the naive approach fails here

198-odd stop names are informal and locally phrased — *"K-42 near kwality store
bazar"*, *"Gate#4 Sector 36"*, *"T Point"*. Geocoders answer these **confidently
and wrongly**. Three specific defences are implemented:

1. **No region suffix.** Photon AND-matches its tokens, so appending
   `", Delhi NCR, India"` turns a working query into *zero* results —
   `"Ace Aspire"` resolves correctly, `"Ace Aspire, Greater Noida West"` returns
   nothing at all. Region is supplied by a bias coordinate and a bounding box
   instead, which constrain without excluding.

2. **Token overlap is a gate, not a bonus.** A result sharing no distinctive
   token with the query is rejected however well-typed it is. Photon answers
   `"Klay School Noida"` with four *other* schools in Noida and types them all
   `house`; a scorer led by result type picks a confident wrong answer every time.

3. **IDF weighting over the school's own stop list.** A flat token count cannot
   tell `"metro station"` from `"Vishwavidyalaya"`, so Photon's answer of
   *"Karkarduma Metro Station"* scored 2 matches out of 3 and won — a confident
   point 10 km from the real stop. Weighting each token by its rarity across the
   254 stop names fixes this with no hand-maintained word list: `metro`,
   `sector` and `apartments` recur across dozens of stops and sink to near zero,
   while `vishwavidyalaya`, `kalkaji` and `gitanjali` appear once and carry the
   match.

### Pickup and drop are different places

24 stop cells pack two locations into one string:

```
P - Gate 1, Yojna Vihar D - Gitanjali Apartments, Karkardooma
P- Kaveri Gate, Jaypee Greens D- Narmada Gate, Jaypee Greens
Laxmi Nagar Metro p-Gate3,D-#1
```

Geocoding the whole string drops one marker halfway between two real places. The
splitter separates them, carries any shared prefix onto both halves, and the
morning layer uses the P point while the afternoon layer uses the D point. A
human correcting one half only writes `"<stop name>||P"` or `"||D"` in the
overrides file.

### Verified versus unverified

**Only points in `geocode_overrides.json` are treated as verified.** Everything
else — however confident the geocoder sounded — is drawn with a dashed outline,
labelled `unverified` in tables, and counted on the Overview and Data quality
tabs. Overrides always win over the cache.

`geocode_review.csv` is sorted worst-first and carries, for every point: the
query used, the returned coordinate, the address the service returned, the match
confidence, the weighted name-match score, distance to the neighbouring stop on
the same route, and a plain-English list of what is suspect. It is built for
someone to spend an afternoon in, and their corrections go straight into the
overrides file.

Road times are fetched **only for consecutive stop pairs along existing routes**
— around 500 legs, not a 254×254 matrix. A full matrix would be ~64,000 calls,
costs real money, and is not needed for a visualisation. A missing leg renders as
"no road time available", never as zero.

### The congestion calibration

For each consecutive stop pair, observed leg duration is computed from
**afternoon drop taps only** — the difference between first drop taps at
consecutive stops on the same route-day. That leg is bias-free.
`observed ÷ road duration` is then summarised by hour band, giving a congestion
factor derived from the school's own buses, which is more accurate for this
fleet than any generic traffic model.

Morning boarding taps are **not** used for this. They carry `L` and would corrupt
the calibration in a way that is invisible afterwards.

---

## Arbitrary constants

Every one of these is a judgement call. They are labelled as arbitrary in the
interface, and they all live at the top of `build_data.py`.

| Constant | Value | What it does | Sensitivity |
|---|---|---|---|
| `MIN_OBS` | 15 | below this a stop is shown but never classified | **Moderate.** At 10, ~20 more stops become classifiable, mostly on low-ridership routes with the noisiest estimates. At 20, several genuinely useful stops drop out. 15 gives roughly 3 school weeks of evidence. |
| `LATE_THRESHOLD` | 5 min | \|median − L\| beyond this reads as running late/early | **High, and it interacts with `L`.** Moving it to 3 roughly doubles the late count at any `L`; moving it to 8 roughly halves it. It is doing similar work to the slider — treat the two together. |
| `TIGHT_IQR` | 8 min | a late/early verdict also requires spread at or below this | **Moderate.** Separates "wrong printed time" from "unreliable stop". Loosening to 12 merges the two categories and makes the late count look larger than the evidence supports. |
| `UNPREDICTABLE_IQR` | 12 min | above this the stop is called unpredictable | **Low.** Only 9 stops sit in this band and the count barely moves between 10 and 15. |
| `ONTIME_BAND` | ±5 min | window for the on-time rate | **Low** for ranking, **high** for the headline percentage. It is a presentational choice, not a finding. |
| `LOW_COVERAGE` | 70% | triggers the suspect-data banner for a route-leg | **Moderate.** Chosen because the afternoon drop leg sits at 70.4% fleet-wide — just above it. At 75% most routes trip the banner and readers learn to ignore it. |
| `ORDER_VIOLATION_RATE` | 20% | share of days out of sequence that flags a stop pair | **Moderate.** Drives the fleet-wide ordering figure on the Overview. At 10% roughly half of all pairs flag; at 30% roughly a sixth. |
| `RATIO_LOW` / `RATIO_HIGH` | 0.40 / 2.50 | observed-vs-road disagreement band | **Low.** Deliberately wide. Legs outside it are almost always geocoding errors rather than traffic findings, which is what the warning says. |
| `L_MIN` / `L_MAX` / `L_DEFAULT` | 0 / 10 / 3 | scan-lag slider | **This is the big one.** See above. The default of 3 is the midpoint of the plausible range and carries no evidential weight whatsoever. |
| `LEG_MIN_MIN` / `LEG_MAX_MIN` | 0.5 / 90 min | plausible observed leg duration for calibration | **Low.** Excludes same-minute double taps and overnight artefacts. |
| `MAX_RIDE_MIN` | 150 min | quarantine threshold (pre-existing) | **Low.** 144 rows quarantined, all traceable to PM times appearing in AM columns in the vendor export. |
| `AM_DELTA_WINDOW` / `PM_DELTA_WINDOW` | (−90,120) / (−90,180) | sanity filter (pre-existing) | **Very low.** Drops 13 and 20 deltas respectively, well under 1%. |
| Geocoding bounding box | Delhi NCR | rejects out-of-region matches | **Low but load-bearing.** Without it Photon returns results in Michigan and Tanzania for some stop names. |
| `FAR_FROM_NEIGHBOUR_KM` | 3.0 | flags a stop far from its route neighbours | **Low.** A review trigger only; it never changes a number. |
| `MIN_TOKEN_OVERLAP` | 0.34 | weighted share of query tokens a geocode must match | **High for geocode quality.** Below 0.3 confident-wrong matches get through; above 0.5 legitimate partial matches are lost and the unresolved count climbs. Points between 0.34 and 0.60 are accepted but flagged `weak name match`. |

---

## Divergences from the brief's acceptance checks

Section 9 of the brief lists expected figures. The brief also says to report
divergence rather than adjust silently, so **no filter was altered to reach a
target number.** The Overview tab shows this same table live.

**23 of 31 checks reproduce exactly**, including every volume, coverage,
join-integrity and route-feasibility figure.

Reproduces exactly:

- 10,134 raw tap rows; 8 dropped as `Testing`; 10,126 usable
- 405 distinct children, 31 school days, 24 route labels in the logs
- 25 roster routes, **23 operating**
- Coverage by leg: 86.9 / 79.5 / 95.6 / 70.4 percent
- Morning median deviation +4 min; afternoon deviation sd 18.3
- Routes that cannot reach school on time: **1 at `L=0`, 7 at `L=6`, 8 at `L=8`**
- 781 off-route student-days across 104 children
- 0 stops running early at `L=0`

Diverges:

| Check | Brief | This pipeline | Why |
|---|---|---|---|
| Roster students | 444 | **443** | 453 roster rows contain 443 distinct `PRO…` admission numbers; 10 students appear twice because they are picked up by one route and dropped by another. Those are split AM/PM assignments, not duplicates. The pre-existing phase-1 report also says 443. |
| Roster stops | 198 | **254** on paper (296 with observed-only) | Definitional, and chosen deliberately. This build covers the **union of the master route list (243 pairs) and the roster (202)** = 254, because the route list carries printed stops with no current riders (evidence about network sprawl) and the roster carries drop-only points the route list omits (which would leave holes in the afternoon map). A further **42** (route, stop) pairs appear only in the tap data, under routes children were reassigned to mid-term; folding them in is what makes the browser's counts and the pipeline's agree. |
| Stops with n≥15 | 176 | **179** | Union across both legs: 169 morning, 137 afternoon. |
| Stops running late at `L`=0/3/6 | 50 / 18 / 7 | **54 / 21 / 8** | Same direction, same shape, consistently 1–4 higher. |
| Stops running early at `L`=6 | 46 | **47** | |
| Route 7 dismissal gate load | 44.4 min | **42.65 min** | The share of days over 30 minutes reproduces almost exactly: 53.6% against the brief's 54%. |

The late/early and gate-load differences are small and directionally identical,
consistent with a slightly different ordering of the regime, sanity and
quarantine filters in whichever run produced the brief — not with a parsing
error. The parsers reproduce every volume, coverage and join-integrity figure
exactly, which is where a parsing error would surface first.

### Four places where the brief's definition needed a decision

Each of these changed a number. All are reported both ways.

**1. Stop-ordering violations — 28% in the brief, 58% or 32% here depending on
one operator.** Tap times resolve to the minute, and the attendant scans
continuously as the bus moves, so on **16.6%** of tested day-pairs two
consecutive stops share a timestamp. Counting "at or before" (the brief's
wording) makes those ties violations and gives 58%. Counting strictly "before"
gives **31.6%**, close to the brief's 28%. A tie means the two stops cannot be
*ordered* — a resolution limit, not evidence the sequence is wrong — so the
dashboard reports the two separately rather than conflating them. Pairs are also
only tested where both stops are genuinely adjacent in the *printed* sequence;
roster-only stops have no printed position, and pairing them with their
neighbours manufactures violations out of an order the school never published.

**2. Route feasibility — which stop anchors the run-in.** The brief says
"printed departure from the final stop". On **10 of 25 routes the final printed
stop has no tap data at all** — nobody boards there — so the literal reading
silently leaves those routes unassessed. The dashboard anchors on the last
printed stop that actually has observations, which assesses every route and
finds more of them over-committed: **1 / 11 / 12** at `L` = 0 / 6 / 8, against
**1 / 7 / 8** under the strict reading. Both appear in the reconciliation table,
and each route says which stop its run-in was measured from.

**3. Which routes are "operating".** Judging by last tap date reports 24,
because a withdrawn route keeps collecting stray taps from children who board it
once. Route 23's tap volume collapses from ~110/week to under 5, and **no child's
dominant route is R23 at all**. Measuring recency on in-regime rows only — where
the route is the child's usual one — gives the brief's **23**, with Route 12
(7 children rostered, never runs) and Route 23 (ceased) identified by data, not
by name.

**4. Three stop names are printed twice on the same route.** R04's pair is an
outright duplicated row. R15 prints "Civil Line Red Light" at both 06:26 and
06:42, and "Main Road Near Chandgram Akhada Red Light" with two different drop
times. Tap data cannot tell the visits apart — every tap at that name collapses
to one stop — so keeping both rows would double-count the stop in every fleet
total and produce two contradictory verdicts for one physical place. They are
collapsed to one row against the earlier printed time, and the alternates are
surfaced as a warning on the stop.

---

## Known limits of this build

*The figures in this section are from the free stack. They are replaced by the
first build with Google enabled.*

**Geocoding resolves 219 of 323 distinct stop locations (68%).** The remaining
104 returned nothing that passed the name-match gate. That is the free stack's
ceiling on informal Indian addresses, and it is the single strongest argument
for the Google migration. **No point is presented as fact**: all 323 are
unverified until a human confirms them, 236 carry an explicit flag, and every
one is listed in `geocode_review.csv`.

**Road times cover 296 of 572 consecutive legs**, limited by the same missing
coordinates. A missing leg reads "no road time available", never zero.

**The congestion ratio is 0.54× — below 1, which is not credible.** It says the
buses beat free-flow driving times across the NCR. The cause is almost certainly
stop coordinates: legs between mislocated stops carry inflated road distances,
which deflates the ratio. The dashboard states this in place of presenting 0.54×
as a congestion factor. Two things would fix it, in order: correcting the flagged
coordinates, and moving to a traffic-aware road-time provider. Until then the
calibration is a **geocoding diagnostic, not a traffic finding** — which is
itself the useful reading, and is how it is labelled.

---

## Things this dashboard deliberately does not do

- **No student home locations, anywhere.** Stops only. Homes are not in this
  dataset and plotting them on a shared screen would be a privacy problem.
- **No attribution to any named driver, attendant or vehicle.** Scan compliance
  is far too weak to support it, and it is not the point.
- **Student names appear only on the Exceptions tab**, where identifying the
  child is the operational purpose.
- **No single-point arrival times presented as promises.** Every expected
  arrival is shown as a p10–p90 window. A bus does not arrive at 06:33; it
  arrives between 06:31 and 06:44 on eight days out of ten. Single-point times
  are the original sin this project exists to correct.
- **`n` is shown next to every statistic without exception.** A stop with n=4 is
  never presented like a stop with n=31.
- **No recommendations.** Nothing here says what should change. That is the next
  version.

---

## Route optimiser (foundation, not yet run on real coordinates)

`pipeline/optimiser/` redesigns the **whole network**, not one route at a time: it
decides which bus serves which stop and in what order, for the morning and the
afternoon separately, and compares the result against today's plan using the
same scorer and the same travel-time matrix. `build_routes.py` still does the
narrower job of re-ordering stops within each existing route.

It is built and tested. It has **not produced a usable plan yet**, because no
stop coordinate is verified. By default it will only route verified stops.

### Workflow once coordinates are collected

```bash
python pipeline/run_optimiser.py --export-coord-template   # 1. outputs/optimiser/stop_coordinates_template.csv
#    fill the google_maps column: right-click the pin in Google Maps, copy "28.57012, 77.39105"
python pipeline/run_optimiser.py --import-coords filled.csv # 2. writes cache/geocode_overrides.json (backup kept)
python pipeline/run_optimiser.py                            # 3. writes outputs/optimiser/
```

The template has one row per physical location, with the school and each
pickup/drop half of a combined cell as separate rows. Missing locations come
first, then the stops carrying the most children. Imported points become
"verified" for the dashboard too. A lat/lon outside the NCR box, usually a
swapped pair, is rejected by name.

`--demo` runs the whole thing on a synthetic 200-stop network. `--allow-unverified`
routes geocoder guesses and stamps every output **DRAFT**. Tests:
`python -m unittest discover tests`.

### What it optimises

One weighted objective, in minute-equivalents. All weights are in
`optimiser_config.json` (copy `optimiser_config.example.json`):

| Term | Default | Meaning |
|---|---|---|
| `bus_fixed` | 60 | each bus used, so the solver can take buses off the road |
| `drive_min` / `km` | 1.0 / 0.5 | operating cost |
| `student_ride_min` | 0.05 | every child-minute on board |
| `reassigned_student` | 2.0 | each child moved off today's bus, which keeps plans stable |
| `violation` | 1000 | per seat over capacity or minute over a time limit |

Hard rules, enforced through the violation penalty: seats per bus, the longest
ride a child may take, the school reporting time (AM), and the earliest pickup
and latest drop. **Capacity, ride limit, dismissal time and the time window are
ASSUMPTIONS.** They are not in any source file.

### How

- **Travel times** (`matrix.py`): `haversine` (offline), `osrm` (free, free-flow)
  or `google` (Routes API `computeRouteMatrix`, traffic-aware at the run's
  departure hour). Real road times are fetched only for each stop's 15 nearest
  neighbours and to/from school. Other pairs are estimated from straight-line
  distance, calibrated on the real ones. That is about 7,500 elements instead of
  62,500, and every plan reports its share of estimated legs. Everything is
  cached in `cache/optimiser_matrix_cache.json`.
  On Google this is billed **per element**, and the matrix is the one thing here
  that does not fit the free tier comfortably: at `neighbours: 15` a full network
  of ~300 stops needs roughly 12,000 elements for AM + PM, against a 4,000 cap.
  The fetch is checked against the cap **before** the first request and is not
  started if it does not fit (estimates are used and the log says how many
  elements it needed). Options: `neighbours: 6–8`, `matrix_provider: "osrm"`, or
  raising `GMAPS_CAP_ROUTE_MATRIX_PRO` for one deliberate run (~$70 past the free
  allowance at k=15).
- **Solver** (`solver.py`, pure Python + numpy): it starts from both today's
  routes and a Clarke-Wright construction, then runs relocate / swap / 2-opt /
  2-opt* local search, then ruin-and-recreate until the time limit.
- **Stop consolidation** (`consolidate.py`): proposes merging stops within
  250 m, and flags the same place served by two buses. The merged stop is always
  an existing stop, never a midpoint. It only proposes unless
  `apply_stop_merges` is on.
- **Held stops**: a stop with no usable coordinate stays on today's bus with its
  seats reserved. The time it adds to a run is *not* counted; the report says so.

### Outputs (`outputs/optimiser/`)

`summary.md` (today vs optimised, per bus), `plan.json` (every stop and ETA),
`student_changes.csv` (per admission number: bus today → proposed, time shift),
`readiness.csv`, `stop_merges.csv`. It also lists stops that no bus can reach
within the ride limit even when serving nothing else. Those are usually wrong
pins.

### Not done yet

- A map view of the proposal. `plan.json` carries the coordinates, so
  `routes_template.html` could render it.
- Pickup-time windows per stop ("not before 06:30 at X").
- Retiring a specific bus. Today only spare buses can be *added* (`extra_vehicles`).
- Bus depot / garage legs. Runs are open paths that start at the first stop.
- Calibrating `time_multiplier_*` from the afternoon drop-tap legs once
  coordinates are right. The dashboard already computes the ratio.
