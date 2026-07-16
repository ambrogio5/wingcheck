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
    11. shortwave_radiation
    12. precipitation
  Upper air near Maloja Pass (46.4030N 9.6880E), pressure-level data:
    13. wind_speed_700hPa
    14. wind_direction_700hPa
    15. wind_speed_850hPa
    16. wind_direction_850hPa
  Airmass / instability, at Silvaplana coordinates:
    17. freezing_level_height
    18. cape
    19. snow_depth
  Multi-model ensemble wind_speed_10m at Silvaplana (icon_seamless,
  gfs_seamless, ecmwf_ifs025), fetched separately via the `models=` param:
    20. wind_speed_10m (per model) - averaged + spread into two features;
        best-effort, the pipeline runs fine if this fetch fails.

MeteoSwiss official open data (data.geo.admin.ch) - real station observations,
confirmed against the live API on 2026-07-16 (station codes and column names
below were previously guessed and wrong in this docstring - fu3010z0/z1 and
pp0qffs0 don't exist; the real columns are h0/h1-suffixed):
    Samedan (SAM) - fu3010h0 (wind speed), fu3010h1 (gust). Direction
      (dkl010h0) is available in the data but not currently parsed/used.
    Lugano (LUG) / Zurich-Fluntern (SMA) - pp0qffh0 (sea-level pressure)

  Samedan now serves two purposes: verify_and_learn.py's ground truth
  fallback (see kitesailing_weather.py for the primary one), and, here,
  21. a real-time nowcast feature (samedan_morning_score) - its own
      measured wind ~10km up-valley around 07:00 local the same day, a
      genuine upstream precursor signal rather than another model forecast.
  Lugano/Zurich real pressure similarly feeds
  22. pressure_nowcast_score - the ACTUAL measured pressure gradient this
      morning, distinct from pressure_signal (feature 2 below), which is
      deliberately Open-Meteo FORECAST data: pressure_signal scores a
      1-3 day-ahead target hour, and a real observation can't exist yet for
      a future hour, so it can't be swapped for real data the way Samedan's
      ground-truth role could be.
"""

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from meteoswiss import LUGANO_STATION, ZURICH_STATION, fetch_pressure_observations, fetch_sam_hourly_observations

TIMEZONE = "Europe/Zurich"

SILVAPLANA = (46.4573, 9.7967)
BREGAGLIA = (46.3603, 9.6398)     # Vicosoprano
MALOJA_PASS = (46.4030, 9.6880)


def _get(lat, lon, hourly_vars, forecast_days=None, start_date=None, end_date=None, models=None):
    params = (
        f"latitude={lat}&longitude={lon}"
        f"&hourly={','.join(hourly_vars)}"
        f"&timezone={TIMEZONE}&wind_speed_unit=kmh"
    )
    if models:
        params += f"&models={','.join(models)}"
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
    "temperature_2m", "dew_point_2m",
    "shortwave_radiation", "precipitation",
]
_UPPER_VARS = [
    "wind_speed_700hPa", "wind_direction_700hPa",
    "wind_speed_850hPa", "wind_direction_850hPa",
]

# Independent NWP models for the Silvaplana surface wind, fetched alongside
# the main "best_match" forecast. Averaging them damps single-model error,
# and their spread is itself a signal (models agreeing -> more trustworthy
# forecast). Best-effort: a fetch failure here must not break the whole
# pipeline, since the deterministic forecast alone is still enough to run on.
_ENSEMBLE_MODELS = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"]


def _fetch_ensemble_wind(lat, lon, forecast_days=None, start_date=None, end_date=None):
    try:
        return _get(lat, lon, ["wind_speed_10m"], forecast_days=forecast_days,
                     start_date=start_date, end_date=end_date, models=_ENSEMBLE_MODELS)
    except RuntimeError as e:
        print(f"[warn] multi-model ensemble fetch failed, continuing without it: {e}")
        return None


def _fetch_samedan_recent():
    """Best-effort: real-time Samedan conditions as of right now, used as
    the samedan_morning_score nowcast feature. Not the ground-truth fetch -
    verify_and_learn.py fetches its own copy for labeling; this one just
    feeds the model. A failure here must not break the forecast pipeline."""
    try:
        return fetch_sam_hourly_observations(include_historical=False)
    except Exception as e:
        print(f"[warn] Samedan nowcast fetch failed, continuing without it: {e}")
        return None


def _fetch_pressure_recent(station):
    """Best-effort: real-time pressure at a real MeteoSwiss station, used
    as the pressure_nowcast_score feature. A failure here must not break
    the forecast pipeline."""
    try:
        return fetch_pressure_observations(station, include_historical=False)
    except Exception as e:
        print(f"[warn] {station} pressure nowcast fetch failed, continuing without it: {e}")
        return None


def fetch_raw(forecast_days=3):
    """Forward-looking forecast (used by forecast_and_log.py)."""
    return {
        "silvaplana": _get(*SILVAPLANA, _SILVAPLANA_VARS, forecast_days=forecast_days),
        "bregaglia": _get(*BREGAGLIA, _BREGAGLIA_VARS, forecast_days=forecast_days),
        "upper": _get(*MALOJA_PASS, _UPPER_VARS, forecast_days=forecast_days),
        "lugano": _get(46.0037, 8.9511, ["pressure_msl"], forecast_days=forecast_days),
        "zurich": _get(47.3769, 8.5417, ["pressure_msl"], forecast_days=forecast_days),
        "ensemble": _fetch_ensemble_wind(*SILVAPLANA, forecast_days=forecast_days),
        "samedan_obs": _fetch_samedan_recent(),
        "lugano_obs": _fetch_pressure_recent(LUGANO_STATION),
        "zurich_obs": _fetch_pressure_recent(ZURICH_STATION),
    }


def fetch_raw_historical(start_date: str, end_date: str):
    """Same shape as fetch_raw, but for a past date range (YYYY-MM-DD),
    used by backtest.py. Pulled from the Historical Forecast API archive.
    A short pause between requests keeps us under burst limits.

    Does NOT set "samedan_obs" / "lugano_obs" / "zurich_obs" - backtest.py
    already fetches the full multi-year archives for these once (via
    historical_cache.py) and injects them into raw itself, rather than this
    function re-fetching them (large, rate-limited) again per season."""
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
    out["ensemble"] = _fetch_ensemble_wind(*SILVAPLANA, start_date=start_date, end_date=end_date)
    time.sleep(3)
    return out


def _angle_diff_score(angle, ideal, half_width):
    """1.0 at `ideal` degrees, decaying to 0 at +/- half_width. Handles wraparound."""
    d = abs((angle - ideal + 180) % 360 - 180)
    return max(0.0, 1.0 - d / half_width)


def _lookup_morning_obs(obs, date):
    """The observation nearest 07:00 local on `date` (a naive midnight
    datetime), within +/-90 min, or None. Generic over any {datetime_utc:
    {...}} obs dict - shared by engineer_features (samedan_morning_score,
    pressure_nowcast_score) and raw_snapshot (the unnormalized readings),
    so they can never quietly disagree on which observation they picked."""
    morning_local = date.replace(hour=7, tzinfo=ZoneInfo("Europe/Zurich"))
    morning_utc = morning_local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for offset_min in (0, 60, -60, 90, -90):
        cand = morning_utc + timedelta(minutes=offset_min)
        if cand in obs:
            return obs[cand]
    return None


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

    # 10-12. The model's own surface forecast at the spot. Diagnostics on the
    # first backtest showed the driver-only approach left most of the signal
    # on the table: the point forecast itself is the strongest single input,
    # and the drivers act as corrections on top of it.
    model_wind = sil["wind_speed_10m"][idx] / 20.0          # ~[0, 2]
    model_gust = sil["wind_gusts_10m"][idx] / 30.0          # ~[0, 2]
    dir10 = sil["wind_direction_10m"][idx]
    surface_dir_alignment = _angle_diff_score(dir10, 230, 90) * 2 - 1  # SW = Maloja

    # 13-16. Time-of-day and seasonality. The Maloja wind is a thermal with a
    # strong diurnal cycle (Samedan data: 5.5kt at noon -> 9.7kt at 16-17h)
    # and a seasonal one; sin/cos encoding lets the linear model learn both.
    import math as _math
    t = raw["silvaplana"]["time"][idx]
    hour = int(t[11:13])
    date = datetime.fromisoformat(t[:10])
    doy = date.timetuple().tm_yday
    hour_sin = _math.sin(2 * _math.pi * hour / 24)
    hour_cos = _math.cos(2 * _math.pi * hour / 24)
    doy_sin = _math.sin(2 * _math.pi * doy / 365)
    doy_cos = _math.cos(2 * _math.pi * doy / 365)

    # 17. Multi-model ensemble mean + agreement for the surface wind. Falls
    # back to the single deterministic forecast (neutral 0.5 agreement) if
    # the ensemble fetch failed or didn't cover this hour - see
    # _fetch_ensemble_wind's best-effort contract.
    ens = raw.get("ensemble")
    ens_vals = []
    if ens:
        for key, series in ens.items():
            if key.startswith("wind_speed_10m") and idx < len(series) and series[idx] is not None:
                ens_vals.append(series[idx])
    if ens_vals:
        ensemble_wind_score = (sum(ens_vals) / len(ens_vals)) / 20.0
        ensemble_agreement_score = 1.0 - min(1.0, (max(ens_vals) - min(ens_vals)) / 20.0)
    else:
        ensemble_wind_score = model_wind
        ensemble_agreement_score = 0.5

    # 18. Persistence - wind already forecast/observed in the hours leading
    # up to idx, capturing whether it's building. An instantaneous snapshot
    # misses this trend.
    lag_idxs = [i for i in (idx - 1, idx - 2, idx - 3) if i >= 0]
    if lag_idxs:
        persistence_wind = (sum(sil["wind_speed_10m"][i] for i in lag_idxs) / len(lag_idxs)) / 20.0
    else:
        persistence_wind = model_wind

    # 19-20. Interaction terms - out of every pairwise product tested against
    # logs/backtest_dataset.jsonl, these two gave the largest correlation gain
    # over their individual components.
    upper_wind_dewpoint_interaction = upper_wind_speed_score * dewpoint_score
    thermal_seasonal_interaction = thermal_excess * doy_cos

    # 21. Samedan "morning nowcast" - Samedan is no longer the ground truth
    # (see verify_and_learn.py, which now labels against the real
    # kitesailing.ch Silvaplana reading), but it's real, measured, current
    # wind ~10km up-valley, which the model has never gotten to use as a
    # live precursor signal before. This is its actual observed wind at (or
    # near) 07:00 local the same day - the first forecast run of the day -
    # a genuine upstream nowcast, distinct from the NWP-model-based
    # persistence/ensemble features above. Falls back to a neutral 0.0 if
    # Samedan data isn't available for that morning (best-effort fetch, or
    # historical archive gap).
    samedan_obs = raw.get("samedan_obs")
    samedan_morning_score = 0.0
    if samedan_obs:
        morning_obs = _lookup_morning_obs(samedan_obs, date)
        if morning_obs is not None:
            samedan_morning_score = morning_obs["speed_kmh"] / 20.0

    # 22. Pressure NOWCAST - the same Lugano-Zurich gradient as
    # pressure_signal (feature 2), but from real MeteoSwiss station
    # observations as of this morning rather than the forecast model.
    # Genuinely additive information (a real measurement, not another
    # forecast), unlike pressure_signal itself which has to stay
    # forecast-based since it scores a 1-3 day-ahead target hour a real
    # observation can't exist for yet. Same peaked scoring as
    # pressure_signal (favorable near -1.25 hPa) for comparability. Falls
    # back to a neutral 0.0 if either station's morning reading is missing.
    lugano_obs = raw.get("lugano_obs")
    zurich_obs = raw.get("zurich_obs")
    pressure_nowcast_score = 0.0
    if lugano_obs and zurich_obs:
        lug_morning = _lookup_morning_obs(lugano_obs, date)
        zur_morning = _lookup_morning_obs(zurich_obs, date)
        if lug_morning is not None and zur_morning is not None:
            p_diff_now = lug_morning["pressure_hpa"] - zur_morning["pressure_hpa"]
            pressure_nowcast_score = max(-1.0, min(1.0, 1.0 - abs(p_diff_now + 1.25) / 4.0))

    return {
        "thermal_excess": thermal_excess,
        "pressure_signal": pressure_signal,
        "upper_wind_alignment": upper_wind_alignment,
        "upper_wind_speed_score": upper_wind_speed_score,
        "dewpoint_score": dewpoint_score,
        "cape_penalty": cape_penalty,
        "freezing_level_score": freezing_level_score,
        "precip_penalty": precip_penalty,
        "model_wind": model_wind,
        "model_gust": model_gust,
        "surface_dir_alignment": surface_dir_alignment,
        "hour_sin": hour_sin,
        "hour_cos": hour_cos,
        "doy_sin": doy_sin,
        "doy_cos": doy_cos,
        "ensemble_wind_score": ensemble_wind_score,
        "ensemble_agreement_score": ensemble_agreement_score,
        "persistence_wind": persistence_wind,
        "upper_wind_dewpoint_interaction": upper_wind_dewpoint_interaction,
        "thermal_seasonal_interaction": thermal_seasonal_interaction,
        "samedan_morning_score": samedan_morning_score,
        "pressure_nowcast_score": pressure_nowcast_score,
    }


_SILVAPLANA_RAW_KEYS = (
    "temperature_2m", "dew_point_2m", "relative_humidity_2m", "pressure_msl",
    "cloud_cover", "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m",
    "freezing_level_height", "cape", "snow_depth",
)
_BREGAGLIA_RAW_KEYS = ("temperature_2m", "dew_point_2m", "shortwave_radiation", "precipitation")
_UPPER_RAW_KEYS = (
    "wind_speed_700hPa", "wind_direction_700hPa",
    "wind_speed_850hPa", "wind_direction_850hPa",
)


def raw_snapshot(raw, idx):
    """Every raw physical value behind raw[...][idx], unnormalized - logged
    alongside engineer_features' output so today's live-forecast snapshot
    isn't lost. Open-Meteo's live API only serves ~3 months of history, and
    even backtest.py's historical-archive fetch doesn't reproduce a genuine
    multi-day-lead forecast (see its own docstring) - so once a live
    prediction ages past that window, this is the only remaining record of
    exactly what the forecast said at the time. Useful for building new
    features later without needing data that no API can hand back."""
    sil, bre, up = raw["silvaplana"], raw["bregaglia"], raw["upper"]

    snapshot = {
        "silvaplana": {k: sil[k][idx] for k in _SILVAPLANA_RAW_KEYS},
        "bregaglia": {k: bre[k][idx] for k in _BREGAGLIA_RAW_KEYS},
        "upper": {k: up[k][idx] for k in _UPPER_RAW_KEYS},
        "lugano_pressure_msl": raw["lugano"]["pressure_msl"][idx],
        "zurich_pressure_msl": raw["zurich"]["pressure_msl"][idx],
    }

    ens = raw.get("ensemble")
    if ens:
        snapshot["ensemble_wind_speed_10m"] = {
            key: series[idx] for key, series in ens.items()
            if key.startswith("wind_speed_10m") and idx < len(series) and series[idx] is not None
        }

    date = datetime.fromisoformat(sil["time"][idx][:10])

    samedan_obs = raw.get("samedan_obs")
    if samedan_obs:
        morning_obs = _lookup_morning_obs(samedan_obs, date)
        if morning_obs is not None:
            snapshot["samedan_morning"] = morning_obs

    lugano_obs = raw.get("lugano_obs")
    if lugano_obs:
        morning_obs = _lookup_morning_obs(lugano_obs, date)
        if morning_obs is not None:
            snapshot["lugano_morning"] = morning_obs

    zurich_obs = raw.get("zurich_obs")
    if zurich_obs:
        morning_obs = _lookup_morning_obs(zurich_obs, date)
        if morning_obs is not None:
            snapshot["zurich_morning"] = morning_obs

    return snapshot
