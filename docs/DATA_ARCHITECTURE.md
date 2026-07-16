# Historical data architecture

This document describes `logs/historical/` - the durable archive built for
long-term retraining and station research - and how it relates to the
project's existing, smaller `logs/*.jsonl` operational logs and
`logs/raw_cache/` backtest cache. See also `docs/STATION_RESEARCH.md` for
which stations feed this archive, and `CLAUDE.md` for how the archive fits
into the overall pipeline.

## Why this exists

Before this archive, the only historical station data in the repo was
`logs/raw_cache/*.json` - a compact but purely internal cache of exactly
what `backtest.py` needed for its own labeling, in whatever shape
`meteoswiss.py` happened to return. It had no provenance metadata (no
checksums, no retrieval timestamps, no explicit schema), no way to detect
if a source changed shape, and no path to extending it to new stations
without rewriting backtest-specific code. `logs/historical/` is a
separate, general-purpose, provenance-tracked archive designed so that (a)
a from-scratch archive rebuild is always possible from committed manifests
+ checksums, and (b) any future station can be added by describing it in
`stations.py`, not by writing new one-off fetch/parse code.

## Directory layout

```
logs/historical/
  manifests/
    stations.json     # committed - full snapshot of every registered station + its coverage
    assets.jsonl       # committed - append-only, deduped-by-checksum log of every raw asset ever fetched
  station_raw/
    meteoswiss/        # (gitignored - see "What's committed" below) raw provider responses
    private/           # reserved for any future non-MeteoSwiss / private-licence source
  station_hourly/      # (gitignored) normalized per-station hourly JSONL, canonical schema
  labels/              # reserved for a future kitesailing-based historical label set
                        # (not yet populated - see CLAUDE.md's "currently-open mismatch" note)
  datasets/            # (gitignored) exported training-ready datasets (historical_data.py export-training)
  forecast_vintages/   # committed - genuine forecast-model payloads, gzip-compressed, by issue date
    YYYY/MM/DD/<issue-time>_<provider>_<model>.json.gz
    index.jsonl        # append-only index of every archived vintage
  reports/             # committed - timestamped JSON output of every research script run
```

Every directory that doesn't have a data file in git has a `README.md`
explaining its role - read those for directory-specific detail this
document doesn't repeat.

## What's committed vs. regenerable

Two categories of data live under `logs/historical/`:

1. **Irreplaceable** - committed to git always:
   - `manifests/stations.json`, `manifests/assets.jsonl` (small - tens of
     KB; the manifests, not the data itself)
   - `forecast_vintages/**/*.json.gz` and its `index.jsonl` - once a
     forecast's lead time passes, that exact vintage can never be
     recovered from any live API again (Open-Meteo's live endpoint only
     serves a rolling ~3 months, and even `backtest.py`'s historical
     archive fetch doesn't reproduce a genuine multi-day-lead forecast -
     see `features.py`'s docstring). These are gzip-compressed and deduped
     by content checksum specifically so this permanent commitment stays
     small.
   - `reports/*.json` - each research run's timestamped, provenance-tagged
     output (see "Reports" below).

2. **Regenerable** - gitignored (see `.gitignore`):
   - `station_hourly/*.jsonl`, `datasets/*.jsonl`, `datasets/*.csv`
   - **Why**: the normalized hourly schema (`NORMALIZED_FIELDS` in
     `historical_data.py`) repeats station metadata (name, provider,
     coordinates, elevation) on every single hourly row - roughly 10x more
     verbose per record than the existing compact `raw_cache/*.json`
     format. A full sync + export of the three confirmed stations'
     multi-year history produced **~1.1GB** of derived JSONL/CSV during
     this project's own testing - discovered and corrected before it was
     ever committed. Since every one of these files can be regenerated
     from the small, committed `raw_cache/` + manifests in seconds via
     `python historical_data.py sync`, committing them permanently would
     be pure repository bloat with no corresponding benefit.
   - If a future station's *raw* provider response also turns out
     impractical to commit in full (e.g. a very large multi-decade CSV),
     the same principle applies: keep the compact normalized hourly file
     (regenerated on demand) plus the manifest's URL + checksum, so the
     original can always be re-fetched or re-derived, rather than
     committing raw bytes that balloon the repo.

## Canonical normalized hourly schema

Every station-hourly record, regardless of source station or provider, is
normalized to exactly this field list (`historical_data.NORMALIZED_FIELDS`):

```
timestamp_utc, timestamp_local, station_id, station_name, provider,
latitude, longitude, elevation_m,
air_temperature_c, dew_point_c, relative_humidity_pct,
pressure_station_hpa, pressure_sea_level_hpa,
wind_speed_ms, wind_gust_ms, wind_direction_deg,
precipitation_mm, sunshine_duration_min, global_radiation_wm2,
cloud_cover_pct, snow_depth_cm,
source_file, retrieved_at, quality_flags
```

Rules:
- Missing values are `null`, never a fabricated placeholder (e.g. never
  `0` for a temperature that wasn't reported).
- Both `timestamp_utc` (ISO, timezone-aware) and `timestamp_local`
  (Europe/Zurich) are stored explicitly - no naive, ambiguous timestamps.
  This is what lets `data_quality.find_timestamp_gaps()` detect real gaps
  without DST transitions producing spurious ones (gap detection works
  in UTC).
- Units are normalized (e.g. wind speed always m/s, converted from km/h at
  ingest via `_kmh_to_ms()` if the source reports km/h) - never mixed units
  across stations.
- `quality_flags` holds `data_quality.py`'s findings for that record (see
  "Data quality" below) - flagged, not discarded.

## Manifests

- **`manifests/stations.json`** - a full snapshot, regenerated on every
  sync (`rebuild_stations_manifest()`), of every station in `stations.py`'s
  registry: id, name, provider, coordinates, elevation, variables, roles,
  verification status, and current coverage (record count, date range) if
  any data has been synced.
- **`manifests/assets.jsonl`** - append-only, one entry per raw asset ever
  fetched, deduped by `(station_id, checksum)` so re-running sync never
  re-logs the same download twice (`append_asset_manifest_entry()`).
  Records provider, source URL, filename, checksum, retrieval timestamp,
  and record count - the audit trail for "where did this data actually
  come from."

## Sync CLI (`historical_data.py`)

```bash
python historical_data.py sync                       # sync every registered station
python historical_data.py sync --station sam lug sma  # sync specific stations only
python historical_data.py list-stations               # list the registry (id/name/verification/coverage)
python historical_data.py list-stations --verified-only
python historical_data.py coverage                     # per-station record counts and date ranges
python historical_data.py coverage --station sam
python historical_data.py validate                     # data-quality + sync-health checks (see below)
python historical_data.py export-training               # write a combined dataset for research scripts
```

`sync` is idempotent: it discovers what's already in `station_hourly/`,
attempts a real live fetch (`_attempt_live_fetch()` - best-effort, catches
every exception since most candidate stations aren't confirmed to exist
yet), falls back to ingesting anything new in `logs/raw_cache/` for the
three confirmed stations, normalizes, and merges via
`merge_normalized_records()` - which **never overwrites a record that has
more non-null fields with one that has fewer** (the "never replace valid
old observations with missing ones" requirement). Re-running `sync` with
no new data reports `"added": 0` rather than rewriting anything.

## Data quality (`data_quality.py`)

Wired into `historical_data.py validate`. Checks, per record: implausible
values (`PLAUSIBLE_RANGES`), negative wind speed, gust less than sustained
speed (flagged, not discarded - occasional real short-duration lulls can
legitimately produce this), future timestamps. Across a station's full
record set: duplicate timestamps, timestamp gaps (UTC-based, so DST
transitions don't produce false positives), and (`validate_sync_health()`)
whether an archive has gone stale (`STALE_ARCHIVE_DAYS = 30` with no new
data) or come back completely empty. **Every finding is preserved as a
flag on the record (`quality_flags`), never used as a reason to silently
drop data** - a human or a future analysis can decide whether a flagged
record should be excluded.

A real validation run against the three confirmed stations' actual archives
found genuine gaps: 2 in the Lugano pressure archive, 8 in the Zürich
archive - reported, not hidden, in `historical_data.py validate`'s output
and in `logs/historical/reports/`.

## Forecast vintages (`forecast_vintages.py`)

Every live forecast run (`forecast_and_log.py`, 07:00 and 10:00 CEST) now
archives the raw multi-model forecast payload *before* scoring it, via
`archive_forecast_payload_safe()` - a best-effort wrapper that never lets
an archiving failure break the actual forecast/alert. Deduped by content
checksum (`FORECAST_PAYLOAD_KEYS` excludes the station-nowcast fields,
which are re-fetched fresh each call and would otherwise defeat dedup for
what is really the same forecast-model response). Stored gzip-compressed
under `forecast_vintages/YYYY/MM/DD/<issue-time>_<provider>_<model>.json.gz`
with issue time, target times, and lead-time-in-hours all recorded
explicitly and separately - this is what lets a future analysis
reconstruct exactly what the forecast said at real, multi-day lead times,
which neither the live API's ~3-month retention nor `backtest.py`'s
historical-archive fetch can do (see `features.py`'s own docstring on why
its historical fetch path isn't a genuine forecast reproduction).

## Reports (`research_report.py` + each research script)

Every research script (`station_analysis.py`, `calibration_analysis.py`,
`regime_analysis.py`, `continuous_target_analysis.py`) writes a new,
never-overwritten, timestamped JSON report to `logs/historical/reports/`
via `research_report.save_report()`. Each report carries a provenance
envelope: git commit SHA (`git_commit_sha()`), checksums of the input data
files it read, its own configuration (seed, fold definitions, etc.), and
explicit `warnings`/`limitations` lists - so a report from six months from
now is self-describing without needing this document to interpret it.

## Repository-size considerations going forward

- Forecast vintages grow by roughly one small gzip file per forecast run
  (2/day) - trivial over any realistic timeframe, but if this ever becomes
  a concern, periodic compressed monthly bundles (concatenate + re-gzip a
  month's files, keep the index) would cut overhead further without losing
  data. Not implemented, since current volume doesn't warrant it.
- Station archives are intentionally NOT committed in their normalized
  form (see "What's committed vs. regenerable" above) - only the raw
  provider cache (already small, already committed as `logs/raw_cache/`)
  and the manifests are permanent. Adding a new large station's raw
  archive should follow the same principle: commit the compact form or a
  URL+checksum manifest entry, not a 10x-inflated per-hour normalized
  dump.
- Reports are small (tens to low hundreds of KB each even with a full
  correlation/rolling-origin dump) and accumulate slowly (one per manual
  research run) - no retention policy needed yet; revisit if the
  `station_research` workflow job starts running frequently.

## Disaster recovery / rebuilding from scratch

Everything in `station_hourly/` and `datasets/` can be regenerated from
committed data alone:

```bash
python historical_data.py sync              # rebuilds station_hourly/ from raw_cache/ + manifests
python historical_data.py export-training    # rebuilds datasets/
```

`forecast_vintages/` and `reports/` cannot be regenerated - they are the
permanent record of what a forecast said or what an analysis found at a
point in time. If `logs/historical/manifests/` were ever lost, it can be
fully rebuilt via `rebuild_stations_manifest()` (called automatically by
`sync`) since it's a pure snapshot derived from `stations.py` plus
whatever's on disk - it holds no information that doesn't already exist
elsewhere.

## Licensing and provenance

All three confirmed stations (`sam`, `lug`, `sma`) are MeteoSwiss
SwissMetNet stations under MeteoSwiss Open Data
(opendata.swiss terms of use - attribution required, commercial use
permitted). Every candidate station's presumed licence (where guessed) is
recorded in `stations.py`'s `licence` field and is explicitly marked
`"unknown"` where it could not be determined - a candidate's licence must
be confirmed before its data is used for anything beyond internal research,
not just its data availability.
