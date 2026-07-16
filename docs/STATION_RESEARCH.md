# Station research: what's registered, and its status

This document is the human-readable companion to **`config/stations.json`**
(the machine-readable registry, loaded via `station_registry.py`) - if the
two ever disagree, `config/stations.json` is the source of truth.

## Sandbox network constraint (read this first)

This branch's research session ran in an environment where outbound
network access to `data.geo.admin.ch` (MeteoSwiss's open-data host) was
blocked at the gateway/proxy level - confirmed by `historical_data.py
sync` itself, which logs `[warn] could not fetch <station> recent data:
... ProxyError ... 403 Forbidden` for every station and falls back to
ingesting the already-committed `logs/raw_cache/*.json` files. This means
**no station beyond the three this project already had real cached data
for could be verified against a live source this session.**

Every other candidate in `config/stations.json` is recorded with
`"verification": "unverified"` and `"enabled": false`. **Do not treat
either as ground truth** - a wrong guessed station code fails loudly (the
MeteoSwiss STAC API 404s / returns no assets) rather than silently
returning wrong data, so attempting a real
`historical_data.py sync --station <id>` against these is safe, but no
result should be reported as validated until that sync has actually
returned real data and a human has updated the entry's `verification` and
`enabled` fields after inspecting it.

## Confirmed, enabled stations

| id | Name | Roles | Evidence |
|---|---|---|---|
| `sam` | Samedan | `target_region`, `ground_truth` | 399,181 real hourly records in `logs/raw_cache/samedan_archive.json`; `fu3010h0`/`fu3010h1` wind columns confirmed against the live MeteoSwiss STAC API |
| `lug` | Lugano | `synoptic_pressure` | 196,835 real hourly records in `logs/raw_cache/pressure_lug.json`; `pp0qffh0` confirmed live |
| `sma` | Zürich / Fluntern | `synoptic_pressure` | 196,770 real hourly records in `logs/raw_cache/pressure_sma.json`; `pp0qffh0` confirmed live |

These are the only three stations with `"verification": "confirmed"` and
`"enabled": true` - enforced by `tests/test_station_registry.py`'s honesty
invariants (an enabled station must be confirmed; an unverified station
must never be enabled).

**Why `pressure_family_score` is the only new candidate feature with real
historical coverage** (see `station_analysis.py`'s report): both stations
behind it - `lug` and `sma` - are confirmed and enabled. Every other new
candidate family (source heating, pass activation, summit support,
radiation/moisture) depends on a station role with zero confirmed
stations, so those families are honestly reported as having zero real
coverage rather than a fabricated result.

## Corvatsch (COV) - identity confirmed, metadata pending a real fetch

`cov` (Piz Corvatsch, official MeteoSwiss abbreviation **COV**) is different
from every other entry below: its existence as a genuine, official
MeteoSwiss automatic station is **confirmed** - given directly, not guessed
(an earlier iteration of this registry used the wrong, invented abbreviation
`cor` for the same physical station; that entry has been replaced). What
remains unverified is the exact metadata (latitude/longitude/elevation,
available variables) and a real successful fetch in this repo's own
environment - both were attempted this session
(`meteoswiss.fetch_station_metadata('cov')` against the official
`ogd-smn_meta_stations.csv`, and `meteoswiss.fetch_station_observations('cov')`
against the STAC catalog) and blocked by the sandbox's network policy (the
same `403 ProxyError` documented elsewhere in this file's history for
sam/lug/sma). See `docs/DATA_ARCHITECTURE.md`'s Corvatsch section for the
approximate elevation given in the task specification (~3294-3297m, deliberately
NOT written into `config/stations.json`'s `elevation_m` field until a real
fetch confirms it) and `historical_data.py`'s generic-station sync path
(`meteoswiss.fetch_station_metadata`/`fetch_station_observations`, added this
session) that will complete this the moment it runs somewhere with real
network access - see Part 15 of this PR's own verification steps.

`config/stations.json`'s `cov` entry stays `verification: "unverified",
enabled: false` until that real fetch succeeds and a human inspects the
result - the same bar every other station in this registry must clear,
despite COV's identity already being known.

## Unverified candidates

| id | Name | Roles | Confidence note |
|---|---|---|---|
| `piz_nair` | Piz Nair (St. Moritz) | `summit` | Investigated in this PR - see `docs/PIZ_NAIR_DATA_SOURCE.md`. Not a MeteoSwiss OGD station; no confirmed machine-readable feed found. |
| `maloja` | Maloja | `pass`, `source_region` | One of Switzerland's oldest continuously operated climate stations - moderate confidence a MeteoSwiss station exists here, but the exact current API code was not confirmed. |
| `sils` | Sils / Segl | `target_region` | The lake immediately upstream of Silvaplana - **the single highest-priority candidate for the next real sync attempt**, since it would be a genuinely new, non-redundant signal if it exists. |
| `vicosoprano` | Vicosoprano | `source_region` | **Important**: these coordinates are already used in `features.py` as an Open-Meteo *forecast-model grid point*, not a real ground station - no evidence was found that Vicosoprano hosts an actual MeteoSwiss station. Do not conflate the two. |
| `poschiavo` | Poschiavo | `competing_flow` | Eastern/Bernina-flow-suppression context candidate. |
| `chur` | Chur | `synoptic_pressure`, `down_valley` | Well-known long-record station; moderate confidence it exists under some code, not confirmed. |

No candidate is marked `enabled: true`. Flipping that flag requires an
actual successful `historical_data.py sync --station <id>` fetch,
inspected by a human, followed by updating `config/stations.json` by
hand - never automatically, and never based on a station merely being
named in a task description or general regional knowledge.

## Rejected candidates

Ski-resort webcam/weather widgets (e.g. a cable-car operator's own
"current conditions" page) are **not** treated as stations in this
registry - they are presentation layers, typically over either a
MeteoSwiss feed or a private sensor with no documented API, no historical
archive, and no stable machine-readable access. Per the explicit rule: do
not assume a named station exists merely because a webcam or resort page
displays weather.

## What would change a candidate's status

1. Run `python3 historical_data.py sync --station <id>` - explicitly
   naming a not-yet-enabled candidate makes `sync()` probe it for real
   (see the "sync() bootstrap fix" note below); `sync` with no argument
   only ever touches already-enabled stations (sam/lug/sma today), never
   an unconfirmed candidate - that's a deliberate safety property of the
   routine/scheduled sync, not a way to attempt every registered station.
2. Inspect the actual returned data - `python3 historical_data.py
   coverage --station <id>` shows record count and date range.
3. If real data came back, a human edits `config/stations.json` by hand:
   set `"verification": "confirmed"` and `"enabled": true`, and fill in
   `available_variables`/`historical_available`/`live_available` from what
   was actually observed.
4. Only then may `station_analysis.py` treat features derived from that
   station as anything more than a diagnostic-only "insufficient
   coverage" result.

**`sync()` bootstrap fix**: `sync()` used to refuse to attempt a live
fetch for ANY not-yet-`enabled` station, even when explicitly named via
`--station <id>` - which made step 1 above impossible to actually carry
out (there was no way to get real data back for a candidate to inspect,
since fetching was gated behind the very flag inspecting the data was
supposed to justify flipping). Fixed: an explicitly-named station now
bypasses that gate and gets a real fetch attempt; the default,
no-argument `sync()` (used by the scheduled `sync_historical_data` job)
is unaffected and still only ever touches already-enabled stations. The
`sync_historical_data` workflow's manual dispatch also gained an optional
`probe_station` input that runs this explicit probe in CI (a real
network-enabled environment, unlike the sandbox this branch was
originally developed in) and commits only the resulting coverage
manifest entry - never `enabled`/`verification` themselves, which still
require a human to edit by hand after inspecting the result.

## Feature-promotion prohibition (this PR)

This PR's task explicitly forbids promoting any new feature into the
production model. `station_analysis.py`'s five new candidate family
scores (`source_heating_score`, `summit_support_score`,
`pressure_family_score`, `radiation_family_score`, `competing_flow_score`)
are used ONLY inside `station_analysis.py`'s own research comparisons -
none of them are added to `features.FEATURE_NAMES`, none of them are
scored by the production model, and `weights.json` is never touched by
any research script (enforced by `tests/test_station_analysis.py`
asserting its mtime is unchanged before/after a full run). Promoting a
candidate into production would require, at minimum: the underlying
station being confirmed (see above), a rolling-origin evaluation showing
a stable, non-trivial improvement across multiple folds (not just the
repeatedly-inspected 2026 reference), and a deliberate, separate,
human-reviewed source-code change - none of which this PR does or is
scoped to do.

## PR #5 overlap - not cherry-picked into this PR

An earlier, separate research effort (PR #5, `agent/local-station-research`,
still open as a draft) built infrastructure that substantially overlaps
with what merged as PR #6 (`agent/maloja-data-and-diagnostics`'s base) -
both added a station registry, a historical archive, and a
`station_analysis.py`-style research driver, developed independently
before either was merged. PR #6 was merged; **PR #5 was not, and this PR
does not cherry-pick it wholesale** - its branch now conflicts with
`main` (`mergeable_state: dirty`) and would need a real merge-conflict
resolution pass, not a blind cherry-pick, to bring in cleanly.

PR #5 does contain a few real, useful findings that are **not yet on
`main`** and are explicitly out of scope for this PR (which is limited to
pipeline hardening, Corvatsch ingestion, the bounded Piz Nair
investigation, and the corresponding fixed Corvatsch analysis - see this
PR's own description):

- **Calibration research** (`calibration.py`/`calibration_analysis.py`
  in PR #5): Platt scaling and isotonic regression compared against the
  uncalibrated production model per rolling fold - ECE improved in 4/6
  folds. `station_analysis.py` in this PR (via PR #6) already has a
  lighter-weight reliability-table/ECE diagnostic, but not the actual
  Platt/isotonic fitting PR #5 implemented.
- **Northerly/easterly regime analysis** (`regimes.py`/`regime_analysis.py`
  in PR #5): a rule-based weather-regime classifier showing
  `northerly_suppression` (26.5% false-positive share) and
  `easterly_suppression` (20.7%) as the two regimes driving most false
  GOOD/MARGINAL alerts. This PR's own Part 11 Corvatsch analysis uses a
  much smaller, bounded regime-proxy breakdown (`REGIME_PROXIES` in
  `station_analysis.py`) precisely because it could not reconstruct PR
  #5's richer regime taxonomy from stored historical features (raw wind
  direction isn't retained) - PR #5's version, if ported forward
  properly, would be a real improvement over the proxy used here.
- **Continuous wind-target research** (`continuous_target_analysis.py`
  in PR #5): a continuous wind-speed regression and daily-session-target
  comparison, finding the existing max-hourly-probability aggregation
  already matches or beats a dedicated daily model.

**Recommendation**: handle each of these in its own later, focused PR -
resolve PR #5's conflicts against current `main` deliberately (not via a
blind cherry-pick, since several of its files, e.g. `stations.py` vs.
this codebase's `station_registry.py`/`config/stations.json`, now
diverge in both name and structure from what actually merged), re-verify
its findings still hold against the current dataset, and then close PR
#5 once its useful parts have been ported. This PR does not attempt that
port.
