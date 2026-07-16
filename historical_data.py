"""
historical_data.py - durable historical station-observation archive.

Builds and maintains logs/historical/ from the stations registered (and
enabled) in config/stations.json (see station_registry.py). Distinct from
logs/raw_cache/ (backtest.py's internal, provider-shaped cache): this
module normalizes every station's hourly data to ONE canonical schema
(NORMALIZED_FIELDS) with explicit units, explicit UTC+local timestamps,
and full provenance (checksum, source asset, retrieval time) - a
general-purpose archive any future research script can read regardless of
which provider or station it came from.

Commands:
    python3 historical_data.py sync       # incrementally refresh the archive (idempotent)
    python3 historical_data.py validate   # data-quality + continuity checks
    python3 historical_data.py coverage   # per-station record counts / date ranges

Only stations with enabled=true in config/stations.json are ever actually
fetched - see station_registry.py's docstring for why every other entry
is a documented-but-unfetched candidate.

`sync` never overwrites a richer existing record with a sparser one
(merge_normalized_records) and never re-fetches network data it can avoid:
it first tries the already-fetched, already-committed logs/raw_cache/
files (the same ones backtest.py uses) before attempting any real network
call, and every live-fetch attempt is best-effort (catches all exceptions)
since this repo may run in network-restricted environments.

This module NEVER writes weights.json or docs/dashboard_data.json - it is
research/archive infrastructure only.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import station_registry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_DIR = os.path.join(BASE_DIR, "logs", "historical")
MANIFEST_DIR = os.path.join(HIST_DIR, "manifests")
STATION_RAW_DIR = os.path.join(HIST_DIR, "station_raw")
STATION_HOURLY_DIR = os.path.join(HIST_DIR, "station_hourly")
RAW_CACHE_DIR = os.path.join(BASE_DIR, "logs", "raw_cache")

COVERAGE_MANIFEST_PATH = os.path.join(MANIFEST_DIR, "stations.json")
ASSETS_MANIFEST_PATH = os.path.join(MANIFEST_DIR, "assets.jsonl")

ZURICH_TZ = ZoneInfo("Europe/Zurich")

NORMALIZED_FIELDS = (
    "timestamp_utc", "timestamp_local", "station_id", "provider",
    "latitude", "longitude", "elevation_m",
    "temperature_c", "dew_point_c", "relative_humidity_pct",
    "pressure_station_hpa", "pressure_sea_level_hpa",
    "wind_speed_ms", "wind_gust_ms", "wind_direction_deg",
    "precipitation_mm", "sunshine_duration_min", "global_radiation_wm2",
    "source_asset", "retrieved_at", "quality_flags",
)

_KMH_TO_MS = 1000.0 / 3600.0


def _blank_record(station, dt_utc, source_asset, retrieved_at):
    return {
        "timestamp_utc": dt_utc.astimezone(timezone.utc).isoformat(),
        "timestamp_local": dt_utc.astimezone(ZURICH_TZ).isoformat(),
        "station_id": station.station_id,
        "provider": station.provider,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "elevation_m": station.elevation_m,
        "temperature_c": None,
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
        "source_asset": source_asset,
        "retrieved_at": retrieved_at,
        "quality_flags": [],
    }


def normalize_wind_observations(station, obs: dict, source_asset: str, retrieved_at: str) -> list:
    """obs: {datetime_utc: {"speed_kmh":..., "gust_kmh":...}} -> list of
    NORMALIZED_FIELDS records, wind converted from km/h to m/s."""
    records = []
    for dt_utc, vals in obs.items():
        rec = _blank_record(station, dt_utc, source_asset, retrieved_at)
        speed_kmh = vals.get("speed_kmh")
        gust_kmh = vals.get("gust_kmh")
        if speed_kmh is not None:
            rec["wind_speed_ms"] = round(speed_kmh * _KMH_TO_MS, 3)
        if gust_kmh is not None:
            rec["wind_gust_ms"] = round(gust_kmh * _KMH_TO_MS, 3)
        if rec["wind_speed_ms"] is not None and rec["wind_gust_ms"] is not None \
                and rec["wind_gust_ms"] < rec["wind_speed_ms"]:
            rec["quality_flags"].append("gust_less_than_speed")
        records.append(rec)
    return records


def normalize_pressure_observations(station, obs: dict, source_asset: str, retrieved_at: str) -> list:
    """obs: {datetime_utc: {"pressure_hpa": ...}} -> NORMALIZED_FIELDS
    records (sea-level/QFF pressure only - MeteoSwiss's open feed doesn't
    expose station-level pressure separately for these stations)."""
    records = []
    for dt_utc, vals in obs.items():
        rec = _blank_record(station, dt_utc, source_asset, retrieved_at)
        rec["pressure_sea_level_hpa"] = vals.get("pressure_hpa")
        records.append(rec)
    return records


def _checksum(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def station_hourly_path(station_id: str) -> str:
    return os.path.join(STATION_HOURLY_DIR, f"{station_id}.jsonl")


def _non_null_count(rec):
    return sum(1 for k, v in rec.items() if k not in ("source_asset", "retrieved_at", "quality_flags") and v is not None)


def merge_normalized_records(existing: list, new: list) -> list:
    """Deduplicated by (station_id, timestamp_utc); never lets a sparser
    new record overwrite a richer existing one. Returns records sorted
    chronologically."""
    by_key = {(r["station_id"], r["timestamp_utc"]): r for r in existing}
    added = 0
    for rec in new:
        key = (rec["station_id"], rec["timestamp_utc"])
        if key not in by_key:
            by_key[key] = rec
            added += 1
        elif _non_null_count(rec) > _non_null_count(by_key[key]):
            by_key[key] = rec
    merged = sorted(by_key.values(), key=lambda r: r["timestamp_utc"])
    return merged, added


def append_asset_manifest_entry(entry: dict):
    """Append-only, deduped by (station_id, checksum) - see docstring."""
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    existing = _read_jsonl(ASSETS_MANIFEST_PATH)
    for e in existing:
        if e.get("station_id") == entry["station_id"] and e.get("checksum") == entry["checksum"]:
            return  # already recorded, nothing to do
    with open(ASSETS_MANIFEST_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _ingest_from_raw_cache(station_id: str):
    """Reads the already-committed logs/raw_cache/*.json files directly
    (no network) - the same data backtest.py already trusts. Returns
    (obs_dict, source_asset_description) or (None, None) if no cache file
    exists for this station."""
    if station_id == "sam":
        path = os.path.join(RAW_CACHE_DIR, "samedan_archive.json")
    elif station_id in ("lug", "sma"):
        path = os.path.join(RAW_CACHE_DIR, f"pressure_{station_id}.json")
    else:
        return None, None
    if not os.path.exists(path):
        return None, None
    with open(path) as f:
        cached = json.load(f)
    obs = {datetime.fromisoformat(k): v for k, v in cached.items()}
    return obs, f"logs/raw_cache/{os.path.basename(path)}"


def _attempt_live_fetch(station_id: str):
    """Best-effort real network fetch for the station's *recent* tail only
    (fast path - full historical re-discovery is already covered by the
    raw_cache ingestion above). Returns (obs_dict, source_asset) or
    ({}, None) on ANY failure - never raises, since this repo must keep
    working in network-restricted environments."""
    try:
        import meteoswiss
        if station_id == "sam":
            obs = meteoswiss.fetch_sam_hourly_observations(include_historical=False)
            return obs, "meteoswiss:sam:recent"
        if station_id in ("lug", "sma"):
            obs = meteoswiss.fetch_pressure_observations(station_id, include_historical=False)
            return obs, f"meteoswiss:{station_id}:recent"
    except Exception as e:
        print(f"  [live-fetch] {station_id}: best-effort fetch failed ({e}); continuing without it")
    return {}, None


def _normalize_for_station(station, obs, source_asset, retrieved_at):
    if "wind_speed_ms" in station.available_variables or station.station_id == "sam":
        return normalize_wind_observations(station, obs, source_asset, retrieved_at)
    return normalize_pressure_observations(station, obs, source_asset, retrieved_at)


def sync(station_ids=None):
    """Refreshes the archive for the given (or all enabled) stations.
    Idempotent: re-running with nothing new reports added=0. Returns a
    per-station result dict."""
    os.makedirs(STATION_HOURLY_DIR, exist_ok=True)
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    registry = station_registry.load_registry()
    ids = station_ids or station_registry.enabled_station_ids(registry)

    results = {}
    retrieved_at = datetime.now(timezone.utc).isoformat()
    for station_id in ids:
        station = registry.get(station_id)
        if station is None:
            results[station_id] = {"status": "unknown_station", "added": 0, "total": 0}
            continue
        if not station.enabled:
            results[station_id] = {"status": "not_enabled", "added": 0, "total": 0}
            continue

        existing = _read_jsonl(station_hourly_path(station_id))
        new_records = []

        cache_obs, cache_source = _ingest_from_raw_cache(station_id)
        if cache_obs:
            recs = _normalize_for_station(station, cache_obs, cache_source, retrieved_at)
            new_records.extend(recs)
            append_asset_manifest_entry({
                "station_id": station_id, "provider": station.provider,
                "source_asset": cache_source, "checksum": _checksum(sorted(cache_obs.keys(), key=str)),
                "retrieved_at": retrieved_at, "n_records": len(cache_obs),
            })

        live_obs, live_source = _attempt_live_fetch(station_id)
        if live_obs:
            recs = _normalize_for_station(station, live_obs, live_source, retrieved_at)
            new_records.extend(recs)
            append_asset_manifest_entry({
                "station_id": station_id, "provider": station.provider,
                "source_asset": live_source, "checksum": _checksum(sorted(str(k) for k in live_obs.keys())),
                "retrieved_at": retrieved_at, "n_records": len(live_obs),
            })

        if not new_records and not existing:
            results[station_id] = {"status": "no_data_available", "added": 0, "total": 0}
            continue

        merged, added = merge_normalized_records(existing, new_records)
        _write_jsonl(station_hourly_path(station_id), merged)
        results[station_id] = {"status": "ok", "added": added, "total": len(merged)}

    _rebuild_coverage_manifest(registry)
    return results


def _coverage_for_station(station_id: str) -> dict:
    records = _read_jsonl(station_hourly_path(station_id))
    if not records:
        return {"n_records": 0, "data_start": None, "data_end": None}
    timestamps = sorted(r["timestamp_utc"] for r in records)
    return {"n_records": len(records), "data_start": timestamps[0], "data_end": timestamps[-1]}


def _rebuild_coverage_manifest(registry=None):
    registry = registry or station_registry.load_registry()
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    snapshot = {}
    for sid, s in registry.items():
        cov = _coverage_for_station(sid)
        snapshot[sid] = {
            "name": s.name, "provider": s.provider, "roles": list(s.roles),
            "enabled": s.enabled, "verification": s.verification,
            **cov,
        }
    with open(COVERAGE_MANIFEST_PATH, "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "stations": snapshot}, f, indent=2)
    return snapshot


def coverage_report(station_id=None) -> dict:
    registry = station_registry.load_registry()
    snapshot = _rebuild_coverage_manifest(registry)
    if station_id:
        return {station_id: snapshot.get(station_id, {"error": "unknown station"})}
    return snapshot


def validate_archive() -> dict:
    """Data-quality + continuity checks over every station currently on
    disk. Delegates the actual checks to data_quality.py so the rules live
    in one, separately-tested place."""
    import data_quality
    registry = station_registry.load_registry()
    report = {}
    for sid in registry:
        records = _read_jsonl(station_hourly_path(sid))
        if not records:
            continue
        report[sid] = data_quality.validate_station_records(records)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Historical station-observation archive")
    sub = parser.add_subparsers(dest="command", required=True)
    p_sync = sub.add_parser("sync", help="Incrementally refresh the station archive")
    p_sync.add_argument("--station", nargs="+", default=None)
    sub.add_parser("validate", help="Data-quality + continuity checks")
    p_cov = sub.add_parser("coverage", help="Report archive coverage")
    p_cov.add_argument("--station", default=None)

    args = parser.parse_args(argv)

    if args.command == "sync":
        results = sync(args.station)
        print(json.dumps(results, indent=2))
    elif args.command == "validate":
        report = validate_archive()
        if not report:
            print("No station data on disk yet - run `historical_data.py sync` first.")
        for sid, findings in report.items():
            print(f"{sid}: {findings['n_flagged']} flagged record(s), "
                  f"{findings['n_duplicates']} duplicate(s), {findings['n_gaps']} gap(s)")
    elif args.command == "coverage":
        cov = coverage_report(args.station)
        print(json.dumps(cov, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
