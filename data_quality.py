"""
data_quality.py - implausible-value, duplicate, and gap/staleness checks
over the normalized historical station archive (historical_data.py).

Flags, never silently discards: every finding is returned so a human or a
future analysis can decide what to do with a flagged record - this module
never deletes or mutates the archive itself.
"""

from datetime import datetime, timezone

PLAUSIBLE_RANGES = {
    "temperature_c": (-40.0, 45.0),
    "dew_point_c": (-50.0, 35.0),
    "relative_humidity_pct": (0.0, 100.0),
    "pressure_station_hpa": (800.0, 1080.0),
    "pressure_sea_level_hpa": (930.0, 1080.0),
    "wind_speed_ms": (0.0, 75.0),
    "wind_gust_ms": (0.0, 100.0),
    "wind_direction_deg": (0.0, 360.0),
    "precipitation_mm": (0.0, 300.0),
    "sunshine_duration_min": (0.0, 60.0),
    "global_radiation_wm2": (0.0, 1400.0),
}

STALE_ARCHIVE_DAYS = 30


def validate_record(record: dict) -> list:
    """Returns a list of flag strings for one normalized record (empty if
    clean). Does not mutate the record."""
    flags = []
    for field, (lo, hi) in PLAUSIBLE_RANGES.items():
        val = record.get(field)
        if val is not None and not (lo <= val <= hi):
            flags.append(f"implausible_{field}")
    speed = record.get("wind_speed_ms")
    if speed is not None and speed < 0:
        flags.append("negative_wind_speed")
    gust = record.get("wind_gust_ms")
    if speed is not None and gust is not None and gust < speed:
        flags.append("gust_less_than_speed")
    ts = record.get("timestamp_utc")
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            if dt > datetime.now(timezone.utc):
                flags.append("future_timestamp")
        except ValueError:
            flags.append("unparseable_timestamp")
    return flags


def find_duplicates(records: list) -> list:
    """Returns a list of timestamp_utc strings that appear more than once."""
    seen = {}
    for r in records:
        ts = r.get("timestamp_utc")
        seen[ts] = seen.get(ts, 0) + 1
    return [ts for ts, count in seen.items() if count > 1]


def find_timestamp_gaps(records: list, expected_interval_hours: int = 1) -> list:
    """UTC-based gap detection (DST-safe - never compares naive/local
    times). Returns a list of (prev_ts, next_ts) pairs where the gap
    exceeds expected_interval_hours."""
    timestamps = sorted(
        datetime.fromisoformat(r["timestamp_utc"]) for r in records if r.get("timestamp_utc")
    )
    gaps = []
    for prev, nxt in zip(timestamps, timestamps[1:]):
        delta_hours = (nxt - prev).total_seconds() / 3600.0
        if delta_hours > expected_interval_hours * 1.5:
            gaps.append((prev.isoformat(), nxt.isoformat()))
    return gaps


def validate_station_records(records: list) -> dict:
    """Full validation pass for one station's records. Returns flagged
    records (with the record's own index and its flags), duplicate
    timestamps, and gaps - the archive isn't modified."""
    flagged = []
    for i, r in enumerate(records):
        flags = validate_record(r)
        if flags:
            flagged.append({"index": i, "timestamp_utc": r.get("timestamp_utc"), "flags": flags})
    duplicates = find_duplicates(records)
    gaps = find_timestamp_gaps(records)
    return {
        "n_records": len(records),
        "n_flagged": len(flagged),
        "flagged": flagged,
        "n_duplicates": len(duplicates),
        "duplicates": duplicates,
        "n_gaps": len(gaps),
        "gaps": gaps,
    }


def validate_sync_health(coverage_snapshot: dict) -> dict:
    """Given historical_data._rebuild_coverage_manifest()'s output for one
    station, flags an archive that's gone stale (no new data in
    STALE_ARCHIVE_DAYS) or come back completely empty for an enabled
    station."""
    findings = {}
    for sid, info in coverage_snapshot.items():
        if not info.get("enabled"):
            continue
        if info.get("n_records", 0) == 0:
            findings[sid] = "empty_archive_for_enabled_station"
            continue
        data_end = info.get("data_end")
        if data_end:
            end_dt = datetime.fromisoformat(data_end)
            age_days = (datetime.now(timezone.utc) - end_dt).total_seconds() / 86400.0
            if age_days > STALE_ARCHIVE_DAYS:
                findings[sid] = f"stale_archive ({age_days:.1f} days since last record)"
    return findings
