"""
station_features.py - pre-forecast station feature generation.

Generates candidate (research-only) features from real station
observations available BEFORE a given issuance cutoff - 07:00 or 10:00
Europe/Zurich, matching forecast_and_log.py's two scheduled runs. These
features are NOT added to features.FEATURE_NAMES and never influence the
production model; they exist so maloja_diagnostics.py and
station_analysis.py have something concrete to build on.

Cutoff and reporting-delay discipline (the anti-leakage core of this
module): an observation timestamped T is only considered "available" at
T + reporting_delay_minutes (from station_registry.py's per-station
config). An observation is only included if its availability time is <=
the requested cutoff - this is checked explicitly in _available_before,
and tested directly (no afternoon leakage, no post-cutoff observation
sneaking in).

"Since sunrise" features use a fixed SUNRISE_REFERENCE_HOUR (06:00 local)
rather than a real per-day astronomical sunrise calculation - a
deliberate, documented simplification; Silvaplana's actual sunrise varies
by at most ~2 hours across the May-Oct season this project cares about,
and adding a full solar-position calculation for a research-only feature
layer would cut against this project's "stay lightweight" principle.
"""

import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import station_registry

ZURICH_TZ = ZoneInfo("Europe/Zurich")
VALID_CUTOFFS = ("07:00", "10:00")
SUNRISE_REFERENCE_HOUR = 6


def _cutoff_datetime(target_date: str, cutoff: str) -> datetime:
    if cutoff not in VALID_CUTOFFS:
        raise ValueError(f"cutoff must be one of {VALID_CUTOFFS}, got {cutoff!r}")
    hour, minute = (int(x) for x in cutoff.split(":"))
    d = datetime.strptime(target_date, "%Y-%m-%d").date()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=ZURICH_TZ)


def _local_dt(record: dict) -> datetime:
    return datetime.fromisoformat(record["timestamp_local"])


def _available_before(record: dict, cutoff_dt: datetime, reporting_delay_minutes) -> bool:
    """True if this record's data is actually knowable by cutoff_dt, given
    the station's reporting delay. No observation whose availability time
    exceeds the cutoff may ever pass this check."""
    delay = timedelta(minutes=reporting_delay_minutes or 0)
    availability_time = _local_dt(record) + delay
    return availability_time <= cutoff_dt


def _morning_window_records(records: list, target_date: str, cutoff: str, reporting_delay_minutes):
    cutoff_dt = _cutoff_datetime(target_date, cutoff)
    day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=ZURICH_TZ)
    usable = [
        r for r in records
        if day_start <= _local_dt(r) <= cutoff_dt and _available_before(r, cutoff_dt, reporting_delay_minutes)
    ]
    return sorted(usable, key=lambda r: r["timestamp_local"]), cutoff_dt


def _nearest_before(records_sorted, target_dt, tolerance_hours=1.5):
    best = None
    best_delta = None
    for r in records_sorted:
        dt = _local_dt(r)
        delta_hours = (target_dt - dt).total_seconds() / 3600.0
        if 0 <= delta_hours <= tolerance_hours and (best_delta is None or delta_hours < best_delta):
            best, best_delta = r, delta_hours
    return best


def _wind_vector(speed, direction_deg):
    if speed is None or direction_deg is None:
        return None, None
    rad = math.radians(direction_deg)
    # Meteorological convention: direction is where the wind blows FROM.
    u = -speed * math.sin(rad)
    v = -speed * math.cos(rad)
    return u, v


def generate_station_features(records: list, target_date: str, cutoff: str, reporting_delay_minutes=0) -> dict:
    """records: normalized hourly records for ONE station (historical_data.
    NORMALIZED_FIELDS shape), any time range - this function does its own
    cutoff/window filtering. Returns a flat dict of generic features (None
    where data doesn't exist) plus 'coverage' and 'missing_indicator'."""
    usable, cutoff_dt = _morning_window_records(records, target_date, cutoff, reporting_delay_minutes)

    out = {
        "latest_wind_speed": None, "mean_morning_wind": None, "max_morning_gust": None,
        "wind_u": None, "wind_v": None,
        "wind_speed_trend_1h": None, "wind_speed_trend_3h": None,
        "temperature_latest": None, "temperature_change_since_sunrise": None,
        "temperature_trend_1h": None, "temperature_trend_3h": None,
        "dew_point_depression": None, "relative_humidity": None,
        "pressure_latest": None, "pressure_trend_3h": None,
        "precipitation_since_midnight": None, "radiation_since_sunrise": None,
        "coverage": 0.0, "missing_indicator": 1.0,
    }
    if not usable:
        return out

    out["missing_indicator"] = 0.0
    day_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=ZURICH_TZ)
    expected_hours = max(1, int((cutoff_dt - day_start).total_seconds() / 3600.0) + 1)
    out["coverage"] = round(min(1.0, len(usable) / expected_hours), 3)

    latest = usable[-1]
    out["latest_wind_speed"] = latest.get("wind_speed_ms")
    out["temperature_latest"] = latest.get("temperature_c")
    out["relative_humidity"] = latest.get("relative_humidity_pct")
    out["pressure_latest"] = latest.get("pressure_sea_level_hpa") or latest.get("pressure_station_hpa")
    if latest.get("temperature_c") is not None and latest.get("dew_point_c") is not None:
        out["dew_point_depression"] = round(latest["temperature_c"] - latest["dew_point_c"], 2)

    out["wind_u"], out["wind_v"] = _wind_vector(latest.get("wind_speed_ms"), latest.get("wind_direction_deg"))

    speeds = [r["wind_speed_ms"] for r in usable if r.get("wind_speed_ms") is not None]
    gusts = [r["wind_gust_ms"] for r in usable if r.get("wind_gust_ms") is not None]
    if speeds:
        out["mean_morning_wind"] = round(sum(speeds) / len(speeds), 2)
    if gusts:
        out["max_morning_gust"] = round(max(gusts), 2)

    precip = [r["precipitation_mm"] for r in usable if r.get("precipitation_mm") is not None]
    if precip:
        out["precipitation_since_midnight"] = round(sum(precip), 2)
    radiation = [r for r in usable if _local_dt(r).hour >= SUNRISE_REFERENCE_HOUR and r.get("global_radiation_wm2") is not None]
    if radiation:
        out["radiation_since_sunrise"] = round(sum(r["global_radiation_wm2"] for r in radiation), 1)

    one_h_ago = _nearest_before(usable, _local_dt(latest) - timedelta(hours=1))
    three_h_ago = _nearest_before(usable, _local_dt(latest) - timedelta(hours=3))
    if one_h_ago and one_h_ago.get("wind_speed_ms") is not None and out["latest_wind_speed"] is not None:
        out["wind_speed_trend_1h"] = round(out["latest_wind_speed"] - one_h_ago["wind_speed_ms"], 2)
    if three_h_ago and three_h_ago.get("wind_speed_ms") is not None and out["latest_wind_speed"] is not None:
        out["wind_speed_trend_3h"] = round(out["latest_wind_speed"] - three_h_ago["wind_speed_ms"], 2)
    if one_h_ago and one_h_ago.get("temperature_c") is not None and out["temperature_latest"] is not None:
        out["temperature_trend_1h"] = round(out["temperature_latest"] - one_h_ago["temperature_c"], 2)
    if three_h_ago and three_h_ago.get("temperature_c") is not None and out["temperature_latest"] is not None:
        out["temperature_trend_3h"] = round(out["temperature_latest"] - three_h_ago["temperature_c"], 2)
    if three_h_ago and three_h_ago.get("pressure_sea_level_hpa") is not None and out["pressure_latest"] is not None:
        out["pressure_trend_3h"] = round(out["pressure_latest"] - three_h_ago["pressure_sea_level_hpa"], 2)

    sunrise_dt = day_start.replace(hour=SUNRISE_REFERENCE_HOUR)
    sunrise_rec = _nearest_before(usable, sunrise_dt, tolerance_hours=2.0)
    if sunrise_rec and sunrise_rec.get("temperature_c") is not None and out["temperature_latest"] is not None:
        out["temperature_change_since_sunrise"] = round(out["temperature_latest"] - sunrise_rec["temperature_c"], 2)

    return out


# --- Pairwise helpers (station A vs station B) ---

def temperature_difference(a: dict, b: dict):
    if a.get("temperature_latest") is None or b.get("temperature_latest") is None:
        return None
    return round(a["temperature_latest"] - b["temperature_latest"], 2)


def warming_rate_difference(a: dict, b: dict):
    if a.get("temperature_change_since_sunrise") is None or b.get("temperature_change_since_sunrise") is None:
        return None
    return round(a["temperature_change_since_sunrise"] - b["temperature_change_since_sunrise"], 2)


def pressure_difference(a: dict, b: dict):
    if a.get("pressure_latest") is None or b.get("pressure_latest") is None:
        return None
    return round(a["pressure_latest"] - b["pressure_latest"], 2)


def pressure_tendency_difference(a: dict, b: dict):
    if a.get("pressure_trend_3h") is None or b.get("pressure_trend_3h") is None:
        return None
    return round(a["pressure_trend_3h"] - b["pressure_trend_3h"], 2)


def wind_vector_difference(a: dict, b: dict):
    if a.get("wind_u") is None or b.get("wind_u") is None or a.get("wind_v") is None or b.get("wind_v") is None:
        return None, None
    return round(a["wind_u"] - b["wind_u"], 3), round(a["wind_v"] - b["wind_v"], 3)


def wind_vector_shear(a: dict, b: dict):
    du, dv = wind_vector_difference(a, b)
    if du is None:
        return None
    return round(math.hypot(du, dv), 3)


def generate_all_station_features(records_by_station: dict, target_date: str, cutoff: str, registry=None) -> dict:
    """records_by_station: {station_id: [normalized records]}. Returns
    {station_id: feature_dict} for every enabled station present."""
    registry = registry if registry is not None else station_registry.load_registry()
    out = {}
    for sid, records in records_by_station.items():
        station = registry.get(sid)
        delay = station.reporting_delay_minutes if station else 0
        out[sid] = generate_station_features(records, target_date, cutoff, delay)
    return out
