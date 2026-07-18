"""
sia_import.py - real ingestion of the official MeteoSwiss Segl-Maria (SIA)
files the repo owner downloaded directly from data.geo.admin.ch, since this
development sandbox has no outbound network access to that host (confirmed:
the gateway proxy 403s CONNECT attempts to data.geo.admin.ch).

Two real files were supplied:
  - ogd-smn_sia_t_historical_2000-2009.csv - the 2000s decade bucket of
    SIA's 10-minute ("_t_") product. In practice this file contains ONLY
    tre200s0/ure200s0/tde200s0/pva200s0 (temperature/humidity/dew point/
    vapour pressure) at three fixed synoptic hours a day (06:00, 12:00,
    18:00 local-as-UTC*) from 2004-02-01 through 2009-12-31 - confirmed by
    inspecting the file directly (6,472 rows, non-empty-column counts).
    There is NO wind, pressure, precipitation or radiation in this file -
    SIA's automatic (10-minute, all-variable) sensors were evidently not
    yet installed/reporting in the 2000s. This directly contradicts an
    earlier, unverified claim (from the patch this module was built to
    support) of "108,116 hourly rows... including wind... from
    2014-03-18" - that claim could not be confirmed from any file the repo
    owner has actually supplied, and is NOT propagated by this module. See
    docs/DATA_ARCHITECTURE.md's "SIA ingestion" section for the full
    correction.
  - ogd-smn_sia_t_recent.csv - a genuine, fully-populated 10-minute record
    (temperature, humidity, dew point, wind speed/gust/direction,
    station-level pressure, precipitation, radiation, sunshine) but only
    for 2026-01-01 through 2026-07-16 (28,368 rows, true 10-minute
    cadence) - MeteoSwiss's OGD "recent" files are a rolling tail, not a
    multi-year archive. This means there is a real, currently unfilled
    coverage gap from 2010 through end of 2025 - no file covering that
    span has been supplied or fetched. Do not assume it exists.

*MeteoSwiss's `reference_timestamp` convention is treated as UTC by this
codebase's existing meteoswiss.py (`parse_generic_station_csv`) - this
module deliberately reuses that same convention rather than introducing a
second, inconsistent interpretation for the 10-minute product. See
meteoswiss.py's docstring for the established handling.

This module never fabricates data: it preserves both raw files unmodified
under logs/historical/raw/meteoswiss/sia/ (checksummed, small enough -
about 4 MB combined - to commit directly, unlike a hypothetical
multi-decade full archive), parses them with
meteoswiss.parse_generic_station_csv_10min (real, confirmed column
mapping - see meteoswiss.py), and writes:
  - logs/historical/station_10min/sia.jsonl - the real 10-minute records,
    gitignored/regenerable from the committed raw files.
  - logs/historical/station_hourly/sia.jsonl - honest top-of-hour hourly
    AGGREGATES computed from the real 10-minute readings that actually
    exist within each UTC hour (arithmetic mean for scalar fields, a
    circular/vector mean for wind direction, never an interpolated or
    invented value for an hour with no underlying 10-minute data). Every
    such record is tagged quality_flags=["derived_from_10min_mean"] and a
    source_asset prefixed "derived_from_10min:" so it is never confused
    with a genuine, separately-published MeteoSwiss hourly ("_h_") product
    - no "_h_" file has been supplied or fetched for SIA in this session.
"""

import csv
import hashlib
import math
import os
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import historical_data as hd
import meteoswiss

RAW_SIA_DIR = os.path.join(hd.RAW_METEOSWISS_DIR, "sia")

DERIVED_HOURLY_FLAG = "derived_from_10min_mean"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text(path: str) -> str:
    with open(path, encoding="latin-1") as f:
        return f.read()


def preserve_raw_file(source_path: str, dest_name: str) -> dict:
    """Copies source_path immutably into RAW_SIA_DIR under dest_name.
    Never overwrites a preserved file with different content - raises if
    dest_name already exists with a different checksum, since raw files
    must never be silently modified once preserved. Returns real, measured
    metadata (never guessed): checksum, byte size."""
    os.makedirs(RAW_SIA_DIR, exist_ok=True)
    checksum = _sha256_file(source_path)
    size = os.path.getsize(source_path)
    dest_path = os.path.join(RAW_SIA_DIR, dest_name)
    if os.path.exists(dest_path):
        existing_checksum = _sha256_file(dest_path)
        if existing_checksum != checksum:
            raise ValueError(
                f"refusing to overwrite immutable raw file {dest_path} - "
                f"existing checksum {existing_checksum} != new {checksum}"
            )
    else:
        shutil.copyfile(source_path, dest_path)
    return {"dest_path": dest_path, "checksum": checksum, "byte_size": size}


def inspect_csv(path: str) -> dict:
    """Real, measured statistics about a raw MeteoSwiss ogd-smn CSV - every
    field here comes from actually reading the file, never assumed from
    its filename or a claimed schema."""
    with open(path, encoding="latin-1", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        rows = list(reader)

    timestamps_raw = [r[1] for r in rows if len(r) > 1]
    times = []
    for ts in timestamps_raw:
        try:
            times.append(datetime.strptime(ts.strip(), "%d.%m.%Y %H:%M"))
        except ValueError:
            continue
    times.sort()

    n = len(rows)
    n_unique_ts = len(set(timestamps_raw))
    duplicate_count = n - n_unique_ts

    # Real cadence: the most common gap between consecutive real timestamps -
    # never assumed to be exactly 10 minutes just because the file is
    # named "_t_".
    gaps_minutes = []
    for a, b in zip(times, times[1:]):
        gaps_minutes.append((b - a).total_seconds() / 60.0)
    modal_gap = None
    if gaps_minutes:
        counts = defaultdict(int)
        for g in gaps_minutes:
            counts[g] += 1
        modal_gap = max(counts.items(), key=lambda kv: kv[1])[0]

    missing_count = 0
    if modal_gap and modal_gap > 0:
        for g in gaps_minutes:
            if g > modal_gap:
                missing_count += int(round(g / modal_gap)) - 1

    non_empty_columns = []
    for col_idx, col_name in enumerate(header):
        if col_idx < 2:
            continue  # station_abbr, reference_timestamp
        if any(len(r) > col_idx and r[col_idx].strip() for r in rows):
            non_empty_columns.append(col_name)

    return {
        "path": path,
        "filename": os.path.basename(path),
        "byte_size": os.path.getsize(path),
        "checksum": _sha256_file(path),
        "n_columns": len(header),
        "columns": header,
        "non_empty_columns": non_empty_columns,
        "row_count": n,
        "duplicate_timestamp_count": duplicate_count,
        "first_timestamp": times[0].isoformat() if times else None,
        "last_timestamp": times[-1].isoformat() if times else None,
        "modal_gap_minutes": modal_gap,
        "missing_timestamp_count_at_modal_cadence": missing_count,
    }


def merge_obs(*obs_dicts: dict) -> dict:
    """Merge several {datetime_utc: {field: value}} dicts. A sparser
    record never silently overwrites a richer one for the same
    timestamp - ties keep the first dict's value (deterministic, does not
    depend on dict iteration order beyond the caller's own ordering)."""
    merged = {}
    for obs in obs_dicts:
        for dt, vals in obs.items():
            if dt not in merged:
                merged[dt] = vals
                continue
            existing_non_null = sum(1 for v in merged[dt].values() if v is not None)
            new_non_null = sum(1 for v in vals.values() if v is not None)
            if new_non_null > existing_non_null:
                merged[dt] = vals
    return merged


def _circular_mean_degrees(values):
    if not values:
        return None
    sin_sum = sum(math.sin(math.radians(v)) for v in values)
    cos_sum = sum(math.cos(math.radians(v)) for v in values)
    if sin_sum == 0 and cos_sum == 0:
        return None
    # round before the modulo so a float-noise 359.9999999 folds to 0
    # rather than surviving as "360.0", which is not a valid compass value
    return round(math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0, 6) % 360.0


def derive_hourly_from_10min(obs_10min: dict) -> dict:
    """Aggregates real 10-minute observations into top-of-hour hourly
    records. Only produces an hour if at least one real 10-minute reading
    exists within it - never fabricates a value for a fully-missing hour.
    Wind direction is combined with a circular (vector) mean, since
    averaging raw compass degrees across the 0/360 wrap point silently
    produces wrong results (e.g. 350 and 10 -> naive mean 180, the exact
    opposite direction; circular mean correctly gives 0)."""
    by_hour = defaultdict(list)
    for dt, vals in obs_10min.items():
        hour = dt.replace(minute=0, second=0, microsecond=0)
        by_hour[hour].append(vals)

    hourly = {}
    for hour, samples in by_hour.items():
        record = {}
        fields = set()
        for s in samples:
            fields.update(s.keys())
        for field in fields:
            values = [s.get(field) for s in samples if s.get(field) is not None]
            if not values:
                record[field] = None
            elif field == "wind_direction_deg":
                record[field] = _circular_mean_degrees(values)
            elif field == "wind_gust_ms":
                record[field] = max(values)  # gust is a peak, not a mean, by definition
            else:
                record[field] = sum(values) / len(values)
        record["_n_10min_samples"] = len(samples)
        hourly[hour] = record
    return hourly


def import_sia_official_files(historical_path: str, recent_path: str,
                               meta_stations_path: str = None, meta_parameters_path: str = None,
                               station_id: str = "sia") -> dict:
    """Real end-to-end SIA import: preserve raw files, inspect them,
    parse via meteoswiss.parse_generic_station_csv_10min, merge, derive
    honest hourly aggregates, write both normalized outputs, and update
    the asset/coverage manifests with REAL (not assumed) figures."""
    import station_registry
    registry = station_registry.load_registry()
    station = registry.get(station_id)
    if station is None:
        raise ValueError(f"{station_id!r} is not registered in config/stations.json")

    file_reports = {}
    raw_meta = {}
    for label, path, dest_name in (
        ("historical", historical_path, "ogd-smn_sia_t_historical_2000-2009.csv"),
        ("recent", recent_path, "ogd-smn_sia_t_recent.csv"),
    ):
        file_reports[label] = inspect_csv(path)
        raw_meta[label] = preserve_raw_file(path, dest_name)

    if meta_stations_path:
        raw_meta["meta_stations"] = preserve_raw_file(meta_stations_path, "ogd-smn_meta_stations.csv")
    if meta_parameters_path:
        raw_meta["meta_parameters"] = preserve_raw_file(meta_parameters_path, "ogd-smn_meta_parameters.csv")

    historical_parsed = meteoswiss.parse_generic_station_csv_10min(_read_text(historical_path))
    recent_parsed = meteoswiss.parse_generic_station_csv_10min(_read_text(recent_path))

    merged_10min_obs = merge_obs(historical_parsed["observations"], recent_parsed["observations"])
    retrieved_at = datetime.now(timezone.utc).isoformat()

    combined_source_asset = (
        f"user_provided_raw_file:ogd-smn_sia_t_historical_2000-2009.csv+ogd-smn_sia_t_recent.csv"
    )
    ten_min_records = hd.normalize_generic_observations(station, merged_10min_obs, combined_source_asset, retrieved_at)

    existing_10min = hd._read_jsonl(hd.station_10min_path(station_id))
    merged_10min_records, added_10min = hd.merge_normalized_records(existing_10min, ten_min_records)
    hd._write_jsonl(hd.station_10min_path(station_id), merged_10min_records)

    hourly_obs = derive_hourly_from_10min(merged_10min_obs)
    hourly_source_asset = "derived_from_10min:" + combined_source_asset
    hourly_records = []
    for hour, vals in hourly_obs.items():
        n_samples = vals.pop("_n_10min_samples")
        rec = hd._blank_record(station, hour, hourly_source_asset, retrieved_at)
        for field, value in vals.items():
            if field in rec:
                rec[field] = value
        rec["quality_flags"].append(DERIVED_HOURLY_FLAG)
        rec["quality_flags"].append(f"n_10min_samples:{n_samples}")
        if rec.get("wind_speed_ms") is not None and rec.get("wind_gust_ms") is not None \
                and rec["wind_gust_ms"] < rec["wind_speed_ms"]:
            rec["quality_flags"].append("gust_less_than_speed")
        hourly_records.append(rec)

    existing_hourly = hd._read_jsonl(hd.station_hourly_path(station_id))
    merged_hourly_records, added_hourly = hd.merge_normalized_records(existing_hourly, hourly_records)
    hd._write_jsonl(hd.station_hourly_path(station_id), merged_hourly_records)

    for label in ("historical", "recent"):
        rel_path = os.path.relpath(raw_meta[label]["dest_path"], hd.BASE_DIR)
        hd.append_asset_manifest_entry({
            "station_id": station_id, "provider": station.provider,
            "source_asset": f"user_provided_raw_file:{rel_path}",
            "checksum": raw_meta[label]["checksum"],
            "retrieved_at": retrieved_at,
            "n_records": file_reports[label]["row_count"],
        })

    hd._rebuild_coverage_manifest(registry)

    return {
        "station_id": station_id,
        "file_reports": file_reports,
        "raw_files_preserved": {k: v["dest_path"] for k, v in raw_meta.items()},
        "n_10min_parsed": len(merged_10min_obs),
        "n_10min_added": added_10min,
        "n_10min_total": len(merged_10min_records),
        "n_hourly_derived": len(hourly_obs),
        "n_hourly_added": added_hourly,
        "n_hourly_total": len(merged_hourly_records),
        "coverage": hd._coverage_for_station(station_id),
        "warning": (
            "Hourly records are DERIVED (arithmetic/circular mean of real 10-minute "
            "readings), not a genuine separately-fetched MeteoSwiss hourly ('_h_') "
            "product - no such file was supplied or fetched. Coverage has a real gap "
            "from 2010 through end of 2025 (no file spans that period); the 2000s "
            "decade file contains temperature/humidity only, no wind."
        ),
    }


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Import real, user-supplied official MeteoSwiss SIA files")
    parser.add_argument("--historical", required=True)
    parser.add_argument("--recent", required=True)
    parser.add_argument("--meta-stations", default=None)
    parser.add_argument("--meta-parameters", default=None)
    parser.add_argument("--station", default="sia")
    args = parser.parse_args(argv)
    import json
    report = import_sia_official_files(
        args.historical, args.recent, args.meta_stations, args.meta_parameters, args.station,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
