# Historical data architecture

This document describes `logs/historical/` (the durable archive built for
station research and forecast-vintage preservation) and how it relates to
the project's existing `logs/*.jsonl` operational logs and
`logs/raw_cache/` backtest cache. See also `docs/STATION_RESEARCH.md` for
which stations feed this archive, and `docs/MALOJA_DIAGNOSTICS.md` for how
the derived station features are turned into diagnostics.

## Why this exists

Before this archive, the only historical station data in the repo was
`logs/raw_cache/*.json` - a compact but purely internal cache of exactly
what `backtest.py` needs for its own labeling, with no explicit schema, no
checksums, and no path to extending it to new stations without rewriting
backtest-specific code. `logs/historical/` is a separate, general-purpose,
provenance-tracked archive: any future confirmed station can be added by
describing it in `config/stations.json`, not by writing new one-off
fetch/parse code, and every raw asset it ever ingests is checksummed and
logged.

## Directory layout

```
logs/historical/
  manifests/
    stations.json     # committed - auto-regenerated coverage snapshot (record counts, date ranges)
    assets.jsonl       # committed - append-only, deduped-by-checksum log of every raw asset ingested
  station_raw/         # reserved for raw provider payloads too large/provider-specific to normalize
                        # losslessly (currently empty - see "What's committed" below)
  station_hourly/      # (gitignored) normalized per-station hourly JSONL, canonical schema
  forecast_vintages/   # committed - genuine forecast-model payloads, gzip-compressed, by issue date
    YYYY/MM/DD/<issued-at>_<provider>_<model>.json.gz
    index.jsonl        # append-only index of every archived vintage
  reports/              # committed - timestamped JSON+Markdown output of every station_analysis.py run
```

Note: `config/stations.json` (the hand-maintained station **registry** -
what stations exist, their roles, whether they're confirmed/enabled) is a
different file from `logs/historical/manifests/stations.json` (an
auto-regenerated **coverage snapshot** of that same registry plus whatever
data has actually been synced) - don't confuse the two; only the latter is
ever rewritten by `historical_data.py`.

## What's committed vs. regenerable

1. **Irreplaceable - always committed:**
   - `manifests/stations.json`, `manifests/assets.jsonl` (small - tens of
     KB, not the station data itself)
   - `forecast_vintages/**/*.json.gz` + `index.jsonl` - once a forecast's
     lead time passes, that exact vintage can never be recovered from any
     live API again (Open-Meteo's live endpoint only serves a rolling ~3
     months, and even `backtest.py`'s historical-archive fetch is 0-hour
     data, not a genuine multi-day-lead forecast - see `features.py`'s own
     docstring). Gzip-compressed and deduped by content checksum
     specifically so this permanent commitment stays small.
   - `reports/*.json` + `*.md` - each `station_analysis.py` run's
     timestamped, provenance-tagged output.

2. **Regenerable - gitignored** (see `.gitignore`):
   - `station_hourly/*.jsonl`
   - **Why**: the normalized hourly schema (`historical_data.NORMALIZED_FIELDS`)
     repeats station metadata (coordinates, elevation, provider) on every
     single hourly row - substantially more verbose per record than the
     existing compact `raw_cache/*.json` format. A full sync of the three
     confirmed stations' multi-year history produces several hundred MB of
     derived JSONL - regenerable from the small, committed `raw_cache/` +
     manifests in seconds via `python3 historical_data.py sync`, so
     committing it permanently would be pure repository bloat. This
     mirrors a real repository-size lesson from an earlier, adjacent
     research effort on this project - avoid repeating it.

## Canonical normalized hourly schema

Every station-hourly record, regardless of source station or provider, is
normalized to exactly this field list (`historical_data.NORMALIZED_FIELDS`):

```
timestamp_utc, timestamp_local, station_id, provider,
latitude, longitude, elevation_m,
temperature_c, dew_point_c, relative_humidity_pct,
pressure_station_hpa, pressure_sea_level_hpa,
wind_speed_ms, wind_gust_ms, wind_direction_deg,
precipitation_mm, sunshine_duration_min, global_radiation_wm2,
clouds_raw,
source_asset, retrieved_at, quality_flags
```

`clouds_raw` (added for the Sils manual import - see "Manual imports"
below) is a free-form provider-specific cloud-state string (e.g. a
METAR-style code like `"SKC"`) with no normalized numeric mapping of its
own - preserved as-is rather than discarded, `null` for every station that
doesn't report it.

Rules:
- Missing values are `null`, never a fabricated placeholder.
- Both `timestamp_utc` (timezone-aware ISO) and `timestamp_local`
  (Europe/Zurich) are stored explicitly - no naive, ambiguous timestamps.
  This is what lets `data_quality.find_timestamp_gaps()` detect real gaps
  without DST transitions producing false positives (gap detection works
  entirely in UTC).
- Units are normalized (wind speed always m/s, converted from km/h at
  ingest) - never mixed units across stations.
- `quality_flags` holds `data_quality.py`'s findings for that record -
  flagged, never silently discarded.

## Station registry (`config/stations.json` / `station_registry.py`)

Every station this project knows about (in production use or under
research) is described by one entry:

```json
{
  "station_id": "sam", "name": "Samedan", "provider": "meteoswiss",
  "latitude": 46.5335, "longitude": 9.8794, "elevation_m": 1705,
  "roles": ["target_region", "ground_truth"],
  "available_variables": ["wind_speed_ms", "wind_gust_ms"],
  "historical_available": true, "live_available": true,
  "licence": "MeteoSwiss Open Data (...)",
  "reporting_delay_minutes": 10,
  "enabled": true, "verification": "confirmed", "notes": "..."
}
```

Supported roles: `source_region`, `pass`, `target_region`, `down_valley`,
`summit`, `synoptic_pressure`, `competing_flow`, `ground_truth`.
`station_registry.py` loads and validates this file (`validate_registry()`
enforces that an `enabled` station is always `confirmed`, and that no
`unverified` station is ever `enabled`) and provides lookup helpers
(`enabled_station_ids()`, `stations_by_role()`, `stations_by_provider()`).
See `docs/STATION_RESEARCH.md` for the full narrative on every registered
station's status.

## Sync/validate/coverage CLI (`historical_data.py`)

```bash
python3 historical_data.py sync                       # sync every enabled station
python3 historical_data.py sync --station sam lug sma  # sync specific stations only
python3 historical_data.py validate                     # data-quality + continuity checks
python3 historical_data.py coverage                     # per-station record counts and date ranges
python3 historical_data.py coverage --station sam
```

`sync` is idempotent: for each enabled station it first tries ingesting
the already-committed `logs/raw_cache/*.json` file (no network), then
attempts a best-effort real network fetch for the recent tail (catches
every exception - safe in network-restricted environments), normalizes
both sources, and merges via `merge_normalized_records()` - which **never
overwrites a record with more non-null fields with one that has fewer**.
Re-running `sync` with no new data reports `"added": 0` rather than
rewriting anything. Every raw asset ingested (whether from `raw_cache` or
a live fetch) is logged to `manifests/assets.jsonl`, deduped by
`(station_id, checksum)`.

## Manual imports (`manual_station_import.py`, `historical_data.py import-csv`)

Not every real station has an API. A station in
`historical_data.NO_LIVE_SOURCE_STATIONS` (currently just `sils` - see
`docs/STATION_RESEARCH.md`'s "Sils / Segl (Silser See) manual import"
section) has no live/API access at all, ever - `_attempt_live_fetch()` and
`station_nowcast.py`'s `_fetch_normalized_recent()` both short-circuit for
it before reaching the generic MeteoSwiss-fetch fallback that every other
non-role-specific station goes through. Its only data source is a
user-provided file, ingested via:

```bash
python3 historical_data.py import-csv --station <id> --file <path> --format <name>
```

`manual_station_import.PARSERS` registers parsers by FILE FORMAT (not
station), since a future upload for a different station may share the
exact same layout. Each parser returns the same
`{datetime_utc: {normalized_field_name: value}}` shape
`normalize_generic_observations()` already expects from a live MeteoSwiss
fetch, so no new normalize function or storage format was needed - only a
new parser. `import_manual_csv()` merges (never overwrites) the parsed
result into the same durable, committed `logs/raw_cache/generic_<id>.json`
convention already used for `cov`, then runs the normal `sync([id])` path.
Because `sync` merges records instead of replacing the file, importing
additional files later (more dates, or a different station) for the same
manually-sourced id just accumulates.

## Candidate signals on probation (`candidate_signals.py`)

Three new signals are being **logged but NOT scored** while we accumulate
enough real history to evaluate them honestly. They are candidates on
probation, subject to the same maturity discipline as the SIA/lake ratio
in `config/ground_truth_policy.json`: nothing here touches
`features.py`, `weights.json`, model scoring, or the dashboard tiers.
`candidate_signals.py` fetches the 10-minute recent files for the
user-verified official MeteoSwiss stations via
`meteoswiss.fetch_station_raw_10min` (a low-level, raw-column-addressed
extension of the existing fetch pattern — needed because QNH pressure has
no `NORMALIZED_FIELDS` slot), derives the three signals, and appends one
record per 10-minute UTC observation timestamp (deduped, forward-only) to
`logs/candidate_signals.jsonl` — committed, small, append-only. It runs in
the existing `forecast` job (right after `station_nowcast.py`, which
already fetches SIA/COV/SAM), `continue-on-error` so a probation-signal
hiccup can never fail the real forecast.

The signals (every raw component is stored beside every derived value so
they can be re-derived differently later, no re-fetch):

1. **`corvatsch_wind`** — COV (Piz Corvatsch, 3294 m) `fu3010z0` speed /
   `fu3010z1` gust / `dkl010z0` direction. Free-air flow aloft; the
   hypothesis is it gates whether the valley thermal establishes.
2. **`bregaglia_engadin_gradient`** — sea-level-reduced pressure
   difference VIO (Vicosoprano, 1089 m, warm south side of the Maloja
   pass) minus SAM (Samedan, 1709 m) and minus SIA (Segl-Maria, 1804 m).
   **Uses a sea-level-reduced field (QFF `pp0qffs0` preferred, QNH
   `pp0qnhs0` fallback), never the raw station pressure `prestas0`** — the
   raw difference is ~62 hPa of pure altitude offset (VIO sits 620 m below
   SAM) and is meteorologically meaningless. The reduction field actually
   used is recorded per station in each record's provenance.
3. **`valley_summit_temp_spread`** — `tre200s0`(SIA) minus `tre200s0`(COV),
   a thermal-buildup proxy that showed a faint ~1 h lead on lake wind in
   the initial 2-day sample.

**Why probation (do not trust the 2-day sample):** the 2-day correlations
(COV daytime wind r≈0.80, VIO−SAM gradient r≈0.66) are almost entirely a
*between-day* artifact — Friday was windy, Saturday was not, so anything
that also differs Fri/Sat correlates by coincidence rather than mechanism.
COV free-air wind even flips sign within a single afternoon. None of these
is trustworthy until there are ~2 weeks of **independent days**, at which
point they can be evaluated against the lake reading the same way any
candidate feature must be before promotion. `vio` is fetched by its
official abbreviation and kept strictly separate from the pre-existing
`vicosoprano` entry in `config/stations.json`, which is an Open-Meteo
forecast-grid point, **not** this MeteoSwiss ground station — they are not
conflated, and `vio` is deliberately not added to the station registry
until a human confirms the fetch (see that entry's own note and
`docs/STATION_RESEARCH.md`'s no-guessing rule).

## Data quality (`data_quality.py`)

Wired into `historical_data.py validate`. Checks, per record: implausible
values (`PLAUSIBLE_RANGES`), negative wind speed, gust less than sustained
speed (flagged, not discarded - occasional real short-duration lulls can
legitimately produce this), future timestamps. Across a station's full
record set: duplicate timestamps and timestamp gaps (UTC-based, DST-safe).
`validate_sync_health()` additionally flags an enabled station whose
archive has gone completely empty, or hasn't seen new data in
`STALE_ARCHIVE_DAYS` (30) days. **Every finding is preserved as a flag,
never used as a reason to silently drop data.**

## Forecast vintages (`forecast_vintages.py`)

Every live forecast run (`forecast_and_log.py`, 07:00 and 10:00 CEST) now
archives the raw multi-model forecast payload *before* scoring it, via
`archive_forecast_payload_safe()` - a best-effort wrapper that logs any
failure visibly to stderr but never lets archiving break the actual
forecast/Telegram send. Deduped by content checksum
(`FORECAST_PAYLOAD_KEYS` excludes the station-nowcast fields, which are
refetched fresh each call and would otherwise defeat dedup for what is
really the same forecast-model response). Stored gzip-compressed under
`forecast_vintages/YYYY/MM/DD/<issued-at>_<provider>_<model>.json.gz` with
issue time, target times, and lead-time-in-hours all recorded explicitly
and separately.

## Reports (`station_analysis.py`)

Every run writes a new, never-overwritten, timestamped JSON+Markdown
report to `logs/historical/reports/`, with a provenance envelope (git
commit SHA, checksum of the input dataset file, configuration, warnings,
limitations) via `research_report.py`. `station_analysis.py` itself never
writes `weights.json` or `docs/dashboard_data.json` - see
`docs/STATION_RESEARCH.md`'s feature-promotion-prohibition section.

## Repository-size considerations

- Forecast vintages grow by roughly one small gzip file per forecast run
  (2/day) - trivial over any realistic timeframe.
- Station archives are intentionally NOT committed in normalized form -
  only the raw provider cache (`logs/raw_cache/`, already small and
  already committed for `backtest.py`'s purposes) and the manifests are
  permanent.
- Reports are small (tens to low hundreds of KB each) and accumulate
  slowly (one per manual research run).

## Disaster recovery / rebuilding from scratch

`station_hourly/` can be fully regenerated from committed data alone:

```bash
python3 historical_data.py sync   # rebuilds station_hourly/ from raw_cache/ + the registry
```

`forecast_vintages/` and `reports/` cannot be regenerated - they are the
permanent record of what a forecast said or what an analysis found at a
point in time. `manifests/stations.json` can always be rebuilt (it's a
pure snapshot derived from `config/stations.json` plus whatever's on disk)
via any `sync`/`coverage` call.

## Licensing and provenance

### Ground-truth registry and SIA calibration

`logs/historical/ground_truth/observations.jsonl` is the derived canonical
registry used to prepare labels. It deliberately supports several records per
UTC timestamp: lake/Windsurfcenter, SIA and Samedan are not collapsed during
ingestion. Each row retains station ID, original source asset/retrieval
metadata, quality flags, validation status and confidence.

Selection is a separate policy operation (`config/ground_truth_policy.json`,
policy_version 2 - the provisional SIA-first policy): direct lake
measurements (source `kitesailing`) always win; SIA is the principal
reference when no lake reading exists; a missing lake+SIA hour stays
UNLABELED. **Samedan is deliberately no longer a default label**
(`allow_samedan_fallback: false`) - it stays in the registry as preserved
context and may only be re-enabled inside an explicitly named research
experiment (`legacy_samedan_proxy`), never silently. SIA's measurement
quality (official MeteoSwiss station) is kept separate from its
equivalence to the Windsurfcenter/lake target, which is UNMEASURED - the
policy's `sia_confidence` is null on purpose, and
`station_calibration.py`'s maturity gates (fewer than 14 independent
overlapping days = insufficient; 14-41 = preliminary; 42+ = calibration
candidate) govern what may even be *reported*, let alone acted on. The
analysis command never edits policy or production weights - any policy
change is a reviewed edit of the versioned policy file citing a real
calibration report.

### SIA ingestion - real coverage across two distinct products

Official SIA identity metadata is snapshotted in
`config/sia_official_metadata.json`, verified against a real user-supplied
copy of MeteoSwiss's `ogd-smn_meta_stations.csv`.

**The genuine HOURLY ("_h_") product** was confirmed by a real
network-enabled CI fetch (GitHub Actions run 29616366900, 2026-07-17):
**108,118 records** (50,760 from the 2010-2019 decade file + 52,608 from
2020-2029 + 4,728 recent + 22 now), 0 quality flags, 0 gaps. An earlier
"108,116 rows from 2014-03-18" figure - flagged unverifiable when only
local files existed - is consistent with this product (±the moving
recent/now tail). CI's `historical_data.py sync` re-fetches it on demand;
the normalized output stays gitignored/regenerable, with real coverage
figures recorded in `manifests/stations.json` on every CI sync.

**The 10-MINUTE ("_t_") product** held locally (two user-supplied raw
files, preserved unmodified + sha256-checksummed under
`logs/historical/raw/meteoswiss/sia/`, ingested by `sia_import.py`):

- `ogd-smn_sia_t_historical_2000-2009.csv`: 6,472 rows, 2004-02-01 through
  2009-12-31, at three fixed synoptic hours per day (06/12/18) -
  temperature/humidity/dew point/vapour pressure ONLY, **no wind**.
- `ogd-smn_sia_t_recent.csv`: 28,368 rows, true 10-minute cadence,
  2026-01-01 through 2026-07-16 - full variable set (wind fu3010z0/fkl010z0,
  gust fu3010z1, direction dkl010z0, QFE prestas0, precipitation,
  radiation, sunshine; column meanings verified against the official
  `ogd-smn_meta_parameters.csv`, never guessed).
- **Open gap in the LOCAL 10-minute holdings: 2010 through 2025** (the
  `_t_historical_2010-2019` / `2020-2029` decade files have not been
  acquired). The genuine HOURLY product above covers that span - the gap
  applies to sub-hourly resolution only.

`logs/historical/station_10min/sia.jsonl` holds the normalized real
10-minute records; `logs/historical/station_hourly/sia.jsonl` holds, in a
local-only checkout, top-of-hour aggregates honestly DERIVED from them
(arithmetic mean for scalars, vector/circular mean for direction, max for
gust; every record flagged `derived_from_10min_mean` + `n_10min_samples:N`;
an hour with no real 10-minute data simply does not exist - never
interpolated). A CI sync merges the genuine hourly ("_h_") product on top
via the richer-record-wins merge, superseding derived records where both
cover an hour. Both normalized files are gitignored/regenerable; the raw
files + checksums in `manifests/assets.jsonl` are the committed source of
truth.

### Station identity rules (sils / sia / kitesailing / windsurfcenter)

Nearby names are NOT merged without conclusive evidence: `sils` (a 22-row
user-provided sample, 2014-04-02) is kept distinct from `sia` - its
station-level pressure (845-848 hPa, ~1550m equivalent) is ~25 hPa above
SIA's real measured QFE (~816-826 hPa at 1804m), and its format (knots,
METAR cloud codes) matches no MeteoSwiss product. Likewise the live
`kitesailing` scrape is NOT labeled `windsurfcenter` - the widget is
embedded on kitesailing.ch and its upstream sensor identity has not been
demonstrated; `windsurfcenter_silvaplana` exists in the registry as an
explicitly unverified, dataless placeholder until the real archive is
acquired. If two names are ever proven aliases of one physical station,
document the evidence and introduce a canonical ID with preserved source
aliases - never silently collapse them.

`retraining_dataset.py` joins the current feature rows to approved registry
labels, writes label provenance (source, station, confidence,
policy_version, source provenance) into every output row and reports
exclusions plus coverage by source/year/month/confidence. It stops before
model training so calibration can be reviewed first.
`model_comparison_sia.py` is the research-only comparison harness (fresh
`model.new_weights()` per fold, day-grouped chronological splits, asserts
`weights.json` is byte-identical before/after).

Confirmed MeteoSwiss stations (`sam`, `sia`, `lug`, `sma`, `cov`) are
SwissMetNet stations under MeteoSwiss Open Data (opendata.swiss terms of
use - attribution required, commercial use permitted). Every candidate
station's presumed licence is recorded in `config/stations.json`'s
`licence` field, explicitly `"unknown"` where it could not be determined -
a candidate's licence must be confirmed before its data is used for
anything beyond internal research.
