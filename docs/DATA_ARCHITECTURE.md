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

All three confirmed stations (`sam`, `lug`, `sma`) are MeteoSwiss
SwissMetNet stations under MeteoSwiss Open Data (opendata.swiss terms of
use - attribution required, commercial use permitted). Every candidate
station's presumed licence is recorded in `config/stations.json`'s
`licence` field, explicitly `"unknown"` where it could not be determined -
a candidate's licence must be confirmed before its data is used for
anything beyond internal research.
