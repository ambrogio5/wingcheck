"""
continuous_target_analysis.py - Phase 9: research-only alternative
targets, evaluated alongside (never replacing) the production binary
GOOD/MARGINAL/UNLIKELY classifier.

9.1 Continuous wind target: a simple linear-regression research model
    (pure stdlib gradient descent, identity link) predicting actual_wind_kt
    directly, plus day-level aggregates (max wind, hours above 10/12kt,
    peak/onset/collapse hour) computed straight from the data.
9.2 Daily session target: compares three ways of turning hourly
    predictions into a daily "will there be a session" answer - the
    existing max-hourly-probability aggregation (metrics.build_session_samples,
    already used by backtest.py), a rule-based threshold count, and a
    dedicated daily logistic model trained on day-level feature aggregates.

Never modifies weights.json. Uses the same rolling-origin folds as
station_analysis.py so every number is genuinely out-of-sample.
"""

import json
import math
import os
import random
import sys
from collections import defaultdict

from features import FEATURE_NAMES
from metrics import classification_report, build_session_samples
from model import new_weights, train_epochs, score as score_binary, validate_schema
from research_metrics import rolling_origin_splits, spearman_correlation
from research_report import new_report, save_report, BASE_DIR
from station_analysis import RESEARCH_SEED, EPOCHS, day_id, FULL_WINDOW

DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")


def load_dataset():
    with open(DATASET_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# 9.1 Continuous wind target: a plain linear-regression research model
# ---------------------------------------------------------------------------

def train_linear_regression(samples: list, feature_names: tuple, epochs: int = 100,
                             learning_rate: float = 0.02, seed: int = RESEARCH_SEED) -> dict:
    """Deterministic batch gradient descent minimizing mean squared error
    (identity link, not sigmoid) - a plain linear model predicting a
    continuous target (actual_wind_kt). Does not mutate `samples`."""
    rng = random.Random(seed)
    weights = {name: 0.0 for name in feature_names}
    bias = 0.0
    local = list(samples)
    n = len(local)
    if n == 0:
        return {"bias": 0.0, "weights": weights}
    for _ in range(epochs):
        rng.shuffle(local)
        grad_w = {name: 0.0 for name in feature_names}
        grad_b = 0.0
        for s in local:
            pred = bias + sum(weights[name] * s["features"][name] for name in feature_names)
            error = s["target"] - pred
            for name in feature_names:
                grad_w[name] += error * s["features"][name]
            grad_b += error
        for name in feature_names:
            weights[name] += learning_rate * grad_w[name] / n
        bias += learning_rate * grad_b / n
    return {"bias": bias, "weights": weights}


def predict_linear(model: dict, features: dict, feature_names: tuple) -> float:
    return model["bias"] + sum(model["weights"][name] * features[name] for name in feature_names)


def _regression_metrics(actual: list, predicted: list) -> dict:
    n = len(actual)
    if n == 0:
        return {"n": 0}
    errors = [p - a for a, p in zip(actual, predicted)]
    mae = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
    bias = sum(errors) / n
    rank_corr = spearman_correlation(actual, predicted)
    return {"n": n, "mae": round(mae, 3), "rmse": round(rmse, 3), "bias": round(bias, 3),
            "rank_correlation": round(rank_corr, 4) if rank_corr is not None else None}


def _threshold_derived_report(actual_kt: list, predicted_kt: list, threshold_kt: float) -> dict:
    labels = [1.0 if a >= threshold_kt else 0.0 for a in actual_kt]
    # A continuous predictor has no natural probability - use a squashed
    # distance-from-threshold as a monotone stand-in so the same
    # classification_report machinery (which expects a 0..1 "probability
    # of positive") can be reused for a threshold-derived comparison.
    probs = [1.0 / (1.0 + math.exp(-(p - threshold_kt))) for p in predicted_kt]
    return classification_report(labels, probs, threshold=0.5)


def run_continuous_wind_analysis(samples):
    folds = rolling_origin_splits(samples, date_key="date")
    results = []
    for fold in folds:
        train = [{"features": s["features"], "target": s["actual_wind_kt"]} for s in fold["train"]]
        model = train_linear_regression(train, FEATURE_NAMES, epochs=EPOCHS, seed=RESEARCH_SEED)

        actual = [s["actual_wind_kt"] for s in fold["validate"]]
        predicted = [predict_linear(model, s["features"], FEATURE_NAMES) for s in fold["validate"]]

        results.append({
            "fold": fold["name"], "kind": fold["kind"], "n_validate": len(fold["validate"]),
            "regression_metrics": _regression_metrics(actual, predicted),
            "threshold_10kt_derived": _threshold_derived_report(actual, predicted, 10.0),
            "threshold_12kt_derived": _threshold_derived_report(actual, predicted, 12.0),
        })
    return results


# ---------------------------------------------------------------------------
# 9.2 Daily session target
# ---------------------------------------------------------------------------

def build_daily_session_dataset(samples: list, window=FULL_WINDOW) -> list:
    """One row per calendar day: any_rideable, n_rideable_hours, max_wind_kt,
    max_gust_kt, best_hour, first_rideable_hour, last_rideable_hour,
    session_duration_hours, plus day-level feature AGGREGATES (max of each
    hourly feature across the window) for training a dedicated daily model."""
    by_day = defaultdict(list)
    for s in samples:
        hour = int(s["date"][11:13])
        if window[0] <= hour <= window[1]:
            by_day[day_id(s)].append(s)

    rows = []
    for day, hours in sorted(by_day.items()):
        hours.sort(key=lambda s: s["date"])
        rideable_hours = [int(s["date"][11:13]) for s in hours if s["outcome"] == 1.0]
        row = {
            "date": day,
            "any_rideable": 1.0 if rideable_hours else 0.0,
            "n_rideable_hours": len(rideable_hours),
            "max_wind_kt": max(s["actual_wind_kt"] for s in hours),
            "max_gust_kt": max(s["actual_gust_kt"] for s in hours),
            "best_hour": max(hours, key=lambda s: s["actual_wind_kt"])["date"][11:13],
            "first_rideable_hour": min(rideable_hours) if rideable_hours else None,
            "last_rideable_hour": max(rideable_hours) if rideable_hours else None,
            "session_duration_hours": (max(rideable_hours) - min(rideable_hours) + 1) if rideable_hours else 0,
            "features": {name: max(s["features"][name] for s in hours) for name in FEATURE_NAMES},
        }
        rows.append(row)
    return rows


def run_daily_session_analysis(samples):
    folds = rolling_origin_splits(samples, date_key="date")
    results = []
    for fold in folds:
        # Method 1: max-hourly-probability aggregation (existing production approach)
        w = new_weights(FEATURE_NAMES)
        validate_schema(w, FEATURE_NAMES)
        train_samples = [{"features": s["features"], "outcome": s["outcome"]} for s in fold["train"]]
        w = train_epochs(w, train_samples, epochs=EPOCHS, seed=RESEARCH_SEED)

        val_dates = [s["date"] for s in fold["validate"]]
        val_outcomes = [s["outcome"] for s in fold["validate"]]
        val_probs = [score_binary(s["features"], w) for s in fold["validate"]]
        session_outcomes, session_probs, _ = build_session_samples(val_dates, val_outcomes, val_probs, *FULL_WINDOW)
        max_prob_report = classification_report(session_outcomes, session_probs, threshold=0.5)

        # Method 2: dedicated daily logistic model trained on day-level feature maxes
        train_daily = build_daily_session_dataset(fold["train"])
        validate_daily = build_daily_session_dataset(fold["validate"])
        daily_report = {"n": 0}
        if train_daily and validate_daily:
            dw = new_weights(FEATURE_NAMES)
            validate_schema(dw, FEATURE_NAMES)
            daily_train_samples = [{"features": r["features"], "outcome": r["any_rideable"]} for r in train_daily]
            dw = train_epochs(dw, daily_train_samples, epochs=EPOCHS, seed=RESEARCH_SEED)
            daily_labels = [r["any_rideable"] for r in validate_daily]
            daily_probs = [score_binary(r["features"], dw) for r in validate_daily]
            daily_report = classification_report(daily_labels, daily_probs, threshold=0.5)

        # Method 3: simple rule - "positive" if max hourly model_wind feature
        # (already 0..~2 normalized) exceeds a fixed cutoff.
        rule_labels = [r["any_rideable"] for r in validate_daily] if validate_daily else []
        rule_probs = [1.0 if r["features"]["model_wind"] > 0.5 else 0.0 for r in validate_daily] if validate_daily else []
        rule_report = classification_report(rule_labels, rule_probs, threshold=0.5) if validate_daily else {"n": 0}

        results.append({
            "fold": fold["name"], "kind": fold["kind"], "n_days": len(validate_daily),
            "max_hourly_probability": max_prob_report,
            "dedicated_daily_model": daily_report,
            "rule_based_threshold": rule_report,
        })
    return results


def main():
    if not os.path.exists(DATASET_PATH):
        print(f"No dataset at {DATASET_PATH} - run backtest.py first.", file=sys.stderr)
        return 1

    samples = load_dataset()
    print(f"Loaded {len(samples)} labeled hours.")

    print("Running continuous wind-target analysis...")
    continuous_results = run_continuous_wind_analysis(samples)
    for r in continuous_results:
        rm = r["regression_metrics"]
        print(f"  {r['fold']:22s} n={r['n_validate']:4d}  MAE={rm.get('mae')} RMSE={rm.get('rmse')} "
              f"bias={rm.get('bias')} rank_corr={rm.get('rank_correlation')}")

    print("Running daily-session-target analysis...")
    session_results = run_daily_session_analysis(samples)
    for r in session_results:
        m, d, rb = r["max_hourly_probability"], r["dedicated_daily_model"], r["rule_based_threshold"]
        print(f"  {r['fold']:22s} n_days={r['n_days']:3d}  "
              f"max_prob(bal_acc={m.get('balanced_accuracy')}, spec={m.get('specificity')})  "
              f"daily_model(bal_acc={d.get('balanced_accuracy')}, spec={d.get('specificity')})  "
              f"rule(bal_acc={rb.get('balanced_accuracy')}, spec={rb.get('specificity')})")

    report = new_report(
        "continuous_target_analysis",
        config={"seed": RESEARCH_SEED, "epochs": EPOCHS},
        data_sources=[DATASET_PATH],
        warnings=[
            "The continuous-target linear regression is a plain identity-link model, "
            "not tuned/regularized - a diagnostic comparison point, not a production candidate.",
            "Daily session outcomes are highly imbalanced (see 'positive_rate' in each "
            "fold's classification_report) - balanced accuracy/specificity/ROC AUC are "
            "the metrics to trust here, not raw accuracy.",
        ],
        limitations=["The 2026 fold is a repeatedly-inspected reference evaluation, not a pristine holdout."],
    )
    report["continuous_wind_target"] = continuous_results
    report["daily_session_target"] = session_results
    path = save_report(report, "continuous_target_analysis")
    print(f"\nReport written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
