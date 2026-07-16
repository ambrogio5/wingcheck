"""
regime_analysis.py - Phase 10: breaks down the current production
feature set's out-of-sample performance by weather regime (regimes.py),
using the SAME rolling-origin folds as station_analysis.py so every
number here is genuinely out-of-sample, not fit-then-scored on the same
data.

Never modifies weights.json.
"""

import json
import os
import sys
from collections import defaultdict

from features import FEATURE_NAMES
from metrics import classification_report
from regimes import classify_samples, REGIME_NAMES
from research_metrics import rolling_origin_splits
from research_report import new_report, save_report, BASE_DIR
from station_analysis import train_fresh, score_group, RESEARCH_SEED, EPOCHS

DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")


def load_dataset():
    with open(DATASET_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def run_regime_analysis(samples):
    """For each rolling-origin fold, trains fresh on the fold's training
    split and scores the fold's validation split, then breaks those
    out-of-sample predictions down by the validation samples' weather
    regime. Returns {fold_name: {regime_name: classification_report}}."""
    folds = rolling_origin_splits(samples, date_key="date")
    results = {}
    for fold in folds:
        weights = train_fresh(FEATURE_NAMES, fold["train"], seed=RESEARCH_SEED, epochs=EPOCHS)
        validate = fold["validate"]
        labels, probs = score_group(weights, FEATURE_NAMES, validate)
        regime_labels = classify_samples(validate)

        by_regime_labels = defaultdict(list)
        by_regime_probs = defaultdict(list)
        for regime, y, p in zip(regime_labels, labels, probs):
            by_regime_labels[regime].append(y)
            by_regime_probs[regime].append(p)

        fold_result = {}
        for regime in REGIME_NAMES:
            ys = by_regime_labels.get(regime, [])
            ps = by_regime_probs.get(regime, [])
            report = classification_report(ys, ps, threshold=0.5)
            report["false_positive_rate"] = (
                round(report["false_positive"] / (report["false_positive"] + report["true_negative"]), 4)
                if report.get("n") and (report["false_positive"] + report["true_negative"]) else None
            )
            fold_result[regime] = report
        results[fold["name"]] = {"kind": fold["kind"], "n_validate": len(validate), "by_regime": fold_result}
    return results


def summarize_false_positive_drivers(results: dict) -> dict:
    """Aggregates false positives across all folds, per regime, to answer
    Phase 10's specific question: which regimes drive false positives?"""
    totals = defaultdict(lambda: {"false_positive": 0, "n": 0})
    for fold_data in results.values():
        for regime, report in fold_data["by_regime"].items():
            if report.get("n"):
                totals[regime]["false_positive"] += report.get("false_positive", 0)
                totals[regime]["n"] += report["n"]
    summary = {}
    for regime, t in totals.items():
        summary[regime] = {
            "n": t["n"], "total_false_positives": t["false_positive"],
            "false_positive_share_of_regime": round(t["false_positive"] / t["n"], 4) if t["n"] else None,
        }
    return dict(sorted(summary.items(), key=lambda kv: -(kv[1]["false_positive_share_of_regime"] or 0)))


def main():
    if not os.path.exists(DATASET_PATH):
        print(f"No dataset at {DATASET_PATH} - run backtest.py first.", file=sys.stderr)
        return 1

    samples = load_dataset()
    print(f"Loaded {len(samples)} labeled hours.")
    print("Running regime analysis across rolling-origin folds (out-of-sample only)...")
    results = run_regime_analysis(samples)
    fp_summary = summarize_false_positive_drivers(results)

    print("\nFalse-positive share by regime (aggregated across all folds):")
    for regime, s in fp_summary.items():
        print(f"  {regime:35s} n={s['n']:5d}  FP_share={s['false_positive_share_of_regime']}")

    report = new_report(
        "regime_analysis",
        config={"seed": RESEARCH_SEED, "epochs": EPOCHS, "regimes": list(REGIME_NAMES)},
        data_sources=[DATASET_PATH],
        warnings=[
            "Regime labels are rule-based on already-normalized engineered features, "
            "not fit or tuned against any holdout.",
            "'easterly_suppression' is a rough proxy (surface-wind SW-character check), "
            "not a real eastern-station-based classification - no eastern/Bernina "
            "station has real historical data in this project (see stations.py).",
        ],
        limitations=[
            "'uncertain_mixed' can be a large share of hours - a rule-based scheme "
            "this simple will not cleanly separate every real synoptic situation.",
        ],
    )
    report["by_fold"] = results
    report["false_positive_summary"] = fp_summary
    path = save_report(report, "regime_analysis")
    print(f"\nReport written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
