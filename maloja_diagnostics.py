"""
maloja_diagnostics.py - fixed, transparent diagnostic families for "why
might/might not the Maloja wind work today," built on top of
station_features.py's per-station generic features.

Every diagnostic returns exactly:
    {"score": float, "status": str, "raw_values": dict, "sources": list,
     "explanation_key": str, "missing": bool}

Explanation keys are drawn from a small FIXED vocabulary per family (see
each function's *_KEYS dict below) - never unrestricted generated prose,
so a caller (the dashboard, a future report) can map keys to translated
human text without parsing free-form strings.

HONESTY ON MISSING DATA: several families (source heating, pass
activation, summit support, radiation support) are specified against
station roles (source_region, pass, summit) that have NO confirmed,
enabled station in config/stations.json yet (see docs/STATION_RESEARCH.md
- only sam/lug/sma are confirmed). In production, these currently report
missing=True with an honest *_missing_station_data explanation_key rather
than fabricating a score from data that doesn't exist. Every function
still fully implements its scoring logic and is exercised by tests using
fixture station-feature dicts, so the moment a real source-region/pass/
summit station is confirmed (station_registry.py), these diagnostics
start producing real, non-missing output with no code change needed.
"""

import math

# --- Source heating ---

SOURCE_HEATING_KEYS = {
    "favourable": "source_heating_favourable",
    "neutral": "source_heating_neutral",
    "unfavourable": "source_heating_unfavourable",
    "missing": "source_heating_missing_station_data",
}
SOURCE_HEATING_STRONG_DIFF_C = 3.0  # source region notably warmer than target region


def source_heating(source_feats: dict, target_feats: dict) -> dict:
    """source_feats/target_feats: station_features.generate_station_features()
    output for a source_region station (e.g. a future Bregaglia station)
    and a target_region station (sam)."""
    if not source_feats or not target_feats or source_feats.get("missing_indicator") == 1.0 \
            or target_feats.get("missing_indicator") == 1.0:
        return _result(0.0, "missing", {}, [], SOURCE_HEATING_KEYS["missing"], True)

    diff = None
    if source_feats.get("temperature_latest") is not None and target_feats.get("temperature_latest") is not None:
        diff = source_feats["temperature_latest"] - target_feats["temperature_latest"]
    warming = source_feats.get("temperature_change_since_sunrise")

    if diff is None:
        return _result(0.0, "missing", {}, [], SOURCE_HEATING_KEYS["missing"], True)

    raw = {"temperature_difference_c": round(diff, 2), "source_warming_since_sunrise_c": warming}
    if diff >= SOURCE_HEATING_STRONG_DIFF_C and (warming is None or warming > 0):
        return _result(min(1.0, diff / (2 * SOURCE_HEATING_STRONG_DIFF_C)), "favourable", raw,
                       ["source_region", "target_region"], SOURCE_HEATING_KEYS["favourable"], False)
    if diff <= 0:
        return _result(0.0, "unfavourable", raw, ["source_region", "target_region"],
                       SOURCE_HEATING_KEYS["unfavourable"], False)
    return _result(0.5, "neutral", raw, ["source_region", "target_region"], SOURCE_HEATING_KEYS["neutral"], False)


# --- Pass activation ---

PASS_ACTIVATION_KEYS = {
    "favourable": "pass_activation_favourable",
    "neutral": "pass_activation_neutral",
    "unfavourable": "pass_activation_unfavourable",
    "missing": "pass_activation_missing_station_data",
}
PASS_ALIGNED_SECTOR = (200, 260)  # degrees FROM, roughly SW - the thermal-onset alignment
PASS_ONSET_MIN_MS = 1.5


def pass_activation(pass_feats: dict) -> dict:
    """pass_feats: station_features output for a pass-role station (e.g. a
    future Maloja station)."""
    if not pass_feats or pass_feats.get("missing_indicator") == 1.0 or pass_feats.get("latest_wind_speed") is None:
        return _result(0.0, "missing", {}, [], PASS_ACTIVATION_KEYS["missing"], True)

    speed = pass_feats["latest_wind_speed"]
    direction = _direction_from_vector(pass_feats.get("wind_u"), pass_feats.get("wind_v"))
    raw = {"latest_wind_speed_ms": speed, "wind_direction_deg": direction, "wind_speed_trend_1h": pass_feats.get("wind_speed_trend_1h")}

    aligned = direction is not None and PASS_ALIGNED_SECTOR[0] <= direction <= PASS_ALIGNED_SECTOR[1]
    if speed >= PASS_ONSET_MIN_MS and aligned:
        return _result(min(1.0, speed / (2 * PASS_ONSET_MIN_MS)), "favourable", raw, ["pass"],
                       PASS_ACTIVATION_KEYS["favourable"], False)
    if speed < PASS_ONSET_MIN_MS:
        return _result(0.2, "unfavourable", raw, ["pass"], PASS_ACTIVATION_KEYS["unfavourable"], False)
    return _result(0.5, "neutral", raw, ["pass"], PASS_ACTIVATION_KEYS["neutral"], False)


# --- Summit support ---

SUMMIT_SUPPORT_KEYS = {
    "weak": "summit_support_weak",
    "supportive": "summit_support_supportive",
    "excessive": "summit_support_excessive",
    "opposing": "summit_support_opposing",
    "missing": "summit_support_missing_station_data",
}
SUMMIT_MIN_SUPPORTIVE_MS = 3.0
SUMMIT_MAX_SUPPORTIVE_MS = 12.0
SUMMIT_ALIGNED_SECTOR = (180, 280)  # SW-ish alignment reinforcing the thermal
SUMMIT_OPPOSING_SECTOR = (0, 90)    # NE-ish - directly opposing the expected thermal flow


def summit_support(summit_feats: dict) -> dict:
    """summit_feats: station_features output for a summit-role station
    (e.g. a future Corvatsch/Piz Nair station). Implements the transparent
    nonlinear status: weak / supportive / excessive / opposing / missing."""
    if not summit_feats or summit_feats.get("missing_indicator") == 1.0 or summit_feats.get("latest_wind_speed") is None:
        return _result(0.0, "missing", {}, [], SUMMIT_SUPPORT_KEYS["missing"], True)

    speed = summit_feats["latest_wind_speed"]
    direction = _direction_from_vector(summit_feats.get("wind_u"), summit_feats.get("wind_v"))
    raw = {"latest_wind_speed_ms": speed, "wind_direction_deg": direction}
    opposing = direction is not None and SUMMIT_OPPOSING_SECTOR[0] <= direction <= SUMMIT_OPPOSING_SECTOR[1]
    aligned = direction is not None and SUMMIT_ALIGNED_SECTOR[0] <= direction <= SUMMIT_ALIGNED_SECTOR[1]

    if opposing:
        return _result(0.0, "opposing", raw, ["summit"], SUMMIT_SUPPORT_KEYS["opposing"], False)
    if speed < SUMMIT_MIN_SUPPORTIVE_MS:
        return _result(0.2, "weak", raw, ["summit"], SUMMIT_SUPPORT_KEYS["weak"], False)
    if speed > SUMMIT_MAX_SUPPORTIVE_MS:
        return _result(0.3, "excessive", raw, ["summit"], SUMMIT_SUPPORT_KEYS["excessive"], False)
    score = 1.0 if aligned else 0.6
    return _result(score, "supportive", raw, ["summit"], SUMMIT_SUPPORT_KEYS["supportive"], False)


# --- Radiation support ---

RADIATION_SUPPORT_KEYS = {
    "favourable": "radiation_support_favourable",
    "neutral": "radiation_support_neutral",
    "unfavourable": "radiation_support_unfavourable",
    "missing": "radiation_support_missing_station_data",
}
RADIATION_STRONG_WM2 = 150.0
PRECIP_SUPPRESSIVE_MM = 1.0


def radiation_support(source_feats: dict) -> dict:
    """source_feats: station_features output for a station reporting
    radiation/precipitation (e.g. a future source-region station)."""
    if not source_feats or source_feats.get("missing_indicator") == 1.0:
        return _result(0.0, "missing", {}, [], RADIATION_SUPPORT_KEYS["missing"], True)
    radiation = source_feats.get("radiation_since_sunrise")
    precip = source_feats.get("precipitation_since_midnight")
    if radiation is None and precip is None:
        return _result(0.0, "missing", {}, [], RADIATION_SUPPORT_KEYS["missing"], True)

    raw = {"radiation_since_sunrise_wm2": radiation, "precipitation_since_midnight_mm": precip}
    if precip is not None and precip >= PRECIP_SUPPRESSIVE_MM:
        return _result(0.1, "unfavourable", raw, ["source_region"], RADIATION_SUPPORT_KEYS["unfavourable"], False)
    if radiation is not None and radiation >= RADIATION_STRONG_WM2:
        return _result(min(1.0, radiation / (2 * RADIATION_STRONG_WM2)), "favourable", raw,
                       ["source_region"], RADIATION_SUPPORT_KEYS["favourable"], False)
    return _result(0.5, "neutral", raw, ["source_region"], RADIATION_SUPPORT_KEYS["neutral"], False)


# --- Pressure support ---

PRESSURE_SUPPORT_KEYS = {
    "favourable": "pressure_support_favourable",
    "neutral": "pressure_support_neutral",
    "unfavourable": "pressure_support_unfavourable",
    "missing": "pressure_support_missing_station_data",
}
PRESSURE_GRADIENT_FAVOURABLE_HPA = 2.0  # Lugano (south) higher than Zurich (north)


def pressure_support(lugano_feats: dict, zurich_feats: dict, forecast_pressure_signal: float = None) -> dict:
    """lugano_feats/zurich_feats: station_features output for lug/sma (both
    real, confirmed, enabled stations - this diagnostic has real data in
    production, unlike the others in this module). forecast_pressure_signal
    is passed through ONLY for side-by-side reporting - it is kept
    completely separate from the observed gradient used for scoring, per
    this family's explicit "keep forecast and observed signals separate"
    requirement; it is never blended into the score."""
    if not lugano_feats or not zurich_feats or lugano_feats.get("missing_indicator") == 1.0 \
            or zurich_feats.get("missing_indicator") == 1.0:
        return _result(0.0, "missing", {}, [], PRESSURE_SUPPORT_KEYS["missing"], True)

    from station_features import pressure_difference
    observed_gradient = pressure_difference(lugano_feats, zurich_feats)
    if observed_gradient is None:
        return _result(0.0, "missing", {}, [], PRESSURE_SUPPORT_KEYS["missing"], True)

    raw = {
        "observed_gradient_hpa": observed_gradient,
        "forecast_pressure_signal": forecast_pressure_signal,  # reported, never scored on
    }
    if observed_gradient >= PRESSURE_GRADIENT_FAVOURABLE_HPA:
        return _result(min(1.0, observed_gradient / (2 * PRESSURE_GRADIENT_FAVOURABLE_HPA)), "favourable", raw,
                       ["synoptic_pressure"], PRESSURE_SUPPORT_KEYS["favourable"], False)
    if observed_gradient <= 0:
        return _result(0.1, "unfavourable", raw, ["synoptic_pressure"], PRESSURE_SUPPORT_KEYS["unfavourable"], False)
    return _result(0.5, "neutral", raw, ["synoptic_pressure"], PRESSURE_SUPPORT_KEYS["neutral"], False)


# --- Competing flow ---

COMPETING_FLOW_KEYS = {
    "clear": "competing_flow_clear",
    "easterly": "competing_flow_easterly",
    "northerly": "competing_flow_northerly",
    "misaligned_shear": "competing_flow_misaligned_shear",
    "missing": "competing_flow_missing_data",
}
EASTERLY_SECTOR = (45, 135)
NORTHERLY_SECTOR = (315, 360)
NORTHERLY_SECTOR_WRAP = (0, 45)
SHEAR_MISALIGN_DEG = 90.0


def competing_flow(surface_wind_dir_deg, summit_wind_dir_deg=None) -> dict:
    """surface_wind_dir_deg: whatever direction signal is available (may be
    forecast-derived or station-derived - the source is recorded honestly
    in 'sources', not assumed to be one or the other)."""
    if surface_wind_dir_deg is None:
        return _result(0.0, "missing", {}, [], COMPETING_FLOW_KEYS["missing"], True)

    raw = {"surface_wind_dir_deg": surface_wind_dir_deg, "summit_wind_dir_deg": summit_wind_dir_deg}
    if EASTERLY_SECTOR[0] <= surface_wind_dir_deg <= EASTERLY_SECTOR[1]:
        return _result(0.0, "easterly", raw, ["surface_wind"], COMPETING_FLOW_KEYS["easterly"], False)
    if surface_wind_dir_deg >= NORTHERLY_SECTOR[0] or surface_wind_dir_deg <= NORTHERLY_SECTOR_WRAP[1]:
        return _result(0.0, "northerly", raw, ["surface_wind"], COMPETING_FLOW_KEYS["northerly"], False)
    if summit_wind_dir_deg is not None:
        delta = abs(_angular_difference(surface_wind_dir_deg, summit_wind_dir_deg))
        if delta >= SHEAR_MISALIGN_DEG:
            return _result(0.2, "misaligned_shear", raw, ["surface_wind", "summit"],
                           COMPETING_FLOW_KEYS["misaligned_shear"], False)
    return _result(1.0, "clear", raw, ["surface_wind"], COMPETING_FLOW_KEYS["clear"], False)


# --- Data health ---

DATA_HEALTH_KEYS = {
    "healthy": "data_health_healthy",
    "degraded": "data_health_degraded",
    "critical": "data_health_critical",
}


def data_health(station_feats_by_id: dict) -> dict:
    """station_feats_by_id: {station_id: station_features output}. Always
    computable (never 'missing') - reports honestly on whatever coverage
    actually exists, which is itself the point of this diagnostic."""
    if not station_feats_by_id:
        return _result(0.0, "critical", {"n_stations": 0}, [], DATA_HEALTH_KEYS["critical"], False)

    coverages = [f.get("coverage", 0.0) for f in station_feats_by_id.values()]
    missing_count = sum(1 for f in station_feats_by_id.values() if f.get("missing_indicator") == 1.0)
    mean_coverage = sum(coverages) / len(coverages) if coverages else 0.0
    raw = {
        "n_stations": len(station_feats_by_id),
        "n_missing": missing_count,
        "mean_coverage": round(mean_coverage, 3),
        "per_station_coverage": {sid: f.get("coverage", 0.0) for sid, f in station_feats_by_id.items()},
    }
    if missing_count == len(station_feats_by_id) or mean_coverage < 0.3:
        return _result(0.0, "critical", raw, list(station_feats_by_id), DATA_HEALTH_KEYS["critical"], False)
    if missing_count > 0 or mean_coverage < 0.75:
        return _result(0.5, "degraded", raw, list(station_feats_by_id), DATA_HEALTH_KEYS["degraded"], False)
    return _result(1.0, "healthy", raw, list(station_feats_by_id), DATA_HEALTH_KEYS["healthy"], False)


# --- shared helpers ---

def _result(score, status, raw_values, sources, explanation_key, missing):
    return {
        "score": round(float(score), 3),
        "status": status,
        "raw_values": raw_values,
        "sources": sources,
        "explanation_key": explanation_key,
        "missing": missing,
    }


def _direction_from_vector(u, v):
    if u is None or v is None:
        return None
    # Inverse of station_features._wind_vector's meteorological convention.
    deg = (math.degrees(math.atan2(-u, -v))) % 360
    return round(deg, 1)


def _angular_difference(a, b):
    diff = (a - b + 180) % 360 - 180
    return diff
