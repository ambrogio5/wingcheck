"""
station_analysis.py - Phase 5/6: correlation, feature-screening, and
station-family incremental-value evaluation for the Malojawind model.

Never modifies weights.json - every model here is fresh
(model.new_weights()), trained/evaluated in memory, and discarded.
Output goes to logs/historical/reports/ (JSON) via research_report.py.

WHY MOST "STATION FAMILIES" AREN'T ACTUALLY TESTED HERE: this project has
real, fetched, parsed historical data for exactly THREE stations (sam,
lug, sma - see stations.py's verification="confirmed" entries), and both
of the features derived from them (samedan_morning_score,
pressure_nowcast_score) are ALREADY in the production feature set. Every
other candidate station investigated (Corvatsch, Piz Nair, Diavolezza,
etc.) has zero rows in the historical archive as of this run (see
historical_data.py's coverage report) - this script explicitly checks
that and reports "insufficient_coverage" for those rather than fabricating
a correlation from data that doesn't exist. Once historical_data.py sync
actually pulls real data for a new station (requires network access this
sandboxed session didn't have - see docs/STATION_RESEARCH.md), re-running
this script will automatically pick it up as a testable family.

Protocol (Phase 6, avoiding unrestricted 2026 holdout mining):
  - Correlation diagnostics are computed over the FULL dataset (2024-2026)
    for transparency, but are explicitly diagnostic/descriptive, not used
    to select which features enter incremental-value testing.
  - Incremental-value / station-family comparisons use rolling-origin
    (expanding-window, chronological, day-grouped) folds - see
    research_metrics.rolling_origin_splits. 2026 is used only as a labeled
    "reference" fold, not a repeatedly-mined selection holdout.
"""

import json
import os
import sys
from datetime import datetime, timezone

from features import FEATURE_NAMES
from metrics import classification_report, roc_auc, average_precision, build_session_samples
from model import new_weights, train_epochs, score, validate_schema, DEFAULT_TRAIN_SEED
from research_metrics import (
    correlation_report, bootstrap_ci_by_day_multi, pearson_correlation,
    rolling_origin_splits, benjamini_hochberg, corr_to_p_value_approx,
)
from research_report import new_report, save_report, BASE_DIR
from stations import STATIONS

DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")
STATIONS_MANIFEST_PATH = os.path.join(BASE_DIR, "logs", "historical", "manifests", "stations.json")
RESEARCH_SEED = DEFAULT_TRAIN_SEED
EPOCHS = 40
FULL_WINDOW = (12, 18)
PRIME_WINDOW = (14, 18)

WIND_ONLY = ("model_wind",)
WIND_GUST_DIR = ("model_wind", "model_gust", "surface_dir_alignment")

# Feature families this project can actually test (see module docstring
# for why every OTHER candidate station is reported, not tested).
TESTABLE_STATION_FAMILIES = {
    "samedan_morning": ("samedan_morning_score",),
    "pressure_nowcast": ("pressure_nowcast_score",),
}


def load_dataset():
    with open(DATASET_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def day_id(sample):
    return sample["date"][:10]


def hour_of(sample):
    return int(sample["date"][11:13])


def train_fresh(feature_subset, samples, seed=RESEARCH_SEED, epochs=EPOCHS):
    w = new_weights(feature_subset)
    validate_schema(w, feature_subset)
    train_samples = [{"features": {k: s["features"][k] for k in feature_subset}, "outcome": s["outcome"]}
                      for s in samples]
    return train_epochs(w, train_samples, epochs=epochs, seed=seed)


def score_group(weights, feature_subset, samples):
    labels = [s["outcome"] for s in samples]
    probs = [score({k: s["features"][k] for k in feature_subset}, weights) for s in samples]
    return labels, probs


def majority_class_probs(train_samples, n):
    rate = sum(s["outcome"] for s in train_samples) / len(train_samples) if train_samples else 0.5
    return [rate] * n


# ---------------------------------------------------------------------------
# 1. Correlation diagnostics (Phase 5.3) - descriptive, over the full dataset
# ---------------------------------------------------------------------------

def run_correlation_analysis(samples):
    outcomes = [s["outcome"] for s in samples]
    days = [day_id(s) for s in samples]
    results = {}
    p_values, names = [], []

    for name in FEATURE_NAMES:
        values = [s["features"].get(name) for s in samples]
        report = correlation_report(values, outcomes)

        pairs = [(v, o) for v, o in zip(values, outcomes) if v is not None]
        if len(pairs) >= 2 and len({o for _, o in pairs}) > 1:
            vs, os_ = zip(*pairs)
            report["roc_auc"] = roc_auc(list(os_), list(vs))
            report["pr_auc"] = average_precision(list(os_), list(vs))
        else:
            report["roc_auc"] = None
            report["pr_auc"] = None

        point, lo, hi = bootstrap_ci_by_day_multi(
            days, [values, outcomes],
            statistic_fn=lambda vs, os_: pearson_correlation(
                *zip(*[(v, o) for v, o in zip(vs, os_) if v is not None])
            ) if any(v is not None for v in vs) else None,
            n_resamples=200, seed=RESEARCH_SEED)
        report["pearson_bootstrap_ci_90pct"] = [lo, hi]

        results[name] = report
        p_values.append(corr_to_p_value_approx(report["pearson"], report["n_used"])
                         if report["pearson"] is not None else None)
        names.append(name)

    significant = benjamini_hochberg(p_values, alpha=0.10)
    for name, sig in zip(names, significant):
        results[name]["fdr_significant_at_0.10"] = sig
    results["_note"] = (
        f"{len(names)} features tested; Benjamini-Hochberg FDR correction applied at "
        "alpha=0.10 (see 'fdr_significant_at_0.10' per feature). This is a DIAGNOSTIC "
        "correlation screen only - it does not select which features enter the "
        "incremental-value tests below, and must not be used to pick a 'winning' "
        "feature and then report that feature's own correlation as if it were unbiased."
    )
    return results


# ---------------------------------------------------------------------------
# 2. Rolling-origin incremental-value tests (Phase 5.5 / 6.1)
# ---------------------------------------------------------------------------

def _window_filter(samples, window):
    start, end = window
    return [s for s in samples if start <= hour_of(s) <= end]


def evaluate_group_on_fold(feature_subset, train_samples, validate_samples):
    """Trains fresh on train_samples (or scores the majority-class rate if
    feature_subset is None), evaluates on validate_samples for both the
    full and prime windows and the daily-session aggregation. Returns a
    dict of metrics - never touches weights.json."""
    out = {}
    for window_name, window in (("full_window", FULL_WINDOW), ("prime_window", PRIME_WINDOW)):
        val_slice = _window_filter(validate_samples, window)
        if not val_slice:
            out[window_name] = {"n": 0}
            continue
        labels = [s["outcome"] for s in val_slice]
        if feature_subset is None:
            probs = majority_class_probs(_window_filter(train_samples, window) or train_samples, len(val_slice))
        else:
            weights = train_fresh(feature_subset, _window_filter(train_samples, FULL_WINDOW) or train_samples)
            _, probs = score_group(weights, feature_subset, val_slice)
        out[window_name] = classification_report(labels, probs, threshold=0.5)

    # Daily session outcome (any rideable hour in the full window)
    full_val = _window_filter(validate_samples, FULL_WINDOW)
    if full_val:
        dates = [s["date"] for s in full_val]
        outcomes = [s["outcome"] for s in full_val]
        if feature_subset is None:
            probs = majority_class_probs(_window_filter(train_samples, FULL_WINDOW) or train_samples, len(full_val))
        else:
            weights = train_fresh(feature_subset, _window_filter(train_samples, FULL_WINDOW) or train_samples)
            _, probs = score_group(weights, feature_subset, full_val)
        session_outcomes, session_probs, _ = build_session_samples(dates, outcomes, probs, *FULL_WINDOW)
        out["session"] = classification_report(session_outcomes, session_probs, threshold=0.5)
    else:
        out["session"] = {"n": 0}
    return out


def run_rolling_origin_family_comparison(samples):
    """For each of the defined baseline/family groups, evaluates on every
    rolling-origin fold (Phase 6.1) - train/validate splits are always
    chronological and day-grouped (research_metrics.rolling_origin_splits),
    2026 appearing only as a labeled 'reference' fold."""
    folds = rolling_origin_splits(samples, date_key="date")

    groups = {
        "majority_class_baseline": None,
        "forecast_wind_only": WIND_ONLY,
        "wind_gust_direction": WIND_GUST_DIR,
        "full_current_model": FEATURE_NAMES,
    }
    for family_name, family_features in TESTABLE_STATION_FAMILIES.items():
        groups[f"full_plus_{family_name}"] = FEATURE_NAMES  # already includes it - see note below
        groups[f"full_minus_{family_name}"] = tuple(f for f in FEATURE_NAMES if f not in family_features)

    results = {}
    for group_name, feature_subset in groups.items():
        fold_results = []
        for fold in folds:
            metrics = evaluate_group_on_fold(feature_subset, fold["train"], fold["validate"])
            fold_results.append({
                "fold": fold["name"], "kind": fold["kind"],
                "n_train": len(fold["train"]), "n_validate": len(fold["validate"]),
                **metrics,
            })
        results[group_name] = fold_results

    results["_note"] = (
        "'full_plus_<family>' groups are identical to full_current_model, since that "
        "family's feature is ALREADY in the production schema - included for symmetry "
        "with full_minus_<family> so both directions of the ablation are visible per "
        "fold. Every 2024/2025 fold is a genuine rolling-origin validation (never seen "
        "during that fold's training); the 2026 fold is labeled 'reference', not "
        "'holdout', because 2026 has already been inspected repeatedly by earlier work "
        "in this project (see docs/DATA_ARCHITECTURE.md)."
    )
    return results


# ---------------------------------------------------------------------------
# 3. Candidate station coverage report (Phase 3/18's "do not fabricate" gate)
# ---------------------------------------------------------------------------

def run_station_coverage_report():
    if not os.path.exists(STATIONS_MANIFEST_PATH):
        return {"error": "logs/historical/manifests/stations.json not found - run "
                          "`python3 historical_data.py sync` first"}
    with open(STATIONS_MANIFEST_PATH) as f:
        manifest = json.load(f)

    report = {}
    for sid, info in manifest["stations"].items():
        n = info["coverage"]["n_records"]
        if info["verification"] != "confirmed":
            status = "unavailable_historically" if n == 0 else "candidate_partial_data"
        elif n == 0:
            status = "insufficient_coverage"
        else:
            status = "available_for_analysis"
        report[sid] = {
            "name": info["name"], "verification": info["verification"],
            "confidence": info["confidence"], "n_records": n, "status": status,
        }
    return report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(DATASET_PATH):
        print(f"No dataset at {DATASET_PATH} - run backtest.py first.", file=sys.stderr)
        return 1

    samples = load_dataset()
    print(f"Loaded {len(samples)} labeled hours from {DATASET_PATH}.")

    print("Running correlation analysis (diagnostic, full dataset)...")
    correlation = run_correlation_analysis(samples)

    print("Running rolling-origin station-family comparison...")
    rolling_origin = run_rolling_origin_family_comparison(samples)

    print("Building candidate-station coverage report...")
    station_coverage = run_station_coverage_report()

    report = new_report(
        "station_analysis",
        config={
            "seed": RESEARCH_SEED, "epochs": EPOCHS,
            "full_window": FULL_WINDOW, "prime_window": PRIME_WINDOW,
            "testable_station_families": TESTABLE_STATION_FAMILIES,
        },
        data_sources=[DATASET_PATH, STATIONS_MANIFEST_PATH],
        warnings=[
            "Only 3 stations (sam, lug, sma) have real historical data in this "
            "project as of this run - every other candidate station is reported via "
            "station_coverage, not tested, since no real observations exist for them.",
            "Correlation diagnostics are descriptive only; they do NOT select which "
            "features enter the incremental-value/rolling-origin comparison.",
        ],
        limitations=[
            "The 2026 fold is a repeatedly-inspected reference evaluation, not a "
            "pristine holdout - see docs/DATA_ARCHITECTURE.md.",
            "Backtest features come from Open-Meteo's 0-hour historical archive, not "
            "a genuine multi-day-lead forecast.",
        ],
    )
    report["correlation"] = correlation
    report["rolling_origin_family_comparison"] = rolling_origin
    report["station_coverage"] = station_coverage

    path = save_report(report, "station_analysis")
    print(f"\nReport written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
