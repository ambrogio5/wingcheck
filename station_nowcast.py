"""
station_nowcast.py - lightweight, OPERATIONAL live station nowcast.

Fetches only the recent + provisional current-day tails (NEVER a historical discovery/backfill) for
every enabled live station (sam, lug, sma, cov, and any future addition),
normalizes it exactly the way historical_data.py does (reusing its
normalize_wind_observations/normalize_pressure_observations/
normalize_generic_observations functions directly, so there is only one
normalization implementation to keep correct), and writes a small,
time-bounded JSON snapshot: logs/current_station_observations.json.

This is what the operational forecast job (.github/workflows/wingcheck.yml)
runs instead of `historical_data.py sync` - a normal forecast run must
never re-download or re-normalize years of station history just to see
"what happened this morning." `historical_data.py sync` (full discovery +
merge into the durable archive) stays reserved for the daily/manual
`sync_historical_data` and `station_research` jobs.

forecast_and_log.py reads this file directly for its live diagnostics; it
only falls back to the historical station_hourly archive when this file
doesn't exist yet (a local research/dev run that hasn't run this script
first), never as part of the normal GitHub Actions operational path.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import historical_data as hd
import station_registry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "logs", "current_station_observations.json")

# Bounded lookback - enough for a 3h trend and a "since local midnight"
# morning window even on an early-morning run, never "decades of history."
LOOKBACK_HOURS = 30
DISPLAY_10MIN_STATIONS = {"sia", "sam"}


def _fetch_normalized_recent(station):
    """Best-effort: returns (normalized_records, quality_flags). Never
    raises - a fetch failure for one station must not prevent the others
    from being written; the caller still gets an entry with an explicit
    quality flag rather than nothing at all."""
    if station.station_id in hd.NO_LIVE_SOURCE_STATIONS:
        return [], ["no_live_source:manual_import_only"]
    kind = hd._parser_kind_for(station.station_id)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    flags = []
    try:
        import meteoswiss
        if kind == "wind":
            obs = meteoswiss.fetch_sam_hourly_observations(include_historical=False)
            records = hd.normalize_wind_observations(station, obs, "meteoswiss:sam:recent+now", retrieved_at)
        elif kind == "pressure":
            obs = meteoswiss.fetch_pressure_observations(station.station_id, include_historical=False)
            source = f"meteoswiss:{station.station_id}:recent+now"
            records = hd.normalize_pressure_observations(station, obs, source, retrieved_at)
        else:
            result = meteoswiss.fetch_station_observations(station.station_id, include_historical=False)
            source = f"meteoswiss:{station.station_id}:recent+now"
            records = hd.normalize_generic_observations(station, result["observations"], source, retrieved_at)
    except Exception as e:
        flags.append(f"fetch_failed:{e}")
        return [], flags
    if not records:
        flags.append("no_data_returned")
    return records, flags


def _bound_to_lookback(records, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    bounded = [r for r in records if datetime.fromisoformat(r["timestamp_utc"]) >= cutoff]
    return sorted(bounded, key=lambda r: r["timestamp_utc"])


def _fetch_latest_display_observation(station):
    """Newest provisional 10-minute observation for dashboard display."""
    if station.station_id not in DISPLAY_10MIN_STATIONS:
        return None, None
    retrieved_at = datetime.now(timezone.utc).isoformat()
    try:
        import meteoswiss
        result = meteoswiss.fetch_station_observations_10min(station.station_id)
        records = hd.normalize_generic_observations(
            station, result["observations"],
            f"meteoswiss:{station.station_id}:10min:recent+now", retrieved_at)
    except Exception:
        return None, None
    if not records:
        return None, None
    return max(records, key=lambda row: row["timestamp_utc"]), {
        "quality_status": result["quality_status"],
        "resolution_minutes": result["resolution_minutes"],
        "source_assets": result["source_assets"],
    }


def build_snapshot(station_ids=None, registry=None, now=None) -> dict:
    """Pure-ish (network side effects only through _fetch_normalized_recent)
    so tests can monkeypatch that one function and exercise everything
    else - bounding, age calculation, quality-flag propagation - without
    a network call."""
    now = now or datetime.now(timezone.utc)
    registry = registry if registry is not None else station_registry.load_registry()
    ids = station_ids or station_registry.enabled_station_ids(registry)

    stations_out = {}
    for sid in ids:
        station = registry.get(sid)
        if station is None or not station.enabled:
            continue
        records, flags = _fetch_normalized_recent(station)
        bounded = _bound_to_lookback(records, now)
        latest_display, display_metadata = _fetch_latest_display_observation(station)
        latest_available_at = bounded[-1]["timestamp_utc"] if bounded else None
        age_minutes = None
        if latest_available_at:
            age_minutes = round((now - datetime.fromisoformat(latest_available_at)).total_seconds() / 60.0, 1)
        stations_out[sid] = {
            "metadata": {
                "provider": station.provider,
                "roles": list(station.roles),
                "reporting_delay_minutes": station.reporting_delay_minutes,
            },
            "observations": bounded,
            "latest_available_at": latest_available_at,
            "age_minutes": age_minutes,
            "quality_flags": flags,
            "latest_display_observation": latest_display,
            "display_observation_metadata": display_metadata,
        }

    return {"generated_at": now.isoformat(), "stations": stations_out}


def main():
    snapshot = build_snapshot()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)
    n_stations = len(snapshot["stations"])
    n_obs = sum(len(s["observations"]) for s in snapshot["stations"].values())
    print(f"station_nowcast: wrote {n_obs} bounded observation(s) across {n_stations} station(s) to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
