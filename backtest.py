"""
backtest.py - run once (or whenever you want to retrain from scratch).

Instead of waiting weeks for the live model to accumulate outcomes, this
builds the training set directly from history:

  - Weather: Open-Meteo's Historical Forecast API (archive from ~2021),
    same variables/format as the live forecast - not a coarser reanalysis,
    so training data matches what the live model will actually see. Note
    this is still 0-hour archive data, not a genuine multi-day-ahead
    forecast with real lead-time error - see the "Known limitations"
    section of README.md.
  - Ground truth: MeteoSwiss's real Segl-Maria (SIA) station observations,
    selected through the same SIA-first policy (`ground_truth.select_label`,
    config/ground_truth_policy.json v2) the live loop uses - SIA's genuine
    hourly archive (108k+ records, 2014->present, fetched/cached via
    historical_cache.get_sia_archive) fully covers every backtest season.
    The kitesailing.ch lake reading, the live loop's top-priority source,
    has no historical archive yet, so historically SIA (the policy's
    principal reference, ~4km from the lake at the same elevation band) is
    the effective label source; an hour with no acceptable SIA observation
    is EXCLUDED, never silently Samedan-labeled. Both this script and
    verify_and_learn.py label on the shared ground_truth.SIA_REFERENCE_KT
    criterion, closing the historical-vs-live labeling-criterion mismatch
    that existed while this script still labeled on Samedan/SAM_PROXY_KT
    (see CLAUDE.md's "Ground truth and retraining gate", and the
    model_comparison_sia.py report that justified this switch: consistent
    chronological improvement across 2025/2026 folds). Samedan observations
    are still fetched - as a model FEATURE (samedan_morning_score) and as
    per-row context (samedan_wind_kt/samedan_gust_kt) - just never as the
    label.

Seasons covered: May-October, for 2024, 2025, and 2026 (up to today) -
i.e. wingfoil season only, matching how you'd actually use this.

EVALUATION vs. DEPLOYMENT - the core fix this module implements
-----------------------------------------------------------------
Earlier versions of this script loaded the ALREADY-TRAINED weights.json,
reset only the bias, then trained on 2024+2025 and "evaluated" on 2026.
Since weights.json was itself the product of a PREVIOUS retrain that had
already folded 2026 into training, that "holdout" evaluation was
contaminated by prior exposure to the very data it was supposed to be
untouched by - the per-feature weights already knew things about 2026.

This is fixed by training two entirely separate models, each built from
model.new_weights() (never from weights.json):

  1. An EVALUATION model, trained ONLY on 2024+2025 via model.train_epochs,
     with its own thresholds calibrated ONLY on 2024+2025. It is then
     scored ONCE against the untouched 2026 holdout and never touched
     again - not reused, not further trained. Those holdout numbers are
     the only honest answer to "how would this have done on data it never
     saw."
  2. A DEPLOYMENT model, ALSO built fresh from model.new_weights() (not by
     continuing to train the evaluation model further - that would still
     leave 2026 partially represented in weights that started life having
     already learned from 2024+2025 in a specific order/seed tied to the
     evaluation run), trained on 2024+2025+2026, with thresholds
     calibrated on all three years. This is the only model saved to
     weights.json.

The evaluation model is discarded after producing its metrics - it must
never be saved to weights.json or reused for anything else.

Steps:
  1. Fetch weather + SAM obs for each season - via historical_cache.py,
     which persists the raw pulls under logs/raw_cache/ so a closed season
     (2024, 2025) never needs re-fetching and the open season (2026) only
     refetches once per calendar day.
  2. Build one labeled sample per afternoon hour (WINDOW_START_HOUR to
     WINDOW_END_HOUR) per day.
  3. Chronological split: 2024+2025 for evaluation-training, 2026 as the
     untouched holdout.
  4. Train the evaluation model, calibrate its thresholds on 2024+2025
     only, score it once against 2026 (hourly full+prime window, session
     full+prime window, operational threshold performance, and a feature
     ablation comparison - see metrics.py/ablation.py).
  5. Train a separate, fresh deployment model on all three years, calibrate
     its thresholds on all three years, save it to weights.json.
  6. Write logs/backtest_dataset.jsonl (full sample set) and
     docs/dashboard_data.json (evaluation + deployment summary for the
     dashboard).

NOTE on the window: WINDOW_START_HOUR was briefly narrowed to 15 (from 12)
on the theory that hours 12-14 were just noise. The 2026-07-16 backtest
disproved that empirically - full-model AUC on the 2026 holdout DROPPED
from 0.750 (12-18h) to 0.683 (15-18h), because hours 12-14 have a low
positive rate (~25-49%) and were easy, highly-separable true negatives
that boosted overall discriminative power. Restricting to 15-18h left a
smaller, more homogeneous, harder-to-classify population. Reverted back
to 12-18h - don't re-narrow this without backtest evidence it actually
helps.

The "prime window" reports below are a diagnostic SLICE of the same
12-18h-trained model's holdout predictions, not a different training
window - they do not change WINDOW_START_HOUR/WINDOW_END_HOUR, which
remain what forecast_and_log.py schedules against. It was originally
15:00-18:00, then changed to 14:00-18:00 on 2026-07-16 (a separate,
later change from the WINDOW_START_HOUR revert described above) - see
PRIME_WINDOW_START_HOUR/PRIME_WINDOW_END_HOUR.
"""

import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ablation import run_ablation
from features import engineer_features
import ground_truth
from historical_cache import get_season_raw, get_samedan_archive, get_pressure_archive, get_sia_archive
from meteoswiss import LUGANO_STATION, ZURICH_STATION
from metrics import (
    build_session_samples,
    calibrate_good_threshold,
    calibrate_marginal_threshold,
    classification_report,
)
from model import DEFAULT_TRAIN_SEED, new_weights, save_weights, score, train_epochs, validate_schema

WINDOW_START_HOUR = 12  # reverted from 15 - see the docstring note above. Live forecast window - do not change without backtest evidence.
WINDOW_END_HOUR = 18

# Diagnostic-only slice of the same holdout predictions (see docstring) -
# NOT a second training window, purely a reporting split.
PRIME_WINDOW_START_HOUR = 14
PRIME_WINDOW_END_HOUR = 18

MARGINAL_KT = 10
EPOCHS = 40

ZURICH_TZ = ZoneInfo("Europe/Zurich")

SEASONS = [
    ("2024-05-01", "2024-10-31", 2024),
    ("2025-05-01", "2025-10-31", 2025),
    ("2026-05-01", datetime.now().strftime("%Y-%m-%d"), 2026),  # up to today
]

BASE_DIR = os.path.dirname(__file__)
DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")
DASHBOARD_DATA_PATH = os.path.join(BASE_DIR, "docs", "dashboard_data.json")


def kt(kmh: float) -> float:
    return kmh / 1.852


MS_TO_KT = 1.943844


def select_backtest_label(dt_utc, sia_obs, policy):
    """SIA-first label for one target hour, selected through the same
    ground_truth.select_label policy machinery the live loop uses. Returns
    the selected canonical observation, or None when no acceptable
    observation exists - the hour is then EXCLUDED, never proxy-labeled."""
    vals = sia_obs.get(dt_utc)
    if vals is None or vals.get("wind_speed_ms") is None:
        return None
    candidate = ground_truth.canonical_observation(
        timestamp_utc=dt_utc.isoformat(),
        source="sia", station_id="sia",
        wind_speed_ms=vals.get("wind_speed_ms"),
        wind_gust_ms=vals.get("wind_gust_ms"),
        wind_direction_deg=vals.get("wind_direction_deg"),
        temperature_c=vals.get("temperature_c"),
        provenance={"source_asset": "meteoswiss:sia:hourly_archive (historical_cache.get_sia_archive)"},
        validation_status="source_validated",
    )
    return ground_truth.select_label([candidate], policy)


def build_samples_for_season(start_date, end_date, year, sia_obs, policy,
                              sam_obs, lugano_obs, zurich_obs, is_closed):
    raw = get_season_raw(start_date, end_date, year, is_closed)
    # fetch_raw_historical doesn't fetch these itself (would re-download
    # the whole multi-year archives per season) - inject the copies we
    # already have, so engineer_features' samedan_morning_score /
    # pressure_nowcast_score can look them up.
    raw["samedan_obs"] = sam_obs
    raw["lugano_obs"] = lugano_obs
    raw["zurich_obs"] = zurich_obs
    times = raw["silvaplana"]["time"]

    samples = []
    excluded_no_label = 0
    for idx, t in enumerate(times):
        dt_local = datetime.fromisoformat(t).replace(tzinfo=ZURICH_TZ)
        if not (WINDOW_START_HOUR <= dt_local.hour <= WINDOW_END_HOUR):
            continue

        dt_utc = dt_local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        label = select_backtest_label(dt_utc, sia_obs, policy)
        if label is None:
            # no acceptable SIA observation - excluded, never Samedan-labeled
            excluded_no_label += 1
            continue

        feats = engineer_features(raw, idx)
        actual_kt = label["wind_speed_ms"] * MS_TO_KT
        actual_gust_kt = (label["wind_gust_ms"] or 0.0) * MS_TO_KT
        outcome = 1.0 if actual_kt >= ground_truth.SIA_REFERENCE_KT else 0.0

        sample = {
            "date": t,
            "year": year,
            "features": feats,
            "model_wind_kt": round(kt(raw["silvaplana"]["wind_speed_10m"][idx]), 1),
            "actual_wind_kt": round(actual_kt, 1),
            "actual_gust_kt": round(actual_gust_kt, 1),
            "outcome": outcome,
            "ground_truth_source": label["source"],
            "label_provenance": {
                "source": label["source"], "station_id": label["station_id"],
                "confidence": label.get("confidence"),
                "policy_version": label.get("policy_version"),
            },
        }
        # Samedan context, preserved on every row regardless of label source
        # (the long-running correlation-study convention from the live loop).
        sam = sam_obs.get(dt_utc)
        if sam is not None:
            sample["samedan_wind_kt"] = round(kt(sam["speed_kmh"]), 1)
            sample["samedan_gust_kt"] = round(kt(sam["gust_kmh"]), 1)
        samples.append(sample)
    print(f"  -> {len(samples)} labeled hours ({excluded_no_label} excluded: no acceptable ground truth)")
    return samples


def _hour_of(sample):
    return int(sample["date"][11:13])


def _filter_hours(samples, start_hour, end_hour):
    return [s for s in samples if start_hour <= _hour_of(s) <= end_hour]


def hourly_reports(weights, samples, thresholds):
    """Threshold-swept classification reports for one flat list of hourly
    samples: the plain 0.5 cutoff (an unbiased read of the raw model) plus
    the two OPERATIONAL cutoffs actually used to tier live alerts - a 0.5
    cutoff alone does not tell you how the MARGINAL/GOOD tiers perform."""
    labels = [s["outcome"] for s in samples]
    probs = [score(s["features"], weights) for s in samples]
    return {
        "n": len(samples),
        "cutoff_0.5": classification_report(labels, probs, threshold=0.5),
        "cutoff_marginal": classification_report(labels, probs, threshold=thresholds["marginal"]),
        "cutoff_good": classification_report(labels, probs, threshold=thresholds["good"]),
    }


def session_reports(weights, samples, thresholds, start_hour, end_hour):
    """Same three cutoffs as hourly_reports, but aggregated to one row per
    local calendar day first (see metrics.build_session_samples for the
    max-probability / any-positive-hour aggregation rule)."""
    dates = [s["date"] for s in samples]
    outcomes = [s["outcome"] for s in samples]
    probs = [score(s["features"], weights) for s in samples]
    session_outcomes, session_probs, days = build_session_samples(
        dates, outcomes, probs, start_hour, end_hour)
    return {
        "n_days": len(days),
        "cutoff_0.5": classification_report(session_outcomes, session_probs, threshold=0.5),
        "cutoff_marginal": classification_report(session_outcomes, session_probs, threshold=thresholds["marginal"]),
        "cutoff_good": classification_report(session_outcomes, session_probs, threshold=thresholds["good"]),
    }


def calibrate_thresholds(weights, samples):
    labels = [s["outcome"] for s in samples]
    probs = [score(s["features"], weights) for s in samples]
    marginal = calibrate_marginal_threshold(labels, probs)
    good = calibrate_good_threshold(labels, probs, marginal_threshold=marginal)
    return {"good": round(good, 2), "marginal": round(marginal, 2)}


def monthly_breakdown(samples):
    by_month = {}
    for s in samples:
        month = s["date"][:7]  # YYYY-MM
        m = by_month.setdefault(month, {"n": 0, "sessions": 0, "avg_kt_sum": 0.0})
        m["n"] += 1
        m["sessions"] += int(s["outcome"])
        m["avg_kt_sum"] += s["actual_wind_kt"]
    return {
        month: {
            "n": v["n"], "sessions": v["sessions"],
            "session_rate": round(v["sessions"] / v["n"], 3),
            "avg_wind_kt": round(v["avg_kt_sum"] / v["n"], 1),
        }
        for month, v in sorted(by_month.items())
    }


def main():
    print("Fetching MeteoSwiss Segl-Maria (SIA) ground truth (historical + recent)...")
    sia_obs = get_sia_archive()
    print(f"  -> {len(sia_obs)} hourly observations available")
    policy = ground_truth.load_policy()
    print(f"  labeling policy: v{policy.policy_version}, samedan_fallback={policy.allow_samedan_fallback}")

    print("Fetching MeteoSwiss Samedan (feature + per-row context, no longer the label)...")
    sam_obs = get_samedan_archive()
    print(f"  -> {len(sam_obs)} hourly observations available")

    print("Fetching MeteoSwiss Lugano/Zurich pressure (for pressure_nowcast_score)...")
    lugano_obs = get_pressure_archive(LUGANO_STATION)
    zurich_obs = get_pressure_archive(ZURICH_STATION)
    print(f"  -> {len(lugano_obs)} Lugano / {len(zurich_obs)} Zurich hourly observations available")

    today_str = datetime.now().strftime("%Y-%m-%d")
    all_samples = {}
    for start, end, year in SEASONS:
        if end < start:
            print(f"Skipping {year}: season hasn't started yet ({start} > {end}).")
            all_samples[year] = []
            continue
        is_closed = end != today_str
        all_samples[year] = build_samples_for_season(
            start, end, year, sia_obs, policy, sam_obs, lugano_obs, zurich_obs, is_closed)

    os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DASHBOARD_DATA_PATH), exist_ok=True)

    with open(DATASET_PATH, "w") as f:
        for year_samples in all_samples.values():
            for s in year_samples:
                f.write(json.dumps(s) + "\n")

    train_set = all_samples[2024] + all_samples[2025]
    holdout_set = all_samples[2026]
    all_flat = train_set + holdout_set

    # ---- 1. EVALUATION model: fresh, trained ONLY on 2024+2025 ----
    print(f"\n[evaluation] Training a FRESH model on {len(train_set)} samples "
          f"(2024+2025 only), {EPOCHS} epochs, seed={DEFAULT_TRAIN_SEED}...")
    eval_weights = new_weights()
    validate_schema(eval_weights)
    eval_weights = train_epochs(eval_weights, train_set, epochs=EPOCHS, seed=DEFAULT_TRAIN_SEED)

    eval_thresholds = calibrate_thresholds(eval_weights, train_set)
    print(f"[evaluation] Thresholds calibrated on 2024+2025 only -> "
          f"MARGINAL >= {eval_thresholds['marginal']}, GOOD >= {eval_thresholds['good']}")

    print(f"[evaluation] Scoring the UNTOUCHED {len(holdout_set)}-sample 2026 holdout "
          "(this model has never seen 2026 in any form)...")
    prime_holdout = _filter_hours(holdout_set, PRIME_WINDOW_START_HOUR, PRIME_WINDOW_END_HOUR)

    evaluation = {
        "description": (
            "Honest 2026 holdout metrics from a model built via model.new_weights() "
            "and trained ONLY on 2024+2025. This model is discarded after producing "
            "these numbers - it is never saved to weights.json and never trained "
            "further on 2026."
        ),
        # Written ONLY here, by backtest.py - refresh_dashboard.py carries this
        # timestamp forward unchanged along with the rest of this section, so
        # the dashboard can show "this frozen evaluation was generated on X"
        # distinctly from the top-level generated_at (which changes on every
        # refresh_dashboard.py run and reflects the live/deployment data, not
        # this frozen holdout experiment).
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trained_on_years": [2024, 2025],
        "holdout_years": [2026],
        "n_training_samples": eval_weights["trained_samples"],
        "n_holdout_samples": len(holdout_set),
        "thresholds": eval_thresholds,
        "prime_window_hours": [PRIME_WINDOW_START_HOUR, PRIME_WINDOW_END_HOUR],
        "full_window_hours": [WINDOW_START_HOUR, WINDOW_END_HOUR],
        "hourly": {
            "full_window": hourly_reports(eval_weights, holdout_set, eval_thresholds),
            "prime_window": hourly_reports(eval_weights, prime_holdout, eval_thresholds),
        },
        "session": {
            "full_window": session_reports(
                eval_weights, holdout_set, eval_thresholds, WINDOW_START_HOUR, WINDOW_END_HOUR),
            "prime_window": session_reports(
                eval_weights, holdout_set, eval_thresholds, PRIME_WINDOW_START_HOUR, PRIME_WINDOW_END_HOUR),
        },
        "ablation": {
            "description": (
                "DIAGNOSTIC comparison only, not model selection - every group below is "
                "scored once against the same 2026 holdout used above, each trained fresh "
                "on 2024+2025. Picking the best-looking row here and reporting its holdout "
                "number as 'the' model's accuracy would itself be a form of holdout leakage. "
                "The deployed model (see the top-level 'deployment' section) always uses the "
                "full feature set, trained independently of this comparison."
            ),
            "groups": run_ablation(train_set, holdout_set, epochs=EPOCHS, seed=DEFAULT_TRAIN_SEED),
        },
    }

    full_acc = evaluation["hourly"]["full_window"]["cutoff_0.5"].get("accuracy")
    baseline = evaluation["hourly"]["full_window"]["cutoff_0.5"].get("majority_baseline_accuracy")
    print(f"[evaluation] Full-window hourly holdout accuracy @0.5: {full_acc} "
          f"(majority-class baseline: {baseline})")

    # ---- 2. DEPLOYMENT model: fresh, trained on 2024+2025+2026 ----
    print(f"\n[deployment] Training a SEPARATE fresh model on all {len(all_flat)} samples "
          f"(2024+2025+2026), {EPOCHS} epochs, seed={DEFAULT_TRAIN_SEED}...")
    deploy_weights = new_weights()
    validate_schema(deploy_weights)
    deploy_weights = train_epochs(deploy_weights, all_flat, epochs=EPOCHS, seed=DEFAULT_TRAIN_SEED)

    deploy_thresholds = calibrate_thresholds(deploy_weights, all_flat)
    deploy_weights["tier_thresholds"] = deploy_thresholds
    print(f"[deployment] Thresholds calibrated on all data -> "
          f"MARGINAL >= {deploy_thresholds['marginal']}, GOOD >= {deploy_thresholds['good']}")

    save_weights(deploy_weights)

    deployment = {
        "description": (
            "This is the model saved to weights.json and used live. Trained fresh "
            "(via model.new_weights(), never continued from the evaluation model above) "
            "on all three years, so it has seen 2026 - its own performance on 2026 is "
            "NOT a valid holdout number and is intentionally not reported here; see "
            "'evaluation' for the honest holdout metrics."
        ),
        "trained_on_years": [2024, 2025, 2026],
        "n_training_samples": deploy_weights["trained_samples"],
        "thresholds": deploy_thresholds,
    }

    # ---- Dashboard data ----
    dashboard_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_samples": len(all_flat),
        "samples_per_year": {y: len(s) for y, s in all_samples.items()},
        "reproducibility": {
            "seed": DEFAULT_TRAIN_SEED,
            "epochs": EPOCHS,
            "label_source": "sia",
            "label_policy_version": policy.policy_version,
            "label_threshold_kt": ground_truth.SIA_REFERENCE_KT,
            "note": (
                "train_epochs() uses a locally-scoped random.Random(seed) instance "
                "(not the global random module), so re-running this script against "
                "identical cached raw data (logs/raw_cache/) reproduces identical "
                "weights and metrics. Labels are SIA-first (ground_truth policy "
                "above), never Samedan-proxy - metrics are NOT comparable to "
                "pre-SIA-labeling runs."
            ),
        },
        "evaluation": evaluation,
        "deployment": deployment,
        "final_weights": deploy_weights,
        "monthly_breakdown": monthly_breakdown(all_flat),
        "timeline": [
            {"date": s["date"], "actual_kt": s["actual_wind_kt"],
             "probability": round(score(s["features"], deploy_weights), 3), "year": s["year"]}
            for s in sorted(all_flat, key=lambda x: x["date"])
        ],
    }
    with open(DASHBOARD_DATA_PATH, "w") as f:
        json.dump(dashboard_data, f, indent=2)

    print(f"\nDone. {len(all_flat)} total samples. Dashboard data written to {DASHBOARD_DATA_PATH}")


if __name__ == "__main__":
    sys.exit(main())
