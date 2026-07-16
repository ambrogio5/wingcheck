"""
historical_data.py - the durable, append-only historical weather-station
archive: `logs/historical/`. This is deliberately separate from
historical_cache.py (which exists purely to make backtest.py's own
2024-2026 retrain fast) - this module is the general-purpose station
archive meant to outlive any one retrain, support station research
(station_analysis.py etc.), and grow to cover new stations over time.

Layout (see docs/DATA_ARCHITECTURE.md for the full description):

    logs/historical/
      manifests/
        stations.json    - full snapshot: every registered station (stations.py)
                            + its current coverage, regenerated each run
        assets.jsonl      - append-only, deduplicated-by-checksum log of every
                            sync's inputs (what was fetched/ingested, when, how much)
      station_raw/        - original source bytes (see its own README - currently
                            empty; the pre-existing fetch path never kept raw bytes)
      station_hourly/      - normalized canonical hourly records, one JSONL per
                            station, append-only + deduplicated by timestamp
      forecast_vintages/   - see forecast_vintages.py
      labels/, datasets/, reports/ - see their own READMEs / other research modules

CLI:
    python3 historical_data.py sync [--station ID [ID ...]]
    python3 historical_data.py list-stations [--role ROLE] [--provider PROVIDER] [--verified-only]
    python3 historical_data.py coverage [--station ID]
    python3 historical_data.py validate
    python3 historical_data.py export-training --station ID [ID ...] [--out PATH]

`sync` is idempotent and safe to re-run: it never overwrites a valid old
observation with a missing one (existing hourly records are only ever
added to or left alone, never deleted), and re-running with no new data
available produces byte-identical output. For the three CONFIRMED
stations (sam/lug/sma - see stations.py) it can ingest from the existing
`logs/raw_cache/*.json` produced by historical_cache.py when a live fetch
isn't possible, so this repository's own already-fetched data becomes the
first real entries in the new archive without needing network access.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from stations import STATIONS, Station

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_DIR = os.path.join(BASE_DIR, "logs", "historical")
MANIFEST_DIR = os.path.join(HIST_DIR, "manifests")
STATION_HOURLY_DIR = os.path.join(HIST_DIR, "station_hourly")
RAW_CACHE_DIR = os.path.join(BASE_DIR, "logs", "raw_cache")

STATIONS_MANIFEST_PATH = os.path.join(MANIFEST_DIR, "stations.json")
ASSETS_MANIFEST_PATH = os.path.join(MANIFEST_DIR, "assets.jsonl")

ZURICH_TZ = ZoneInfo("Europe/Zurich")

# The canonical normalized hourly schema (Phase 2.2). Every station_hourly/
# record has exactly these keys; unavailable fields are null, never guessed.
NORMALIZED_FIELDS = (
    "timestamp_utc", "timestamp_local", "station_id", "station_name", "provider",
    "latitude", "longitude", "elevation_m",
    "air_temperature_c", "dew_point_c", "relative_humidity_pct",
    "pressure_station_hpa", "pressure_sea_level_hpa",
    "wind_speed_ms", "wind_gust_ms", "wind_direction_deg",
    "precipitation_mm", "sunshine_duration_min", "global_radiation_wm2",
    "cloud_cover_pct", "snow_depth_cm",
    "source_file", "retrieved_at", "quality_flags",
)


def _blank_record(station_id: str, dt_utc: datetime, source_file: str, retrieved_at: str) -> dict:
    station = STATIONS[station_id]
    dt_local = dt_utc.astimezone(ZURICH_TZ)
    return {
        "timestamp_utc": dt_utc.astimezone(timezone.utc).isoformat(),
        "timestamp_local": dt_local.isoformat(),
        "station_id": station_id,
        "station_name": station.name,
        "provider": station.provider,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "elevation_m": station.elevation_m,
        "air_temperature_c": None,
        "dew_point_c": None,
        "relative_humidity_pct": None,
        "pressure_station_hpa": None,
        "pressure_sea_level_hpa": None,
        "wind_speed_ms": None,
        "wind_gust_ms": None,
        "wind_direction_deg": None,
        "precipitation_mm": None,
        "sunshine_duration_min": None,
        "global_radiation_wm2": None,
        "cloud_cover_pct": None,
        "snow_depth_cm": None,
        "source_file": source_file,
        "retrieved_at": retrieved_at,
        "quality_flags": [],
    }


def _kmh_to_ms(v):
    if v is None or v != v:  # None or NaN
        return None
    return round(v / 3.6, 3)


def normalize_wind_observations(station_id: str, obs: dict, source_file: str, retrieved_at: str) -> list:
    """obs: {datetime_utc: {"speed_kmh":..., "gust_kmh":...}} (meteoswiss.py's
    shape) -> list of canonical normalized hourly dicts."""
    records = []
    for dt_utc, reading in obs.items():
        rec = _blank_record(station_id, dt_utc, source_file, retrieved_at)
        speed_ms = _kmh_to_ms(reading.get("speed_kmh"))
        gust_ms = _kmh_to_ms(reading.get("gust_kmh"))
        rec["wind_speed_ms"] = speed_ms
        rec["wind_gust_ms"] = gust_ms
        if speed_ms is not None and gust_ms is not None and gust_ms < speed_ms:
            rec["quality_flags"] = ["gust_lt_speed"]
        records.append(rec)
    return records


def normalize_pressure_observations(station_id: str, obs: dict, source_file: str, retrieved_at: str) -> list:
    """obs: {datetime_utc: {"pressure_hpa": ...}} -> canonical normalized records."""
    records = []
    for dt_utc, reading in obs.items():
        rec = _blank_record(station_id, dt_utc, source_file, retrieved_at)
        rec["pressure_sea_level_hpa"] = reading.get("pressure_hpa")
        records.append(rec)
    return records


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def station_hourly_path(station_id: str) -> str:
    return os.path.join(STATION_HOURLY_DIR, f"{station_id}.jsonl")


def merge_normalized_records(existing: list, new: list) -> list:
    """Deduplicates by timestamp_utc, keyed so a NEW record with real data
    never overwrites an existing valid one with nulls (Phase 2.4's "never
    replace valid old observations with missing values"), but a new record
    DOES fill in a timestamp that was previously entirely missing. Returns
    a list sorted by timestamp_utc."""
    by_ts = {r["timestamp_utc"]: r for r in existing}
    for r in new:
        ts = r["timestamp_utc"]
        if ts not in by_ts:
            by_ts[ts] = r
            continue
        old = by_ts[ts]
        # Keep whichever record has strictly more non-null fields - never
        # let a fresher-but-sparser fetch quietly erase real data.
        old_filled = sum(1 for k in NORMALIZED_FIELDS if old.get(k) is not None)
        new_filled = sum(1 for k in NORMALIZED_FIELDS if r.get(k) is not None)
        if new_filled > old_filled:
            by_ts[ts] = r
    return [by_ts[k] for k in sorted(by_ts)]


def _checksum(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def append_asset_manifest_entry(entry: dict):
    """Append-only, deduplicated by (station_id, checksum) - re-running
    sync against unchanged source data must not grow this file forever."""
    existing = _read_jsonl(ASSETS_MANIFEST_PATH)
    key = (entry["station_id"], entry["checksum"])
    for e in existing:
        if (e.get("station_id"), e.get("checksum")) == key:
            return  # already recorded, nothing to do
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    with open(ASSETS_MANIFEST_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _coverage_for_station(station_id: str) -> dict:
    records = _read_jsonl(station_hourly_path(station_id))
    if not records:
        return {"n_records": 0, "data_start": None, "data_end": None}
    timestamps = sorted(r["timestamp_utc"] for r in records)
    return {"n_records": len(records), "data_start": timestamps[0], "data_end": timestamps[-1]}


def rebuild_stations_manifest():
    """Full snapshot (not append-only, by design - see historical_data.py's
    docstring): every registered station's metadata plus its CURRENT
    coverage. Safe to regenerate wholesale every run since none of this is
    hand-edited."""
    snapshot = {}
    for station_id, station in STATIONS.items():
        coverage = _coverage_for_station(station_id)
        snapshot[station_id] = {
            "name": station.name,
            "provider": station.provider,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "elevation_m": station.elevation_m,
            "variables": list(station.variables),
            "roles": list(station.roles),
            "verification": station.verification,
            "confidence": station.confidence,
            "licence": station.licence,
            "verification_note": station.verification_note,
            "suitable_for_live_retrieval": station.suitable_for_live_retrieval,
            "suitable_for_backtesting": station.suitable_for_backtesting,
            "coverage": coverage,
        }
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    with open(STATIONS_MANIFEST_PATH, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stations": snapshot,
        }, f, indent=2)
    return snapshot


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def _sync_from_raw_cache_wind(station_id: str) -> list:
    """For station_id == 'sam': ingest the already-fetched
    logs/raw_cache/samedan_archive.json (historical_cache.py's cache) as a
    real, deterministic normalization source when a live fetch isn't
    available. Returns [] if that cache file doesn't exist."""
    path = os.path.join(RAW_CACHE_DIR, "samedan_archive.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        cached = json.load(f)
    obs = {datetime.fromisoformat(k): v for k, v in cached.items()}
    retrieved_at = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    records = normalize_wind_observations(station_id, obs, source_file=path, retrieved_at=retrieved_at)
    append_asset_manifest_entry({
        "station_id": station_id, "provider": STATIONS[station_id].provider,
        "kind": "wind", "source_description": "historical_cache.py samedan_archive.json (ingested, not freshly fetched)",
        "source_path": path, "retrieved_at": retrieved_at,
        "checksum": _checksum(cached), "record_count": len(records),
        "data_start": min(obs) .isoformat() if obs else None,
        "data_end": max(obs).isoformat() if obs else None,
    })
    return records


def _sync_from_raw_cache_pressure(station_id: str, cache_filename: str) -> list:
    path = os.path.join(RAW_CACHE_DIR, cache_filename)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        cached = json.load(f)
    obs = {datetime.fromisoformat(k): v for k, v in cached.items()}
    retrieved_at = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    records = normalize_pressure_observations(station_id, obs, source_file=path, retrieved_at=retrieved_at)
    append_asset_manifest_entry({
        "station_id": station_id, "provider": STATIONS[station_id].provider,
        "kind": "pressure", "source_description": f"historical_cache.py {cache_filename} (ingested, not freshly fetched)",
        "source_path": path, "retrieved_at": retrieved_at,
        "checksum": _checksum(cached), "record_count": len(records),
        "data_start": min(obs).isoformat() if obs else None,
        "data_end": max(obs).isoformat() if obs else None,
    })
    return records


def _attempt_live_fetch(station_id: str) -> list:
    """Best-effort live fetch for ANY meteoswiss-provider station via the
    generic meteoswiss.py functions - this is the real code path that will
    actually discover new station data once run in an environment with
    real network access. Returns [] (not an exception) on any failure, so
    `sync` can move on to the next station; the caller logs the outcome."""
    station = STATIONS[station_id]
    if station.provider != "meteoswiss":
        return []
    import meteoswiss
    retrieved_at = datetime.now(timezone.utc).isoformat()
    records = []
    try:
        wind_obs = meteoswiss.fetch_wind_observations(station_id, include_historical=True)
        if wind_obs:
            records += normalize_wind_observations(
                station_id, wind_obs, source_file=f"live:{station_id}:wind", retrieved_at=retrieved_at)
    except Exception as e:
        print(f"  [live fetch] {station_id} wind: unavailable ({e})")
    try:
        pressure_obs = meteoswiss.fetch_pressure_observations(station_id, include_historical=True)
        if pressure_obs:
            records += normalize_pressure_observations(
                station_id, pressure_obs, source_file=f"live:{station_id}:pressure", retrieved_at=retrieved_at)
    except Exception as e:
        print(f"  [live fetch] {station_id} pressure: unavailable ({e})")
    return records


def sync(station_ids=None):
    """Idempotent incremental refresh (Phase 2.4): discover -> fetch/ingest
    -> normalize -> deduplicate -> update manifests. Never network-required
    to make progress: confirmed stations fall back to this repo's own
    already-fetched historical_cache.py output when a live fetch fails."""
    targets = station_ids or list(STATIONS)
    unknown = [s for s in targets if s not in STATIONS]
    if unknown:
        raise SystemExit(f"unknown station id(s): {unknown}")

    results = {}
    for station_id in targets:
        print(f"[sync] {station_id} ({STATIONS[station_id].name})...")
        new_records = _attempt_live_fetch(station_id)

        if not new_records and station_id == "sam":
            new_records = _sync_from_raw_cache_wind(station_id)
        elif not new_records and station_id == "lug":
            new_records = _sync_from_raw_cache_pressure(station_id, "pressure_lug.json")
        elif not new_records and station_id == "sma":
            new_records = _sync_from_raw_cache_pressure(station_id, "pressure_sma.json")

        path = station_hourly_path(station_id)
        existing = _read_jsonl(path)
        if not new_records:
            results[station_id] = {"status": "no_data_available", "added": 0, "total": len(existing)}
            print(f"  -> no new or ingestible data available (existing: {len(existing)} records)")
            continue

        merged = merge_normalized_records(existing, new_records)
        _write_jsonl(path, merged)
        added = len(merged) - len(existing)
        results[station_id] = {"status": "ok", "added": added, "total": len(merged)}
        print(f"  -> {len(merged)} total hourly records ({added:+d} vs before)")

    rebuild_stations_manifest()
    return results


# ---------------------------------------------------------------------------
# list-stations / coverage / validate
# ---------------------------------------------------------------------------

def list_stations(role=None, provider=None, verified_only=False):
    rows = []
    for sid, s in STATIONS.items():
        if role and role not in s.roles:
            continue
        if provider and s.provider != provider:
            continue
        if verified_only and s.verification != "confirmed":
            continue
        rows.append((sid, s))
    return rows


def coverage_report(station_id=None):
    ids = [station_id] if station_id else list(STATIONS)
    report = {}
    for sid in ids:
        if sid not in STATIONS:
            raise SystemExit(f"unknown station id: {sid}")
        report[sid] = {**_coverage_for_station(sid), "verification": STATIONS[sid].verification}
    return report


def validate_archive():
    """Cross-checks the station_hourly archive for internal consistency -
    a lighter-weight companion to data_quality.py's full validation, scoped
    to just the historical_data.py archive itself (schema/dedup/manifest
    consistency, not meteorological plausibility - see data_quality.py)."""
    problems = []
    for sid in STATIONS:
        path = station_hourly_path(sid)
        if not os.path.exists(path):
            continue
        records = _read_jsonl(path)
        seen_ts = set()
        for r in records:
            missing_fields = [f for f in NORMALIZED_FIELDS if f not in r]
            if missing_fields:
                problems.append(f"{sid}: record missing fields {missing_fields}")
            ts = r.get("timestamp_utc")
            if ts in seen_ts:
                problems.append(f"{sid}: duplicate timestamp_utc {ts}")
            seen_ts.add(ts)
            if r.get("station_id") != sid:
                problems.append(f"{sid}: record has mismatched station_id {r.get('station_id')!r}")
        sorted_ts = sorted(seen_ts)
        if sorted_ts != [r["timestamp_utc"] for r in records]:
            problems.append(f"{sid}: records are not stored in chronological order")
    return problems


def export_training(station_ids, out_path=None):
    """Exports the normalized hourly archive for the given (or all
    confirmed) stations into one flat JSONL file under
    logs/historical/datasets/ - the input station_analysis.py and friends
    read, kept separate from historical_data.py's own station_hourly/
    per-station files so downstream research tooling has one clear entry
    point rather than needing to know the archive's internal layout."""
    ids = station_ids or [sid for sid, s in STATIONS.items() if s.verification == "confirmed"]
    out_path = out_path or os.path.join(HIST_DIR, "datasets", "station_export.jsonl")
    all_records = []
    for sid in ids:
        all_records += _read_jsonl(station_hourly_path(sid))
    all_records.sort(key=lambda r: (r["station_id"], r["timestamp_utc"]))
    _write_jsonl(out_path, all_records)
    return out_path, len(all_records)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Historical weather-station archive")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Incrementally refresh the station archive")
    p_sync.add_argument("--station", nargs="+", default=None)

    p_list = sub.add_parser("list-stations", help="List registered stations")
    p_list.add_argument("--role", default=None)
    p_list.add_argument("--provider", default=None)
    p_list.add_argument("--verified-only", action="store_true")

    p_cov = sub.add_parser("coverage", help="Report archive coverage")
    p_cov.add_argument("--station", default=None)

    sub.add_parser("validate", help="Validate the station_hourly archive")

    p_export = sub.add_parser("export-training", help="Export normalized station data for research")
    p_export.add_argument("--station", nargs="+", default=None)
    p_export.add_argument("--out", default=None)

    args = parser.parse_args(argv)

    if args.command == "sync":
        results = sync(args.station)
        print(json.dumps(results, indent=2))
    elif args.command == "list-stations":
        rows = list_stations(args.role, args.provider, args.verified_only)
        for sid, s in rows:
            print(f"{sid:16s} {s.name:30s} provider={s.provider:15s} "
                  f"verification={s.verification:22s} confidence={s.confidence}")
        print(f"\n{len(rows)} station(s)")
    elif args.command == "coverage":
        report = coverage_report(args.station)
        print(json.dumps(report, indent=2))
    elif args.command == "validate":
        problems = validate_archive()
        if problems:
            print(f"{len(problems)} problem(s) found:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("Archive validation: no problems found.")
    elif args.command == "export-training":
        out_path, n = export_training(args.station, args.out)
        print(f"Exported {n} records to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
