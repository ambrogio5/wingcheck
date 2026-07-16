"""
calibration_analysis.py - Phase 8: fits and compares uncalibrated / Platt-
scaled / isotonic-regression-calibrated probabilities for the production
feature set, using the same rolling-origin folds as station_analysis.py
so calibration is always fit on a fold's TRAINING split only and measured
on that fold's separate validation split (never the final evaluation
period) - see calibration.py's module docstring for why fitting on the
evaluation period itself would be a leak.

Never modifies weights.json.
"""

import json
import os
import sys

from calibration import calibration_summary, fit_platt_scaling, apply_platt_scaling, \
    fit_isotonic_regression, apply_isotonic_regression
from features import FEATURE_NAMES
from research_metrics import rolling_origin_splits
from research_report import new_report, save_report, BASE_DIR
from station_analysis import train_fresh, score_group, RESEARCH_SEED, EPOCHS

DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")


def load_dataset():
    with open(DATASET_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def run_calibration_analysis(samples):
    folds = rolling_origin_splits(samples, date_key="date")
    results = []
    for fold in folds:
        weights = train_fresh(FEATURE_NAMES, fold["train"], seed=RESEARCH_SEED, epochs=EPOCHS)
        train_labels, train_probs = score_group(weights, FEATURE_NAMES, fold["train"])
        val_labels, val_probs = score_group(weights, FEATURE_NAMES, fold["validate"])

        platt = fit_platt_scaling(train_labels, train_probs)
        platt_val_probs = apply_platt_scaling(val_probs, platt)

        isotonic_bp = fit_isotonic_regression(train_labels, train_probs)
        isotonic_val_probs = apply_isotonic_regression(val_probs, isotonic_bp)

        results.append({
            "fold": fold["name"], "kind": fold["kind"],
            "n_train": len(fold["train"]), "n_validate": len(fold["validate"]),
            "uncalibrated": calibration_summary(val_labels, val_probs),
            "platt": calibration_summary(val_labels, platt_val_probs),
            "isotonic": calibration_summary(val_labels, isotonic_val_probs),
            "platt_params": platt,
        })
    return results


def main():
    if not os.path.exists(DATASET_PATH):
        print(f"No dataset at {DATASET_PATH} - run backtest.py first.", file=sys.stderr)
        return 1

    samples = load_dataset()
    print(f"Loaded {len(samples)} labeled hours.")
    print("Running calibration analysis across rolling-origin folds...")
    results = run_calibration_analysis(samples)

    for r in results:
        u, p, i = r["uncalibrated"], r["platt"], r["isotonic"]
        print(f"  {r['fold']:22s} n_val={r['n_validate']:4d}  "
              f"ECE uncal={u['expected_calibration_error']}  platt={p['expected_calibration_error']}  "
              f"isotonic={i['expected_calibration_error']}")

    report = new_report(
        "calibration_analysis",
        config={"seed": RESEARCH_SEED, "epochs": EPOCHS, "n_bins": 10},
        data_sources=[DATASET_PATH],
        warnings=[
            "Calibration mappings (Platt/isotonic) are fit on each fold's TRAINING "
            "split only and applied to that fold's separate validation split - never "
            "fit on the period being measured.",
            "Beta calibration was not implemented this pass - see calibration.py's "
            "module docstring.",
        ],
        limitations=[
            "The 2026 fold is a repeatedly-inspected reference evaluation, not a "
            "pristine holdout.",
        ],
    )
    report["folds"] = results
    path = save_report(report, "calibration_analysis")
    print(f"\nReport written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
