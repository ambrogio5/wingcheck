"""
historical_cache.py - persists the raw historical data backtest.py needs
(Open-Meteo weather archive + the full MeteoSwiss Samedan/Lugano/Zurich
archives) under logs/raw_cache/, so retraining doesn't re-pull the same
multi-year data over the network every time. That fetch alone took ~14
minutes in the 2026-07-16 backtest run (three seasons of Open-Meteo
requests, plus discovering and downloading every historical Samedan CSV
file from the STAC catalog) - almost all of which is unchanging once a
season has closed.

Closed seasons (any date range whose end_date isn't today) are cached once
and reused forever - history doesn't change in hindsight. The current,
still-running season is cached per calendar day: re-running backtest.py
again today (e.g. to compare a window or feature change) reuses today's
cache instead of re-fetching; a new day triggers one fresh full-season
refetch. Each MeteoSwiss station archive's expensive part (STAC catalog
discovery + downloading every historical CSV) runs once ever per station;
every later call just merges in the cheap "recent" file.

Caches the FULL day (all 24 hours) regardless of backtest.py's current
WINDOW_START_HOUR/WINDOW_END_HOUR, since fetch_raw_historical already
returns unfiltered data and window filtering happens downstream - so a
future window change (like the one this cache was built to enable testing
of) never needs a re-fetch either.
"""

import json
import os
from datetime import datetime, timezone

from features import fetch_raw_historical
from meteoswiss import fetch_pressure_observations, fetch_sam_hourly_observations, fetch_station_observations

CACHE_DIR = os.path.join(os.path.dirname(__file__), "logs", "raw_cache")
SAMEDAN_CACHE_PATH = os.path.join(CACHE_DIR, "samedan_archive.json")
# Deliberately the same file historical_data._generic_raw_cache_path("sia")
# reads, so one committed cache serves BOTH backtest.py's labeling and an
# offline `historical_data.py sync` (the same double duty
# samedan_archive.json already performs for sam).
SIA_CACHE_PATH = os.path.join(CACHE_DIR, "generic_sia.json")


def _season_cache_path(year):
    return os.path.join(CACHE_DIR, f"season_{year}.json")


def _station_cache_path(station):
    return os.path.join(CACHE_DIR, f"pressure_{station}.json")


def get_season_raw(start_date: str, end_date: str, year: int, is_closed: bool) -> dict:
    """Raw weather dict for one season (same shape as
    features.fetch_raw_historical), served from cache when possible."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _season_cache_path(year)

    if os.path.exists(path):
        with open(path) as f:
            cached = json.load(f)
        if is_closed or cached.get("_cached_end_date") == end_date:
            print(f"  [cache] {year}: using cached raw data (as of {cached.get('_cached_end_date')})")
            return cached["raw"]

    print(f"  [cache] {year}: no usable cache, fetching fresh ({start_date} to {end_date})...")
    raw = fetch_raw_historical(start_date, end_date)
    with open(path, "w") as f:
        json.dump({"_cached_end_date": end_date, "raw": raw}, f)
    return raw


def _get_station_archive(cache_path: str, label: str, fetch_fn) -> dict:
    """Shared caching logic for any {datetime_utc: {...}} MeteoSwiss
    station archive: full historical fetch once, cheap "recent" merge
    every call after that."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        obs = {datetime.fromisoformat(k): v for k, v in cached.items()}
        print(f"  [cache] {label}: using cached archive ({len(obs)} hours), refreshing recent tail...")
        recent = fetch_fn(include_historical=False)
        obs.update(recent)
    else:
        print(f"  [cache] {label}: no cache yet, fetching full historical archive (this is the slow part)...")
        obs = fetch_fn(include_historical=True)

    with open(cache_path, "w") as f:
        json.dump({dt.isoformat(): v for dt, v in obs.items()}, f)
    return obs


def get_samedan_archive() -> dict:
    """Full {datetime_utc: {speed_kmh, gust_kmh}} Samedan archive."""
    return _get_station_archive(SAMEDAN_CACHE_PATH, "Samedan", fetch_sam_hourly_observations)


def get_sia_archive() -> dict:
    """Full {datetime_utc: {normalized_field: value}} Segl-Maria (SIA)
    hourly archive - wind already in m/s (meteoswiss.parse_generic_station_csv
    converts during parsing, unlike the sam archive's raw km/h). The
    ground-truth source for backtest.py's SIA-first labeling."""
    def fetch_fn(include_historical):
        return fetch_station_observations("sia", include_historical=include_historical)["observations"]
    return _get_station_archive(SIA_CACHE_PATH, "Segl-Maria (SIA)", fetch_fn)


def get_pressure_archive(station: str) -> dict:
    """Full {datetime_utc: {pressure_hpa}} archive for a real MeteoSwiss
    station (use meteoswiss.LUGANO_STATION / ZURICH_STATION)."""
    def fetch_fn(include_historical):
        return fetch_pressure_observations(station, include_historical=include_historical)
    return _get_station_archive(_station_cache_path(station), station, fetch_fn)
