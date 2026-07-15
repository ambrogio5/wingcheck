"""
meteoswiss.py - shared access to MeteoSwiss's official open data (SwissMetNet).

Real, licensed, free station observations - data.geo.admin.ch, no API key.
Used as ground truth: what actually happened, as opposed to another model's
guess. Station SAM (Samedan) is ~10km from Silvaplana; MeteoSwiss's own
writeup on valley winds notes the Malojawind reaches that far.

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

STATION = "sam"
STAC_ITEM_URL = f"https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-smn/items/{STATION}"
RECENT_URL = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{STATION}/ogd-smn_{STATION}_h_recent.csv"


def _parse_csv(text: str) -> dict:
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


def _fetch_csv(url: str) -> dict:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return _parse_csv(r.content.decode("utf-8", errors="replace"))


def _discover_hourly_urls() -> list:
    """Ask the STAC catalog which files exist for the station; return the
    URLs of all hourly (_h) CSV assets, historical files first."""
    r = requests.get(STAC_ITEM_URL, timeout=60)
    r.raise_for_status()
    assets = r.json().get("assets", {})
    urls = []
    for name, asset in assets.items():
        href = asset.get("href", "")
        lname = name.lower()
        # hourly granularity files look like ogd-smn_sam_h*.csv
        if f"_{STATION}_h" in lname and lname.endswith(".csv"):
            urls.append(href)
    # historical files first so 'recent' overwrites overlaps last
    urls.sort(key=lambda u: ("recent" in u, "now" in u, u))
    return urls


def fetch_sam_hourly_observations(include_historical: bool = True) -> dict:
    """Returns {datetime_utc: {"speed_kmh":..., "gust_kmh":...}}.

    include_historical=False: just the rolling recent file (fast, used by
    the daily verification job).
    include_historical=True: everything the catalog lists (used by the
    backtest to cover 2024+)."""
    if not include_historical:
        try:
            return _fetch_csv(RECENT_URL)
        except requests.RequestException as e:
            print(f"[warn] could not fetch SAM recent data: {e}")
            return {}

    obs = {}
    try:
        urls = _discover_hourly_urls()
    except requests.RequestException as e:
        print(f"[warn] STAC catalog lookup failed ({e}); falling back to recent file only")
        urls = [RECENT_URL]

    if not urls:
        print("[warn] catalog listed no hourly files; falling back to recent file")
        urls = [RECENT_URL]

    for url in urls:
        try:
            part = _fetch_csv(url)
            obs.update(part)
            print(f"  loaded {len(part)} hours from {url.rsplit('/',1)[-1]}")
        except requests.RequestException as e:
            print(f"[warn] could not fetch {url}: {e}")

    return obs
