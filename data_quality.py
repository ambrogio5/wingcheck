"""
data_quality.py - Phase 14: validates the normalized station_hourly
archive for physically-implausible or suspicious values.

Never discards a questionable observation - every check here FLAGS
(returns a flag string to be merged into the record's own
`quality_flags`, or reported in the aggregate report), consistent with
historical_data.py's own philosophy (see merge_normalized_records's
"never overwrite a valid old observation with a missing value"). Only a
record that fails to parse at all is ever dropped, and that happens
upstream in meteoswiss.py's CSV parsing, not here.
"""

from datetime import datetime, timezone

# (low, high) plausible ranges for each normalized field - deliberately
# generous (real alpine extremes, not "typical" values) so a flag really
# does mean "worth a human look", not "slightly unusual weather".
PLAUSIBLE_RANGES = {
    "air_temperature_c": (-40.0, 45.0),
    "dew_point_c": (-50.0, 35.0),
    "relative_humidity_pct": (0.0, 100.0),
    "pressure_station_hpa": (600.0, 1100.0),
    "pressure_sea_level_hpa": (900.0, 1085.0),
    "wind_speed_ms": (0.0, 75.0),
    "wind_gust_ms": (0.0, 110.0),
    "wind_direction_deg": (0.0, 360.0),
    "precipitation_mm": (0.0, 300.0),
    "snow_depth_cm": (0.0, 1000.0),
}

STALE_ARCHIVE_DAYS = 30  # a confirmed station's historical archive with no
# data newer than this is flagged "stale_archive" - this checks
# historical_data.py's batch archive (synced on demand), NOT a genuinely
# live per-minute feed like kitesailing_weather.py's scrape (that has its
# own staleness notion - a scrape job that stopped running - out of scope
# for this function, which only looks at logs/historical/'s station archive).


def validate_record(record: dict, now: datetime = None) -> list:
    """Returns a list of quality-flag strings for one normalized hourly
    record (historical_data.py's NORMALIZED_FIELDS schema) - a pure
    function, does not mutate `record`."""
    flags = []
    for field, (lo, hi) in PLAUSIBLE_RANGES.items():
        value = record.get(field)
        if value is not None and not (lo <= value <= hi):
            flags.append(f"implausible_{field}")

    speed = record.get("wind_speed_ms")
    gust = record.get("wind_gust_ms")
    if speed is not None and speed < 0:
        flags.append("negative_wind_speed")
    if speed is not None and gust is not None and gust < speed:
        flags.append("gust_lt_speed")  # already applied at normalization time too - kept here for direct-record checks

    ts = record.get("timestamp_utc")
    if ts and now is not None:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > now:
                flags.append("future_timestamp")
        except ValueError:
            flags.append("unparseable_timestamp")

    return flags


def find_timestamp_gaps(sorted_iso_timestamps: list, expected_interval_hours: float = 1.0,
                         tolerance_factor: float = 1.5, max_report: int = 200) -> list:
    """Flags any consecutive-timestamp gap bigger than
    expected_interval_hours * tolerance_factor. Timestamps are UTC ISO
    strings (historical_data.py's canonical form), so a DST transition in
    LOCAL time never shows up as a spurious gap or overlap here - the
    reason every stored timestamp is UTC first, local second."""
    gaps = []
    for i in range(1, len(sorted_iso_timestamps)):
        prev = datetime.fromisoformat(sorted_iso_timestamps[i - 1])
        curr = datetime.fromisoformat(sorted_iso_timestamps[i])
        diff_hours = (curr - prev).total_seconds() / 3600.0
        if diff_hours > expected_interval_hours * tolerance_factor:
            gaps.append({
                "from": sorted_iso_timestamps[i - 1], "to": sorted_iso_timestamps[i],
                "gap_hours": round(diff_hours, 2),
            })
            if len(gaps) >= max_report:
                break
    return gaps


def validate_station_records(records: list, now: datetime = None) -> dict:
    """Cross-record validation for one station's full normalized hourly
    list: duplicate timestamps, implausible-value flags per record, and
    timestamp gaps. Returns a report dict; never raises, never mutates
    `records`."""
    now = now or datetime.now(timezone.utc)
    seen = set()
    duplicates = []
    flagged = {}
    timestamps = []

    for r in records:
        ts = r.get("timestamp_utc")
        if ts in seen:
            duplicates.append(ts)
        seen.add(ts)
        timestamps.append(ts)

        flags = validate_record(r, now=now)
        if flags:
            flagged[ts] = flags

    gaps = find_timestamp_gaps(sorted(t for t in timestamps if t))

    return {
        "n_records": len(records),
        "n_duplicate_timestamps": len(duplicates),
        "duplicate_timestamps_sample": duplicates[:20],
        "n_flagged_records": len(flagged),
        "flagged_records_sample": dict(list(flagged.items())[:20]),
        "n_gaps": len(gaps),
        "gaps_sample": gaps[:20],
    }


def validate_sync_health(stations_manifest: dict, now: datetime = None) -> dict:
    """Station-level health check from historical_data.py's
    manifests/stations.json snapshot: flags a CONFIRMED station reporting
    zero coverage (an unexpectedly empty download / sync regression) or
    whose latest record is older than STALE_ARCHIVE_DAYS."""
    now = now or datetime.now(timezone.utc)
    report = {}
    for station_id, info in stations_manifest.get("stations", {}).items():
        if info.get("verification") != "confirmed":
            continue
        coverage = info.get("coverage", {})
        n = coverage.get("n_records", 0)
        data_end = coverage.get("data_end")
        issues = []
        if n == 0:
            issues.append("empty_download")
        elif data_end:
            try:
                end_dt = datetime.fromisoformat(data_end)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                age_days = (now - end_dt).total_seconds() / 86400.0
                if age_days > STALE_ARCHIVE_DAYS:
                    issues.append("stale_archive")
            except ValueError:
                issues.append("unparseable_data_end")
        if issues:
            report[station_id] = issues
    return report
