"""
meteoswiss.py - shared access to MeteoSwiss's official open data (SwissMetNet).

Real, licensed, free station observations - data.geo.admin.ch, no API key.
Used as ground truth: what actually happened, as opposed to another model's
guess. Station SAM (Samedan) is ~10km from Silvaplana; MeteoSwiss's own
writeup on valley winds notes the Malojawind reaches that far.

MeteoSwiss splits each station's data into a "historical" file (older,
static) and a "recent" file (rolling window, updated regularly). We fetch
both and merge, since a backtest spanning 2024-2026 will straddle that split.
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
BASE = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{STATION}"
HISTORICAL_URL = f"{BASE}/ogd-smn_{STATION}_h_historical.csv"
RECENT_URL = f"{BASE}/ogd-smn_{STATION}_h_recent.csv"


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
            gust = float(row.get("fu3010h1", "") or "nan")
        except ValueError:
            continue
        if speed != speed:  # NaN check
            continue
        obs[dt] = {"speed_kmh": speed, "gust_kmh": gust}
    return obs


def fetch_sam_hourly_observations(include_historical: bool = True) -> dict:
    """Returns {datetime_utc: {"speed_kmh":..., "gust_kmh":...}} merged
    across the historical and recent files."""
    obs = {}
    if include_historical:
        try:
            r = requests.get(HISTORICAL_URL, timeout=60)
            r.raise_for_status()
            obs.update(_parse_csv(r.content.decode("utf-8", errors="replace")))
        except requests.RequestException as e:
            print(f"[warn] could not fetch SAM historical data: {e}")

    try:
        r = requests.get(RECENT_URL, timeout=60)
        r.raise_for_status()
        obs.update(_parse_csv(r.content.decode("utf-8", errors="replace")))  # recent wins on overlap
    except requests.RequestException as e:
        print(f"[warn] could not fetch SAM recent data: {e}")

    return obs
