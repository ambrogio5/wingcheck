"""
backtest.py - run once (or whenever you want to retrain from scratch).

Instead of waiting weeks for the live model to accumulate outcomes, this
builds the training set directly from history:

  - Weather: Open-Meteo's Historical Forecast API (archive from ~2021),
    same variables/format as the live forecast - not a coarser reanalysis,
    so training data matches what the live model will actually see.
  - Ground truth: MeteoSwiss's real Samedan (SAM) station observations.

Seasons covered: May-October, for 2024, 2025, and 2026 (up to today) -
i.e. wingfoil season only, matching how you'd actually use this.

Steps:
  1. Fetch weather + SAM obs for each season.
  2. Build one labeled sample per afternoon hour (12-18h) per day.
  3. Chronological split: train on 2024+2025, hold out 2026 to see how the
     model would have done on data it never trained on.
  4. Train (multiple epochs of online gradient descent over the training set).
  5. Evaluate on the 2026 holdout, then fold 2026 into training too, so the
     weights you deploy live use everything available.
  6. Write logs/backtest_dataset.jsonl (full sample set) and
     docs/dashboard_data.json (summary for the dashboard).
"""

import json
import os
import random
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from features import fetch_raw_historical, engineer_features
from meteoswiss import fetch_sam_hourly_observations, SAM_PROXY_KT
from model import load_weights, save_weights, score

WINDOW_START_HOUR = 12
WINDOW_END_HOUR = 18
MARGINAL_KT = 10
LEARNING_RATE = 0.05
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


def build_samples_for_season(start_date, end_date, year, sam_obs):
    print(f"Fetching {start_date} to {end_date}...")
    raw = fetch_raw_historical(start_date, end_date)
    times = raw["silvaplana"]["time"]

    samples = []
    for idx, t in enumerate(times):
        dt_local = datetime.fromisoformat(t).replace(tzinfo=ZURICH_TZ)
        if not (WINDOW_START_HOUR <= dt_local.hour <= WINDOW_END_HOUR):
            continue

        dt_utc = dt_local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        obs = sam_obs.get(dt_utc)
        if obs is None:
            continue  # no ground truth for this hour, skip

        feats = engineer_features(raw, idx)
        actual_kt = kt(obs["speed_kmh"])
        actual_gust_kt = kt(obs["gust_kmh"])
        outcome = 1.0 if actual_kt >= SAM_PROXY_KT else 0.0

        samples.append({
            "date": t,
            "year": year,
            "features": feats,
            "actual_wind_kt": round(actual_kt, 1),
            "actual_gust_kt": round(actual_gust_kt, 1),
            "outcome": outcome,
        })
    print(f"  -> {len(samples)} labeled hours")
    return samples


def train(weights, samples, epochs=EPOCHS):
    for epoch in range(epochs):
        random.shuffle(samples)
        for s in samples:
            predicted = score(s["features"], weights)
            error = s["outcome"] - predicted
            weights["bias"] += LEARNING_RATE * error
            for name, value in s["features"].items():
                if name in weights["weights"]:
                    weights["weights"][name] += LEARNING_RATE * error * value
    weights["trained_samples"] = weights.get("trained_samples", 0) + len(samples)
    return weights


def evaluate(weights, samples):
    if not samples:
        return {"n": 0}
    tp = fp = tn = fn = 0
    for s in samples:
        p = score(s["features"], weights)
        predicted = 1.0 if p >= 0.5 else 0.0
        actual = s["outcome"]
        if predicted == 1.0 and actual == 1.0:
            tp += 1
        elif predicted == 1.0 and actual == 0.0:
            fp += 1
        elif predicted == 0.0 and actual == 0.0:
            tn += 1
        else:
            fn += 1
    n = len(samples)
    accuracy = (tp + tn) / n
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {
        "n": n, "accuracy": round(accuracy, 3),
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "true_positive": tp, "false_positive": fp, "true_negative": tn, "false_negative": fn,
    }


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
    print("Fetching MeteoSwiss Samedan ground truth (historical + recent)...")
    sam_obs = fetch_sam_hourly_observations(include_historical=True)
    print(f"  -> {len(sam_obs)} hourly observations available")

    all_samples = {}
    for start, end, year in SEASONS:
        if end < start:
            print(f"Skipping {year}: season hasn't started yet ({start} > {end}).")
            all_samples[year] = []
            continue
        all_samples[year] = build_samples_for_season(start, end, year, sam_obs)

    os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DASHBOARD_DATA_PATH), exist_ok=True)

    with open(DATASET_PATH, "w") as f:
        for year_samples in all_samples.values():
            for s in year_samples:
                f.write(json.dumps(s) + "\n")

    train_set = all_samples[2024] + all_samples[2025]
    holdout_set = all_samples[2026]

    weights = load_weights()
    weights["bias"] = -0.5

    print(f"\nTraining on {len(train_set)} samples (2024+2025), {EPOCHS} epochs...")
    weights = train(weights, list(train_set), epochs=EPOCHS)

    print(f"Evaluating on {len(holdout_set)} held-out 2026 samples (never trained on)...")
    holdout_metrics = evaluate(weights, holdout_set)
    # Class-balance baseline: the accuracy a trivial "never windy" model would
    # get. If our accuracy isn't clearly above this, the model isn't adding value.
    if holdout_set:
        positive_rate = sum(s["outcome"] for s in holdout_set) / len(holdout_set)
        baseline = max(positive_rate, 1 - positive_rate)
        holdout_metrics["positive_rate"] = round(positive_rate, 3)
        holdout_metrics["trivial_baseline_accuracy"] = round(baseline, 3)
        print(f"  Windy-hour rate in holdout: {positive_rate:.1%} "
              f"(trivial always-no baseline accuracy: {baseline:.1%})")
    print(f"  Holdout accuracy: {holdout_metrics.get('accuracy')}, "
          f"precision: {holdout_metrics.get('precision')}, recall: {holdout_metrics.get('recall')}")

    # Fold 2026 into training too, so deployed weights use all available data
    if holdout_set:
        print(f"Folding {len(holdout_set)} 2026 samples into training for the deployed model...")
        weights = train(weights, list(holdout_set), epochs=EPOCHS)

    save_weights(weights)

    all_flat = train_set + holdout_set
    dashboard_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_samples": len(all_flat),
        "samples_per_year": {y: len(s) for y, s in all_samples.items()},
        "holdout_metrics_2026": holdout_metrics,
        "final_weights": weights,
        "monthly_breakdown": monthly_breakdown(all_flat),
        "timeline": [
            {"date": s["date"], "actual_kt": s["actual_wind_kt"],
             "probability": round(score(s["features"], weights), 3), "year": s["year"]}
            for s in sorted(all_flat, key=lambda x: x["date"])
        ],
    }
    with open(DASHBOARD_DATA_PATH, "w") as f:
        json.dump(dashboard_data, f, indent=2)

    print(f"\nDone. {len(all_flat)} total samples. Dashboard data written to {DASHBOARD_DATA_PATH}")


if __name__ == "__main__":
    sys.exit(main())
