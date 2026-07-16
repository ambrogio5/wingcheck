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

## Unverified candidates

| id | Name | Roles | Confidence note |
|---|---|---|---|
| `cor` | Corvatsch (summit) | `summit` | Cable-car-served summit station; unclear if it's a full SwissMetNet member vs. an SLF/IMIS-only or ski-operator-only sensor - a real sync must answer this. |
| `piz_nair` | Piz Nair (St. Moritz) | `summit` | No station code could be proposed with any confidence at all. |
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

1. Run `python3 historical_data.py sync --station <id>` (or `sync` with no
   argument to attempt every registered station).
2. Inspect the actual returned data - `python3 historical_data.py
   coverage --station <id>` shows record count and date range.
3. If real data came back, a human edits `config/stations.json` by hand:
   set `"verification": "confirmed"` and `"enabled": true`, and fill in
   `available_variables`/`historical_available`/`live_available` from what
   was actually observed.
4. Only then may `station_analysis.py` treat features derived from that
   station as anything more than a diagnostic-only "insufficient
   coverage" result.

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
