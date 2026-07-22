"""
refresh_dashboard.py - regenerates docs/dashboard_data.json from what's on
disk, so the dashboard stays current as the live loop keeps learning.

Run nightly after verify_and_learn.py. No network calls: it works entirely
from the two local logs.

Data sources:
  - logs/backtest_dataset.jsonl   historical training samples (from backtest.py)
  - logs/predictions.jsonl        live predictions; the verified ones carry
                                  real observed outcomes, the unverified
                                  future ones are the upcoming forecast
  - weights.json                  the CURRENT weights (which keep evolving)

What it computes:
  - Rolling live accuracy: how the deployed model has actually performed on
    live, deduplicated, verified predictions - the number that matters most,
    since it reflects real forecast conditions (1-3 day lead), not backtest
    conditions (0-hour archive data).
  - The original backtest's "evaluation" (honest 2026 holdout metrics from
    a model that only ever saw 2024+2025), "deployment" (the model actually
    saved to weights.json, trained on everything), and "reproducibility"
    (seed/epochs) sections, all carried over UNCHANGED from the last
    backtest.py run - recomputing "evaluation" with today's weights.json
    would quietly turn the 2026 holdout into training data, since those
    weights keep learning from live outcomes that overlap the holdout
    period. Only backtest.py may write these sections.
  - A merged timeline: historical samples + live verified hours, all
    re-scored with current weights so the probability trace reflects the
    model you have today.
  - The upcoming forecast: the latest logged prediction for each future
    target hour (deduplicated the same way verify_and_learn.py dedupes for
    training - same hour gets forecast repeatedly across the 3-day rolling
    window) - this actually answers the dashboard's own headline question
    ("will it blow today?"), which nothing else here does.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from metrics import safe_div
from model import load_weights, score

BASE_DIR = os.path.dirname(__file__)
DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")
PREDICTIONS_PATH = os.path.join(BASE_DIR, "logs", "predictions.jsonl")
KITESAILING_OBSERVATIONS_PATH = os.path.join(BASE_DIR, "logs", "kitesailing_observations.jsonl")
KITESAILING_HEALTH_PATH = os.path.join(BASE_DIR, "logs", "kitesailing_ingestion_health.jsonl")
WATER_TEMPERATURE_PATH = os.path.join(BASE_DIR, "logs", "water_temperature_latest.json")
CURRENT_STATION_OBSERVATIONS_PATH = os.path.join(BASE_DIR, "logs", "current_station_observations.json")
METEOSWISS_LOCAL_FORECAST_PATH = os.path.join(BASE_DIR, "logs", "meteoswiss_local_forecast.json")
ZURICH_TZ = ZoneInfo("Europe/Zurich")
DASHBOARD_DATA_PATH = os.path.join(BASE_DIR, "docs", "dashboard_data.json")
ISSUANCE_LOG_PATH = os.path.join(BASE_DIR, "logs", "forecast_issuances.jsonl")
ZURICH_TZ = ZoneInfo("Europe/Zurich")

# 8-point compass, meteorological convention (direction the wind blows FROM).
COMPASS_POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass_direction(deg):
    """Converts raw wind-direction degrees (0-360) into an 8-point compass
    label for display. Returns None if deg is missing (older logged
    predictions, from before model_wind_dir_deg was added, don't have it) -
    the raw degrees themselves are never stored in dashboard_data.json,
    only this display label, since nothing downstream needs the raw value."""
    if deg is None:
        return None
    return COMPASS_POINTS[round(deg / 45) % 8]


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def dedupe_latest_per_hour(records):
    """Keep only the most recent prediction per target hour."""
    latest = {}
    for r in records:
        k = r["target_time"]
        if k not in latest or r.get("logged_at", "") > latest[k].get("logged_at", ""):
            latest[k] = r
    return list(latest.values())


def upcoming_forecast(predictions):
    """The latest logged prediction for each future target hour, soonest
    first - deduplicated the same way verify_and_learn.py dedupes for
    training, since the same hour gets forecast repeatedly across the
    3-day rolling window."""
    now_local = datetime.now(ZURICH_TZ)
    future = [
        p for p in predictions
        if datetime.fromisoformat(p["target_time"]).replace(tzinfo=ZURICH_TZ) > now_local
    ]
    upcoming = dedupe_latest_per_hour(future)
    upcoming.sort(key=lambda r: r["target_time"])
    return [
        {
            "target_time": r["target_time"],
            # Raw model probability (0-1), preserved as-is - this is the
            # actual number the dashboard's percentage display is derived
            # from (round(probability*100)), never a tier threshold.
            "probability": r["probability"],
            "tier": r["tier"],
            "model_wind_kt": r["model_wind_kt"],
            "model_gust_kt": r["model_gust_kt"],
            "model_wind_dir": compass_direction(r.get("model_wind_dir_deg")),
        }
        for r in upcoming
    ]


def live_metrics(verified):
    if not verified:
        return {"n": 0}
    tp = fp = tn = fn = 0
    for r in verified:
        predicted = r["tier"] in ("GOOD", "MARGINAL")
        actual = r.get("outcome") == 1.0
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and not actual:
            tn += 1
        else:
            fn += 1
    n = len(verified)
    positive_rate = (tp + fn) / n
    recall = safe_div(tp, tp + fn)
    specificity = safe_div(tn, tn + fp)
    balanced_accuracy = (recall + specificity) / 2 if (recall is not None and specificity is not None) else None
    return {
        "n": n,
        "accuracy": round((tp + tn) / n, 3),
        "balanced_accuracy": round(balanced_accuracy, 3) if balanced_accuracy is not None else None,
        "precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
        "recall": round(recall, 3) if recall is not None else None,
        "positive_rate": round(positive_rate, 3),
        "trivial_baseline_accuracy": round(max(positive_rate, 1 - positive_rate), 3),
        "true_positive": tp, "false_positive": fp,
        "true_negative": tn, "false_negative": fn,
    }


def monthly_breakdown(entries):
    """entries: list of {date, actual_kt, outcome}"""
    by_month = {}
    for e in entries:
        month = e["date"][:7]
        m = by_month.setdefault(month, {"n": 0, "sessions": 0, "sum_kt": 0.0})
        m["n"] += 1
        m["sessions"] += int(e["outcome"])
        m["sum_kt"] += e["actual_kt"]
    return {
        month: {
            "n": v["n"], "sessions": v["sessions"],
            "session_rate": round(v["sessions"] / v["n"], 3),
            "avg_wind_kt": round(v["sum_kt"] / v["n"], 1),
        }
        for month, v in sorted(by_month.items())
    }


def _latest_issuance():
    """Optional-fields support (section 10): returns the most recent
    logs/forecast_issuances.jsonl record, or None if the file doesn't
    exist yet (a fresh checkout, or a repo predating this feature) - every
    caller must handle None gracefully so the dashboard keeps working
    without it."""
    if not os.path.exists(ISSUANCE_LOG_PATH):
        return None
    latest = None
    with open(ISSUANCE_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                latest = json.loads(line)
    return latest


def optional_issuance_fields(issuance: dict) -> dict:
    """Builds the 5 optional dashboard fields from the latest issuance
    record. Returns {} for every field when no issuance data exists yet -
    docs/index.html must render identically whether these keys are present
    or entirely absent."""
    if not issuance:
        return {"daily_diagnostics": {}, "session_forecast": {}, "station_health": {},
                "model_agreement": {}, "data_provenance": {}}

    dates = list(issuance.get("session_forecast", {}).keys())
    diagnostics = issuance.get("diagnostics", {})
    return {
        "daily_diagnostics": {date: diagnostics for date in dates},
        "session_forecast": issuance.get("session_forecast", {}),
        "station_health": {
            "data_health": diagnostics.get("data_health", {}),
            "station_input_age_minutes": issuance.get("station_input_age", {}),
            "station_quality_flags": issuance.get("station_quality_flags", []),
        },
        "model_agreement": {
            date: sf.get("model_agreement") for date, sf in issuance.get("session_forecast", {}).items()
        },
        "data_provenance": {
            "issued_at": issuance.get("issued_at"),
            "commit_sha": issuance.get("commit_sha"),
            "model_version": issuance.get("model_version"),
            "feature_schema_version": issuance.get("feature_schema_version"),
            "calibration_version": issuance.get("calibration_version"),
            "raw_payload_checksums": issuance.get("raw_payload_checksums", {}),
        },
    }


def _local_date(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).astimezone(ZURICH_TZ).strftime("%Y-%m-%d")


def lake_station_health() -> dict:
    """Section 10: operational health of the kitesailing.ch lake sampler,
    built entirely from the local health log + observation log (no
    network) - degrades to a 'no_data' status when neither exists yet."""
    health_rows = read_jsonl(KITESAILING_HEALTH_PATH)
    observations = read_jsonl(KITESAILING_OBSERVATIONS_PATH)
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZURICH_TZ).strftime("%Y-%m-%d")

    if not health_rows and not observations:
        return {
            "last_attempt_at": None, "last_success_at": None, "last_observation_at": None,
            "age_minutes": None, "observations_today": 0, "successful_attempts_today": 0,
            "failed_attempts_today": 0, "coverage_12_18": 0.0,
            "expected_collection_count": 13, "actual_collection_count": 0,
            "consecutive_failures": 0, "failure_categories": {},
            "status": "no_data",
        }

    health_rows.sort(key=lambda r: r["attempted_at"])
    observations.sort(key=lambda o: o["observed_at"])

    last_attempt_at = health_rows[-1]["attempted_at"] if health_rows else None
    successful_rows = [r for r in health_rows if r.get("success")]
    last_success_at = successful_rows[-1]["attempted_at"] if successful_rows else None
    last_observation_at = observations[-1]["observed_at"] if observations else None
    age_minutes = None
    if last_observation_at:
        age_minutes = round((now - datetime.fromisoformat(last_observation_at)).total_seconds() / 60.0, 1)

    todays_rows = [r for r in health_rows if _local_date(r["attempted_at"]) == today]
    observations_today = sum(1 for o in observations if _local_date(o["observed_at"]) == today)
    successful_attempts_today = sum(1 for r in todays_rows if r.get("success"))
    failed_attempts_today = sum(1 for r in todays_rows if not r.get("success"))

    consecutive_failures = 0
    for r in reversed(health_rows):
        if r.get("success"):
            break
        consecutive_failures += 1

    # Coverage of this project's actually-scored 12:00-18:00 window today:
    # fraction of the 13 half-hourly slots (12:00, 12:30, ..., 18:00) with
    # an observation within 20 minutes of that slot.
    today_local = now.astimezone(ZURICH_TZ).date()
    slot_count = 13
    covered = 0
    todays_obs_dt = [datetime.fromisoformat(o["observed_at"]) for o in observations
                     if _local_date(o["observed_at"]) == today]
    for i in range(slot_count):
        slot_minutes = 12 * 60 + i * 30
        slot_local = datetime(today_local.year, today_local.month, today_local.day,
                               slot_minutes // 60, slot_minutes % 60, tzinfo=ZURICH_TZ)
        slot_utc = slot_local.astimezone(timezone.utc)
        if any(abs((obs_dt - slot_utc).total_seconds()) <= 20 * 60 for obs_dt in todays_obs_dt):
            covered += 1
    coverage_12_18 = round(covered / slot_count, 3)

    if consecutive_failures >= 3 or (age_minutes is not None and age_minutes > 180):
        status = "critical"
    elif consecutive_failures > 0 or (age_minutes is not None and age_minutes > 60):
        status = "degraded"
    else:
        status = "healthy"

    # Per-category tally of why an attempt failed (kitesailing_weather.py's
    # attempt_reading() already records one of these per failed row) - a
    # summary count alone can't distinguish "one dominant, explainable
    # cause" from "many unrelated issues", same rationale as
    # data_quality.flag_counts().
    failure_categories = {}
    for r in health_rows:
        cat = r.get("failure_category")
        if cat:
            failure_categories[cat] = failure_categories.get(cat, 0) + 1

    return {
        "last_attempt_at": last_attempt_at, "last_success_at": last_success_at,
        "last_observation_at": last_observation_at, "age_minutes": age_minutes,
        "observations_today": observations_today,
        "successful_attempts_today": successful_attempts_today,
        "failed_attempts_today": failed_attempts_today,
        "coverage_12_18": coverage_12_18,
        "expected_collection_count": slot_count,
        "actual_collection_count": covered,
        "consecutive_failures": consecutive_failures,
        "failure_categories": failure_categories,
        "status": status,
    }


def summit_station_health(issuance: dict) -> dict:
    """Section 10: per-summit-station health (today only cov, once
    enabled) from the latest forecast issuance record - {} when no
    issuance exists yet, or when no summit-role station reported data.
    Includes provenance (source assets, reporting delay) alongside the
    physical readings, so a human can see not just "what" but "from where"."""
    if not issuance:
        return {}
    station_inputs = issuance.get("station_inputs", {})
    station_input_age = issuance.get("station_input_age", {})
    diagnostics = issuance.get("diagnostics", {})
    summit = diagnostics.get("summit_support", {})
    sid = summit.get("source_station")
    if not sid or sid not in station_inputs:
        return {}
    feats = station_inputs[sid]
    return {
        sid: {
            "last_observation_at": summit.get("observed_at"),
            "age_minutes": station_input_age.get(sid),
            "coverage": feats.get("coverage"),
            "quality_flags": [summit["explanation_key"]] if summit.get("status") == "missing" else [],
            "wind_speed": feats.get("latest_wind_speed"),
            "gust": feats.get("max_morning_gust"),
            "direction": summit.get("raw_values", {}).get("wind_direction_deg"),
            "temperature": feats.get("temperature_latest"),
            "source_assets": issuance.get("station_source_assets", {}).get(sid, []),
            "reporting_delay_minutes": issuance.get("station_reporting_delay_minutes", {}).get(sid),
        }
    }


def station_nowcast_status(issuance: dict) -> dict:
    """Section 10 (Part 11): whether the latest forecast issuance actually
    used station_nowcast.py's bounded live snapshot, or fell back to the
    historical archive (a local research/dev run only - see
    forecast_and_log.py's _station_records_for_inputs) - derived directly
    from the same os.path.exists() check made at issuance time, never
    guessed after the fact."""
    if not issuance or "station_nowcast_snapshot_used" not in issuance:
        return {"snapshot_used": None, "issued_at": None}
    return {
        "snapshot_used": issuance["station_nowcast_snapshot_used"],
        "issued_at": issuance.get("issued_at"),
    }


def unmatched_predictions_count(predictions: list, now: datetime = None) -> int:
    """Section 10: predictions old enough that verify_and_learn.py should
    have checked them (past its own MIN_AGE_HOURS=20) but that are still
    unverified - meaning neither the kitesailing.ch lake reading nor the
    Samedan fallback ever produced a match for that hour (verify_and_learn.py
    leaves these alone rather than fabricating a label - see its own
    `else: continue` branch). A non-zero count is a real, visible gap in
    ground-truth coverage, not a bug to hide."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=20)
    count = 0
    for r in predictions:
        if r.get("verified"):
            continue
        target_utc = datetime.fromisoformat(r["target_time"]).replace(tzinfo=ZURICH_TZ).astimezone(timezone.utc)
        if target_utc < cutoff:
            count += 1
    return count


def verification_sources(verified: list, predictions: list = None) -> dict:
    """Section 10: how many verified predictions used the real lake
    reading vs. the SIA reference vs. the legacy Samedan fallback - never
    blended into one accuracy number without these counts alongside it
    (see docs/DATA_ARCHITECTURE.md). Legacy samedan_fallback rows (verified
    before ground-truth policy v2) are preserved, never rewritten, and
    counted separately here. Also reports how many mature predictions
    matched NO source."""
    silvaplana_lake_count = sum(1 for r in verified if r.get("ground_truth_source") == "kitesailing")
    sia_reference_count = sum(1 for r in verified if r.get("ground_truth_source") == "sia_reference")
    samedan_fallback_count = sum(1 for r in verified if r.get("ground_truth_source") == "samedan_fallback")
    total = silvaplana_lake_count + sia_reference_count + samedan_fallback_count
    return {
        "silvaplana_lake_count": silvaplana_lake_count,
        "sia_reference_count": sia_reference_count,
        "legacy_samedan_fallback_count": samedan_fallback_count,
        # kept under its old key too so docs/index.html keeps rendering
        # until it's updated to the new one
        "samedan_fallback_count": samedan_fallback_count,
        "lake_coverage_pct": round(silvaplana_lake_count / total, 3) if total else None,
        "sia_coverage_pct": round(sia_reference_count / total, 3) if total else None,
        "unmatched_count": unmatched_predictions_count(predictions or []),
    }


def _ground_truth_policy() -> dict:
    path = os.path.join(BASE_DIR, "config", "ground_truth_policy.json")
    try:
        with open(path) as f:
            raw = json.load(f)
        return {"policy_version": raw.get("policy_version"),
                "priority": raw.get("priority"),
                "samedan_reference_allowed": raw.get("allow_samedan_fallback"),
                "sia_calibration_status": raw.get("sia_calibration_status"),
                "relationship_classification": raw.get("relationship_classification")}
    except (OSError, json.JSONDecodeError):
        return {}


def reference_stations() -> dict:
    """"Historical reference stations" dashboard section: coverage,
    latest observation, and role for every ground-truth-relevant station,
    read from the committed coverage manifest (no network). Empty dict
    when the manifest doesn't exist yet."""
    manifest_path = os.path.join(BASE_DIR, "logs", "historical", "manifests", "stations.json")
    try:
        with open(manifest_path) as f:
            manifest = json.load(f).get("stations", {})
    except (OSError, json.JSONDecodeError):
        return {}
    wanted = {
        "windsurfcenter_silvaplana": "intended primary target (no data acquired yet)",
        "sia": "principal near-lake historical reference",
        "sils": "distinct user-provided lake-adjacent sample",
        "sam": "contextual down-valley station (no longer the default label)",
        "cov": "summit / competing-flow predictor",
    }
    out = {}
    for sid, role in wanted.items():
        m = manifest.get(sid)
        if m is None:
            continue
        out[sid] = {
            "name": m.get("name"), "role": role,
            "enabled": m.get("enabled"), "verification": m.get("verification"),
            "n_records": m.get("n_records"), "data_start": m.get("data_start"),
            "data_end": m.get("data_end"),
            "n_records_10min": m.get("n_records_10min"),
        }
    return out


def latest_station_observation(station_id: str, station_name: str) -> dict:
    """Newest valid reading from station_nowcast's live MeteoSwiss snapshot."""
    try:
        with open(CURRENT_STATION_OBSERVATIONS_PATH) as handle:
            snapshot = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    station_entry = snapshot.get("stations", {}).get(station_id, {})
    display = station_entry.get("latest_display_observation")
    observations = station_entry.get("observations", [])
    if display and display.get("wind_speed_ms") is not None:
        valid = [display]
    else:
        valid = [row for row in observations if row.get("wind_speed_ms") is not None]
    if not valid:
        return {}
    vals = max(valid, key=lambda row: row.get("timestamp_utc", ""))
    out = {
        "observed_at": vals.get("timestamp_utc"),
        "observed_at_local": vals.get("timestamp_local"),
        "wind_kt": round(vals["wind_speed_ms"] * 1.943844, 1),
        "station": station_name,
    }
    metadata = station_entry.get("display_observation_metadata") or {}
    if display:
        out["quality_status"] = metadata.get("quality_status", "provisional_live")
        out["resolution_minutes"] = metadata.get("resolution_minutes", 10)
    else:
        out["quality_status"] = "quality_controlled_hourly"
        out["resolution_minutes"] = 60
    if vals.get("wind_gust_ms") is not None:
        out["gust_kt"] = round(vals["wind_gust_ms"] * 1.943844, 1)
    if vals.get("wind_direction_deg") is not None:
        out["wind_dir"] = compass_direction(vals["wind_direction_deg"])
    if vals.get("temperature_c") is not None:
        out["temp_c"] = vals["temperature_c"]
    return out


def latest_sia_observation() -> dict:
    """Newest real SIA reading from the committed raw cache (no network) -
    refreshed whenever a backtest/sync run recommits generic_sia.json.
    Empty dict when the cache doesn't exist yet."""
    live = latest_station_observation("sia", "Segl-Maria (SIA)")
    if live:
        return live
    path = os.path.join(BASE_DIR, "logs", "raw_cache", "generic_sia.json")
    try:
        with open(path) as f:
            cached = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    ts = max(cached)
    vals = cached[ts]
    if vals.get("wind_speed_ms") is None:
        return {}
    out = {
        "observed_at": ts,
        "wind_kt": round(vals["wind_speed_ms"] * 1.943844, 1),
        "station": "Segl-Maria (SIA)",
    }
    if vals.get("wind_gust_ms") is not None:
        out["gust_kt"] = round(vals["wind_gust_ms"] * 1.943844, 1)
    if vals.get("wind_direction_deg") is not None:
        out["wind_dir"] = compass_direction(vals["wind_direction_deg"])
    if vals.get("temperature_c") is not None:
        out["temp_c"] = vals["temperature_c"]
    return out


def latest_samedan_observation() -> dict:
    return latest_station_observation("sam", "Samedan (SAM)")


def meteoswiss_local_forecast() -> dict:
    """Last successfully downloaded official forecast; never fetches here."""
    try:
        with open(METEOSWISS_LOCAL_FORECAST_PATH) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def station_agreement() -> dict:
    """Latest SIA/lake calibration summary - shown only with its maturity
    label attached, and only the small headline fields (the full report
    stays in logs/historical/reports/). Empty dict when no report exists."""
    import glob
    reports = sorted(glob.glob(os.path.join(
        BASE_DIR, "logs", "historical", "reports", "station_calibration_*_sia_*.json")))
    if not reports:
        return {}
    try:
        with open(reports[-1]) as f:
            r = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    zero = r.get("zero_lag", {})
    n_days = r.get("independent_overlapping_days", 0)
    out = {
        "source_a": r.get("source_a"), "source_b": r.get("source_b"),
        "independent_overlapping_days": n_days,
        "calibration_maturity": r.get("calibration_maturity"),
        "classification": r.get("classification"),
        "generated_at": r.get("generated_at"),
        "n_pairs": zero.get("n", 0),
    }
    # Correlation/bias figures from a too-small overlap are noise dressed
    # as numbers - suppressed below the minimum reporting gate, only the
    # honest maturity state is shown.
    if n_days >= 14:
        out.update({
            "pearson": zero.get("pearson"), "mae_ms": zero.get("mae_ms"),
            "bias_sia_minus_lake_ms": zero.get("bias_sia_minus_lake_ms"),
            "best_lag_hours": r.get("best_lag_hours"),
        })
    return out


def lake_water_temperature() -> dict:
    """Latest lake-water estimate, never the Kitesailing air temperature."""
    empty = {
        "temp_c": None, "retrieved_at": None, "estimated": True,
        "source_url": "https://www.wassertemperatur.org/seen/schweiz/silvaplanersee/",
    }
    try:
        with open(WATER_TEMPERATURE_PATH) as handle:
            reading = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty
    temp_c = reading.get("temp_c")
    if not isinstance(temp_c, (int, float)):
        return empty
    return {
        "temp_c": float(temp_c),
        "retrieved_at": reading.get("retrieved_at"),
        "estimated": True,
        "source_url": reading.get("source_url") or empty["source_url"],
        "source_note": reading.get("source_note"),
    }


def main():
    weights = load_weights()
    backtest_samples = read_jsonl(DATASET_PATH)
    predictions = read_jsonl(PREDICTIONS_PATH)

    verified = dedupe_latest_per_hour([p for p in predictions if p.get("verified")])
    verified.sort(key=lambda r: r["target_time"])

    # Carry over the frozen evaluation/deployment/reproducibility sections
    # from the last backtest.py run untouched (they describe a fixed
    # experiment against a model that has since been superseded by
    # whatever weights.json now holds via verify_and_learn.py's online
    # updates - recomputing "evaluation" with CURRENT weights would quietly
    # turn the 2026 holdout into training data, since those weights have
    # continued learning from live outcomes that overlap the holdout
    # period).
    evaluation = None
    deployment = None
    reproducibility = None
    if os.path.exists(DASHBOARD_DATA_PATH):
        try:
            with open(DASHBOARD_DATA_PATH) as f:
                prev = json.load(f)
            if not prev.get("is_sample_data"):
                evaluation = prev.get("evaluation")
                deployment = prev.get("deployment")
                reproducibility = prev.get("reproducibility")
        except (json.JSONDecodeError, OSError):
            pass

    # Unified entries for timeline + monthly stats, re-scored with current weights
    entries = []
    for s in backtest_samples:
        entries.append({
            "date": s["date"], "actual_kt": s["actual_wind_kt"],
            "outcome": s["outcome"],
            "probability": round(score(s["features"], weights), 3),
            "source": "backtest", "year": s.get("year"),
        })
    for r in verified:
        entries.append({
            "date": r["target_time"], "actual_kt": r["actual_wind_kt"],
            "outcome": r["outcome"],
            "probability": round(score(r["features"], weights), 3),
            "source": "live", "year": int(r["target_time"][:4]),
        })
    entries.sort(key=lambda e: e["date"])

    per_year = {}
    for e in entries:
        per_year[str(e["year"])] = per_year.get(str(e["year"]), 0) + 1

    latest_issuance = _latest_issuance()

    dashboard_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_samples": len(entries),
        "samples_per_year": per_year,
        "reproducibility": reproducibility or {},
        "evaluation": evaluation or {"n_holdout_samples": 0},
        "deployment": deployment or {},
        "live_metrics": live_metrics(verified),
        "upcoming_forecast": upcoming_forecast(predictions),
        "final_weights": weights,
        "monthly_breakdown": monthly_breakdown(entries),
        "timeline": [
            {"date": e["date"], "actual_kt": e["actual_kt"], "outcome": e["outcome"],
             "probability": e["probability"], "year": e["year"], "source": e["source"]}
            for e in entries
        ],
        # Optional fields (section 10) - {} when no issuance record exists
        # yet (fresh checkout / older repo state). docs/index.html must
        # keep working whether these keys are present or absent/empty.
        **optional_issuance_fields(latest_issuance),
        "lake_station_health": lake_station_health(),
        "summit_station_health": summit_station_health(latest_issuance),
        "station_nowcast_status": station_nowcast_status(latest_issuance),
        "verification_sources": verification_sources(verified, predictions),
        "lake_water_temperature": lake_water_temperature(),
        "reference_stations": reference_stations(),
        "ground_truth_policy": _ground_truth_policy(),
        "station_agreement": station_agreement(),
        "latest_sia_observation": latest_sia_observation(),
        "latest_samedan_observation": latest_samedan_observation(),
        "meteoswiss_local_forecast": meteoswiss_local_forecast(),
    }

    os.makedirs(os.path.dirname(DASHBOARD_DATA_PATH), exist_ok=True)
    with open(DASHBOARD_DATA_PATH, "w") as f:
        json.dump(dashboard_data, f, indent=2)

    lm = dashboard_data["live_metrics"]
    print(f"Dashboard refreshed: {len(entries)} entries "
          f"({len(backtest_samples)} backtest + {len(verified)} live verified). "
          f"Live accuracy so far: {lm.get('accuracy', '—')} on n={lm['n']}. "
          f"Upcoming forecast: {len(dashboard_data['upcoming_forecast'])} hours.")


if __name__ == "__main__":
    sys.exit(main())
