"""
features.py - pulls all raw data points for the Malojawind model and turns
them into a normalized feature vector.

RAW DATA POINTS (20), by source:

Open-Meteo forecast model (api.open-meteo.com) - free, no key:
  Silvaplana (target spot, 46.4573N 9.7967E):
    1. temperature_2m
    2. dew_point_2m
    3. relative_humidity_2m
    4. pressure_msl
    5. cloud_cover
    6. wind_speed_10m      (logged as the "actual" model output, not a predictor)
    7. wind_gusts_10m      (same)
    8. wind_direction_10m  (same)
  Vicosoprano / Val Bregaglia (46.3603N 9.6398E) - the source valley:
    9. temperature_2m
    10. dew_point_2m
    11. cloud_cover
    12. shortwave_radiation
    13. precipitation
  Upper air near Maloja Pass (46.4030N 9.6880E), pressure-level data:
    14. wind_speed_700hPa
    15. wind_direction_700hPa
    16. wind_speed_850hPa
    17. wind_direction_850hPa
  Airmass / instability, at Silvaplana coordinates:
    18. freezing_level_height
    19. cape
    20. snow_depth

MeteoSwiss official open data (data.geo.admin.ch) - real station observations,
used ONLY for ground-truth verification (ground truth needs to be real, not
another model run):
    Samedan (SAM) - fu3010z0 (wind speed), fu3010z1 (gust), dkl010z0 (dir)
    Lugano (LUG) / Chur (CHU) - pp0qffs0 (sea-level pressure, real obs)
"""

import time

import requests

TIMEZONE = "Europe/Zurich"

SILVAPLANA = (46.4573, 9.7967)
BREGAGLIA = (46.3603, 9.6398)     # Vicosoprano
MALOJA_PASS = (46.4030, 9.6880)


def _get(lat, lon, hourly_vars, forecast_days=None, start_date=None, end_date=None):
    params = (
        f"latitude={lat}&longitude={lon}"
        f"&hourly={','.join(hourly_vars)}"
        f"&timezone={TIMEZONE}&wind_speed_unit=kmh"
    )
    if start_date and end_date:
        # Historical Forecast API: separate host from the live endpoint, but
        # same variables (pressure levels / cape / freezing level included)
        # and same response shape. Archive available from ~2021. The live
        # api.open-meteo.com host only serves ~3 months of past data, so
        # multi-year backtests MUST go through this host.
        params += f"&start_date={start_date}&end_date={end_date}"
        url = f"https://historical-forecast-api.open-meteo.com/v1/forecast?{params}"
    else:
        params += f"&forecast_days={forecast_days or 3}"
        url = f"https://api.open-meteo.com/v1/forecast?{params}"
    # Historical pulls are large and Open-Meteo rate-limits bursts, so:
    # generous timeout + up to 4 attempts with growing pauses between them.
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            return r.json()["hourly"]
        except (requests.RequestException, KeyError, ValueError) as e:
            last_err = e
            wait = 15 * (attempt + 1)
            print(f"[warn] Open-Meteo request failed (attempt {attempt+1}/4): {e} — retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Open-Meteo request failed after 4 attempts: {last_err}")


_SILVAPLANA_VARS = [
    "temperature_2m", "dew_point_2m", "relative_humidity_2m",
    "pressure_msl", "cloud_cover", "wind_speed_10m",
    "wind_gusts_10m", "wind_direction_10m",
    "freezing_level_height", "cape", "snow_depth",
]
_BREGAGLIA_VARS = [
    "temperature_2m", "dew_point_2m", "cloud_cover",
    "shortwave_radiation", "precipitation",
]
_UPPER_VARS = [
    "wind_speed_700hPa", "wind_direction_700hPa",
    "wind_speed_850hPa", "wind_direction_850hPa",
]


def fetch_raw(forecast_days=3):
    """Forward-looking forecast (used by forecast_and_log.py)."""
    return {
        "silvaplana": _get(*SILVAPLANA, _SILVAPLANA_VARS, forecast_days=forecast_days),
        "bregaglia": _get(*BREGAGLIA, _BREGAGLIA_VARS, forecast_days=forecast_days),
        "upper": _get(*MALOJA_PASS, _UPPER_VARS, forecast_days=forecast_days),
        "lugano": _get(46.0037, 8.9511, ["pressure_msl"], forecast_days=forecast_days),
        "zurich": _get(47.3769, 8.5417, ["pressure_msl"], forecast_days=forecast_days),
    }


def fetch_raw_historical(start_date: str, end_date: str):
    """Same shape as fetch_raw, but for a past date range (YYYY-MM-DD),
    used by backtest.py. Pulled from the Historical Forecast API archive.
    A short pause between the five requests keeps us under burst limits."""
    out = {}
    specs = [
        ("silvaplana", SILVAPLANA, _SILVAPLANA_VARS),
        ("bregaglia", BREGAGLIA, _BREGAGLIA_VARS),
        ("upper", MALOJA_PASS, _UPPER_VARS),
        ("lugano", (46.0037, 8.9511), ["pressure_msl"]),
        ("zurich", (47.3769, 8.5417), ["pressure_msl"]),
    ]
    for key, (lat, lon), hourly_vars in specs:
        out[key] = _get(lat, lon, hourly_vars, start_date=start_date, end_date=end_date)
        time.sleep(3)
    return out


def _angle_diff_score(angle, ideal, half_width):
    """1.0 at `ideal` degrees, decaying to 0 at +/- half_width. Handles wraparound."""
    d = abs((angle - ideal + 180) % 360 - 180)
    return max(0.0, 1.0 - d / half_width)


def engineer_features(raw, idx):
    """Turn raw[idx] into the named feature dict the model scores on."""
    sil = raw["silvaplana"]
    bre = raw["bregaglia"]
    up = raw["upper"]
    lug = raw["lugano"]
    zur = raw["zurich"]

    # 1. Thermal contrast Bregaglia vs Engadin (elevation-adjusted baseline ~4.2C)
    temp_diff = bre["temperature_2m"][idx] - sil["temperature_2m"][idx]
    thermal_excess = (temp_diff - 4.2) / 5.0          # roughly -1..+1 typical range

    # 2. Synoptic pressure gradient (Lugano - Zurich); favorable near 0 to -2.5 hPa
    p_diff = lug["pressure_msl"][idx] - zur["pressure_msl"][idx]
    pressure_signal = 1.0 - abs(p_diff + 1.25) / 4.0   # peak near -1.25 hPa
    pressure_signal = max(-1.0, min(1.0, pressure_signal))

    # 3. Upper wind alignment at 700hPa - SW (~230 deg) amplifies, NE suppresses
    dir700 = up["wind_direction_700hPa"][idx]
    upper_wind_alignment = _angle_diff_score(dir700, 230, 90) * 2 - 1   # -1..+1

    # 4. Upper wind speed - want reinforcement (15-40km/h), not overwhelming (>60)
    spd700 = up["wind_speed_700hPa"][idx]
    if spd700 <= 40:
        upper_wind_speed_score = min(1.0, spd700 / 25)
    else:
        upper_wind_speed_score = max(0.0, 1.0 - (spd700 - 40) / 30)

    # 5. Cloud cover over Bregaglia (need sun on the slopes)
    cloud_score = 1.0 - (bre["cloud_cover"][idx] / 100.0)

    # 6. Dew point spread at Bregaglia (drier air heats faster)
    dp_spread = bre["temperature_2m"][idx] - bre["dew_point_2m"][idx]
    dewpoint_score = max(0.0, min(1.0, dp_spread / 12.0))

    # 7. CAPE - high instability risks storms that disrupt the thermal circulation
    cape = sil["cape"][idx]
    cape_penalty = -min(1.0, cape / 800.0)             # 0 = no risk, -1 = high risk

    # 8. Freezing level - proxy for airmass character (matches "zero termico" in
    #    the Tivano-style infographics); very low freezing level -> cold/unstable
    #    airmass, less favorable for a clean thermal cycle.
    fl = sil["freezing_level_height"][idx]
    freezing_level_score = max(-1.0, min(1.0, (fl - 3000) / 1500))

    # 9. Precipitation at Bregaglia - wet ground heats slower
    precip = bre["precipitation"][idx]
    precip_penalty = -min(1.0, precip / 3.0)

    return {
        "thermal_excess": thermal_excess,
        "pressure_signal": pressure_signal,
        "upper_wind_alignment": upper_wind_alignment,
        "upper_wind_speed_score": upper_wind_speed_score,
        "cloud_score": cloud_score,
        "dewpoint_score": dewpoint_score,
        "cape_penalty": cape_penalty,
        "freezing_level_score": freezing_level_score,
        "precip_penalty": precip_penalty,
    }
