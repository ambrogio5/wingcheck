"""
meteoswiss.py - shared access to MeteoSwiss's official open data (SwissMetNet).

Real, licensed, free station observations - data.geo.admin.ch, no API key.
Used as ground truth: what actually happened, as opposed to another model's
guess. Station SAM (Samedan) is ~10km from Silvaplana; MeteoSwiss's own
writeup on valley winds notes the Malojawind reaches that far.

Also provides real sea-level pressure from Lugano (station "lug") and
Zurich/Fluntern (station "sma") - confirmed against the live API on
2026-07-16 (station codes and the real column name, pp0qffh0, not the
plausible-looking but wrong pp0qffs0 an earlier docstring guessed). This
feeds features.py's pressure_nowcast_score, a NOWCAST feature (current
measured pressure gradient) - it is NOT a substitute for pressure_signal,
which is deliberately Open-Meteo FORECAST data: pressure_signal scores a
1-3 day-ahead target hour, and a real observation can't exist yet for a
future hour.

Instead of guessing file names (which vary and change), we ask the official
STAC catalog API which data files exist for the station, then download every
hourly CSV it lists. This survives MeteoSwiss renaming or re-splitting files.
"""

import csv
import io
from datetime import datetime, timezone

import requests

# Labeling threshold measured AT SAMEDAN. The Malojawind weakens as it runs
# down-valley, so wind at Samedan understates wind at the Silvaplana lake.
# 8kt at SAM is a first-guess proxy for "~10kt+ rideable at the lake".
# Tune this after comparing a few real sessions: if the model misses days
# you actually rode, lower it; if it flags days that were dead, raise it.
SAM_PROXY_KT = 8.0

LUGANO_STATION = "lug"
ZURICH_STATION = "sma"


def _stac_item_url(station: str) -> str:
    return f"https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-smn/items/{station}"


def _recent_url(station: str) -> str:
    return f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{station}/ogd-smn_{station}_h_recent.csv"


def _parse_wind_csv(text: str) -> dict:
    """Returns {datetime_utc: {"speed_kmh":..., "gust_kmh":...}}."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    obs = {}
    for row in reader:
        row = {k.lower(): v for k, v in row.items()}
        ts_raw = row.get("reference_timestamp") or row.get("time")
        if not ts_raw:
            continue
        try:
            dt = datetime.strptime(ts_raw.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            speed = float(row.get("fu3010h0", "") or "nan")
        except ValueError:
            continue
        try:
            gust = float(row.get("fu3010h1", "") or "nan")
        except ValueError:
            gust = float("nan")
        if speed != speed:  # NaN check
            continue
        obs[dt] = {"speed_kmh": speed, "gust_kmh": gust}
    return obs


def _parse_pressure_csv(text: str) -> dict:
    """Returns {datetime_utc: {"pressure_hpa": ...}} from the sea-level
    (QFF) pressure column."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    obs = {}
    for row in reader:
        row = {k.lower(): v for k, v in row.items()}
        ts_raw = row.get("reference_timestamp") or row.get("time")
        if not ts_raw:
            continue
        try:
            dt = datetime.strptime(ts_raw.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            pressure = float(row.get("pp0qffh0", "") or "nan")
        except ValueError:
            continue
        if pressure != pressure:  # NaN check
            continue
        obs[dt] = {"pressure_hpa": pressure}
    return obs


def _fetch_csv(url: str, parser) -> dict:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return parser(r.content.decode("utf-8", errors="replace"))


def _discover_hourly_urls(station: str) -> list:
    """Ask the STAC catalog which files exist for the station; return the
    URLs of all hourly (_h) CSV assets, historical files first."""
    r = requests.get(_stac_item_url(station), timeout=60)
    r.raise_for_status()
    assets = r.json().get("assets", {})
    urls = []
    for name, asset in assets.items():
        href = asset.get("href", "")
        lname = name.lower()
        # hourly granularity files look like ogd-smn_<station>_h*.csv
        if f"_{station}_h" in lname and lname.endswith(".csv"):
            urls.append(href)
    # historical files first so 'recent' overwrites overlaps last
    urls.sort(key=lambda u: ("recent" in u, "now" in u, u))
    return urls


def _fetch_station_observations(station: str, parser, include_historical: bool) -> dict:
    recent_url = _recent_url(station)
    if not include_historical:
        try:
            return _fetch_csv(recent_url, parser)
        except requests.RequestException as e:
            print(f"[warn] could not fetch {station} recent data: {e}")
            return {}

    obs = {}
    try:
        urls = _discover_hourly_urls(station)
    except requests.RequestException as e:
        print(f"[warn] {station} STAC catalog lookup failed ({e}); falling back to recent file only")
        urls = [recent_url]

    if not urls:
        print(f"[warn] {station} catalog listed no hourly files; falling back to recent file")
        urls = [recent_url]

    for url in urls:
        try:
            part = _fetch_csv(url, parser)
            obs.update(part)
            print(f"  loaded {len(part)} hours from {url.rsplit('/', 1)[-1]}")
        except requests.RequestException as e:
            print(f"[warn] could not fetch {url}: {e}")

    return obs


def fetch_wind_observations(station: str, include_historical: bool = True) -> dict:
    """Returns {datetime_utc: {"speed_kmh":..., "gust_kmh":...}} for ANY
    SwissMetNet station code exposing the fu3010h0/fu3010h1 wind columns
    (confirmed for "sam" against the live API on 2026-07-16 - other
    stations are expected to use the same column names since they're part
    of the same standardized SwissMetNet CSV format, but that has not been
    independently confirmed for every station; historical_data.py's sync
    command surfaces a parse failure clearly rather than silently
    substituting a guess). Generalizes fetch_sam_hourly_observations below,
    which is now a thin wrapper kept for backward compatibility."""
    return _fetch_station_observations(station, _parse_wind_csv, include_historical)


def fetch_sam_hourly_observations(include_historical: bool = True) -> dict:
    """Returns {datetime_utc: {"speed_kmh":..., "gust_kmh":...}} for Samedan
    (SAM) - the model's ground-truth fallback (see kitesailing_weather.py
    for the primary one) and, via features.py, a wind nowcast feature.

    include_historical=False: just the rolling recent file (fast, used by
    the daily verification job).
    include_historical=True: everything the catalog lists (used by the
    backtest to cover 2024+)."""
    return fetch_wind_observations("sam", include_historical)


def fetch_pressure_observations(station: str, include_historical: bool = True) -> dict:
    """Returns {datetime_utc: {"pressure_hpa": ...}} for the given real
    SwissMetNet station (use LUGANO_STATION / ZURICH_STATION). Feeds
    features.py's pressure_nowcast_score - see this module's docstring for
    why that's a nowcast feature, not a replacement for pressure_signal."""
    return _fetch_station_observations(station, _parse_pressure_csv, include_historical)
