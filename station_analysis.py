"""
station_analysis.py - fixed station-family research: NOT an open-ended
search. Exactly ten pre-registered comparisons (see FAMILY_DEFINITIONS)
are evaluated, using chronological, day-grouped rolling-origin
(expanding-window) evaluation - never a random row split, never a search
over arbitrary feature combinations.

This script NEVER writes weights.json or docs/dashboard_data.json - every
model it trains goes through model.new_weights() and is discarded after
scoring (verified by tests/test_station_analysis.py asserting
weights.json's mtime is unchanged before/after a full run).

HONESTY NOTE: five of the ten families add a station-derived "family
score" built from maloja_diagnostics.py + station_features.py. Only the
pressure family (lug/sma - both confirmed, enabled stations with a real
multi-year archive) has genuine historical coverage today. The other four
(source heating, summit support, radiation/moisture, competing flow) rely
on station roles (source_region, pass, summit) with NO confirmed station
yet (see config/stations.json / docs/STATION_RESEARCH.md) - for those,
every sample's family score is a constant "missing" value, and the
resulting metrics will correctly show no incremental value, because there
IS no real data yet. This script reports that honestly rather than
disguising a structural data gap as a negative research finding about the
underlying physical hypothesis.

Usage: `python3 station_analysis.py` - reads logs/backtest_dataset.jsonl
(the same labeled dataset backtest.py trains on) and the historical
station archive (historical_data.py), writes a timestamped JSON+Markdown
report to logs/historical/reports/.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone

import historical_data as hd
import maloja_diagnostics as md
import research_metrics as rm
import research_report
import station_features as sf
import station_registry
from backtest import PRIME_WINDOW_END_HOUR, PRIME_WINDOW_START_HOUR, WINDOW_END_HOUR, WINDOW_START_HOUR
from features import FEATURE_NAMES
from metrics import classification_report
from model import new_weights, score as model_score, train_epochs, WEIGHTS_PATH

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")

STATION_CUTOFF = "07:00"  # matches forecast_and_log.py's earliest scheduled run
EPOCHS = 40

NEW_FAMILY_FEATURES = (
    "source_heating_score", "summit_support_score", "pressure_family_score",
    "radiation_family_score", "competing_flow_score",
)

FAMILY_DEFINITIONS = {
    "majority_class_baseline": (),  # special-cased - no real features used
    "forecast_wind_only": ("model_wind",),
    "wind_gust_direction": ("model_wind", "model_gust", "surface_dir_alignment"),
    "full_current_model": tuple(FEATURE_NAMES),
    "full_plus_source_heating": tuple(FEATURE_NAMES) + ("source_heating_score",),
    "full_plus_summit_support": tuple(FEATURE_NAMES) + ("summit_support_score",),
    "full_plus_pressure_family": tuple(FEATURE_NAMES) + ("pressure_family_score",),
    "full_plus_radiation_family": tuple(FEATURE_NAMES) + ("radiation_family_score",),
    "full_plus_competing_flow": tuple(FEATURE_NAMES) + ("competing_flow_score",),
    "full_plus_all_spatial_families": tuple(FEATURE_NAMES) + NEW_FAMILY_FEATURES,
}


def load_dataset(path=DATASET_PATH) -> list:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _group_by_local_date(records: list) -> dict:
    by_date = {}
    for r in records:
        date_str = r["timestamp_local"][:10]
        by_date.setdefault(date_str, []).append(r)
    return by_date


def load_station_archives() -> dict:
    """Returns {station_id: records_by_local_date} for every enabled
    station currently on disk."""
    registry = station_registry.load_registry()
    out = {}
    for sid, s in registry.items():
        if not s.enabled:
            continue
        records = hd._read_jsonl(hd.station_hourly_path(sid))
        out[sid] = {"records_by_date": _group_by_local_date(records), "reporting_delay_minutes": s.reporting_delay_minutes}
    return out


def compute_family_scores_by_date(dates: list, archives: dict) -> dict:
    """Computes the 5 new candidate family scores once per unique calendar
    date (not once per hourly sample - a date's station data doesn't
    change across the 7 scored hours in that day)."""
    lug = archives.get("lug", {}).get("records_by_date", {})
    sma = archives.get("sma", {}).get("records_by_date", {})
    lug_delay = archives.get("lug", {}).get("reporting_delay_minutes", 0)
    sma_delay = archives.get("sma", {}).get("reporting_delay_minutes", 0)

    scores_by_date = {}
    for date in sorted(set(dates)):
        lug_feats = sf.generate_station_features(lug.get(date, []), date, STATION_CUTOFF, lug_delay)
        sma_feats = sf.generate_station_features(sma.get(date, []), date, STATION_CUTOFF, sma_delay)
        pressure_result = md.pressure_support(lug_feats, sma_feats)

        # No confirmed source_region/pass/summit station yet (see
        # docs/STATION_RESEARCH.md) - these three are honestly always
        # "missing" today. Kept as explicit calls (not hardcoded 0.0) so
        # the moment a real station is confirmed, this starts producing
        # real scores with no code change here.
        source_heating_result = md.source_heating({}, {})
        summit_support_result = md.summit_support({})
        radiation_result = md.radiation_support({})
        # Raw wind-direction degrees aren't retained in the historical
        # labeled dataset (only normalized engineered features are) - see
        # this module's docstring. Honestly missing, not fabricated.
        competing_flow_result = md.competing_flow(None)

        scores_by_date[date] = {
            "source_heating_score": source_heating_result["score"] if not source_heating_result["missing"] else 0.0,
            "summit_support_score": summit_support_result["score"] if not summit_support_result["missing"] else 0.0,
            "pressure_family_score": pressure_result["score"] if not pressure_result["missing"] else 0.0,
            "radiation_family_score": radiation_result["score"] if not radiation_result["missing"] else 0.0,
            "competing_flow_score": competing_flow_result["score"] if not competing_flow_result["missing"] else 0.0,
            "_missing": {
                "source_heating_score": source_heating_result["missing"],
                "summit_support_score": summit_support_result["missing"],
                "pressure_family_score": pressure_result["missing"],
                "radiation_family_score": radiation_result["missing"],
                "competing_flow_score": competing_flow_result["missing"],
            },
        }
    return scores_by_date


def augment_samples_with_family_scores(samples: list, scores_by_date: dict) -> list:
    """Returns NEW sample dicts (does not mutate input) with the 5 new
    family-score features merged into each sample's features dict."""
    out = []
    for s in samples:
        date = s["date"][:10]
        scores = scores_by_date.get(date, {})
        feats = dict(s["features"])
        for name in NEW_FAMILY_FEATURES:
            feats[name] = scores.get(name, 0.0)
        out.append({**s, "features": feats})
    return out


def _log_loss(labels, probs):
    eps = 1e-9
    total = 0.0
    for y, p in zip(labels, probs):
        p = min(max(p, eps), 1 - eps)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(labels) if labels else None


def _full_metrics(samples, probs) -> dict:
    labels = [s["outcome"] for s in samples]
    n = len(labels)
    if n == 0:
        return {"n": 0}
    report = classification_report(labels, probs, threshold=0.5)
    report["log_loss"] = round(_log_loss(labels, probs), 4) if labels else None
    report["false_positive_rate"] = round(1 - report["specificity"], 4) if report.get("specificity") is not None else None
    report["false_negative_rate"] = round(1 - report["recall"], 4) if report.get("recall") is not None else None
    report["coverage"] = 1.0  # production features always populated (best-effort neutral fallback, never null)
    report["missing_rate"] = 0.0
    report["n_unique_days"] = len({s["date"][:10] for s in samples})
    return report


def majority_class_probs(train_samples, validate_samples) -> list:
    if not train_samples:
        return [0.5] * len(validate_samples)
    positive_rate = sum(s["outcome"] for s in train_samples) / len(train_samples)
    return [positive_rate] * len(validate_samples)


def evaluate_family_on_fold(family_name: str, feature_names: tuple, fold: dict) -> dict:
    train, validate = fold["train"], fold["validate"]
    if family_name == "majority_class_baseline":
        probs = majority_class_probs(train, validate)
    else:
        weights = new_weights(feature_names=feature_names)
        train_epochs(weights, train, EPOCHS)
        probs = [model_score(s["features"], weights) for s in validate]

    paired = list(zip(validate, probs))
    full_window_pairs = [(s, p) for s, p in paired if WINDOW_START_HOUR <= int(s["date"][11:13]) <= WINDOW_END_HOUR]
    prime_window_pairs = [(s, p) for s, p in paired if PRIME_WINDOW_START_HOUR <= int(s["date"][11:13]) <= PRIME_WINDOW_END_HOUR]
    full_window = [s for s, _ in full_window_pairs]
    prime_window = [s for s, _ in prime_window_pairs]
    full_probs = [p for _, p in full_window_pairs]
    prime_probs = [p for _, p in prime_window_pairs]

    return {
        "fold": fold["name"], "kind": fold["kind"],
        "full_window": _full_metrics(full_window, full_probs),
        "prime_window": _full_metrics(prime_window, prime_probs),
    }


def run_rolling_origin_family_comparison(samples: list) -> dict:
    folds = rm.rolling_origin_splits(samples)
    results = {name: [] for name in FAMILY_DEFINITIONS}
    for fold in folds:
        for name, feature_names in FAMILY_DEFINITIONS.items():
            results[name].append(evaluate_family_on_fold(name, feature_names, fold))
    return results


# --- Correlation report (section 9) ---

def run_correlation_analysis(samples: list) -> dict:
    """Per-feature Pearson/Spearman/point-biserial/ROC AUC, coverage, and
    day-level bootstrap CI, with Benjamini-Hochberg FDR correction. Wind
    direction is never fed as raw degrees - only vector-derived features
    (already the case for every candidate feature in this codebase)."""
    from metrics import roc_auc as _roc_auc

    outcomes = [s["outcome"] for s in samples]
    days = [s["date"][:10] for s in samples]
    all_feature_names = sorted({k for s in samples for k in s["features"]})

    raw_results = {}
    p_values = []
    for name in all_feature_names:
        values = [s["features"].get(name) for s in samples]
        coverage = rm.coverage_pct(values)
        pearson = rm.pearson_correlation(values, outcomes)
        spearman = rm.spearman_correlation(values, outcomes)
        point_biserial = rm.point_biserial_correlation(outcomes, values)
        try:
            auc = _roc_auc(outcomes, [v if v is not None else 0.0 for v in values])
        except Exception:
            auc = None

        def _pearson_stat(pairs):
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            return rm.pearson_correlation(xs, ys)

        ci = rm.bootstrap_ci_by_day_multi(days, list(zip(values, outcomes)), _pearson_stat, n_resamples=200)
        n_used = sum(1 for v in values if v is not None)
        p_val = rm.corr_to_p_value_approx(pearson, n_used)
        p_values.append(p_val)
        raw_results[name] = {
            "pearson": pearson, "spearman": spearman, "point_biserial": point_biserial,
            "roc_auc": auc, "coverage": round(coverage, 4), "n_used": n_used,
            "pearson_bootstrap_ci_90pct": ci, "_p_value_approx": p_val,
        }

    significance = rm.benjamini_hochberg(p_values, alpha=0.10)
    for name, sig in zip(all_feature_names, significance):
        raw_results[name]["fdr_significant_at_0.10"] = sig
        del raw_results[name]["_p_value_approx"]

    n_unique_days = len(set(days))
    return {
        "n_samples": len(samples), "n_unique_days": n_unique_days,
        "features": raw_results,
    }


# --- Calibration reliability summary (a lightweight diagnostic, not a
# separate calibration-fitting step - this project has no Platt/isotonic
# transform; this just reports how well the raw model probabilities are
# already calibrated per rolling fold) ---

def reliability_table(labels, probs, n_bins=10) -> list:
    bins = [{"lo": i / n_bins, "hi": (i + 1) / n_bins, "n": 0, "sum_pred": 0.0, "sum_actual": 0.0} for i in range(n_bins)]
    for y, p in zip(labels, probs):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx]["n"] += 1
        bins[idx]["sum_pred"] += p
        bins[idx]["sum_actual"] += y
    table = []
    for b in bins:
        if b["n"] == 0:
            table.append({"range": [round(b["lo"], 2), round(b["hi"], 2)], "n": 0, "avg_predicted": None, "observed_rate": None})
            continue
        table.append({
            "range": [round(b["lo"], 2), round(b["hi"], 2)], "n": b["n"],
            "avg_predicted": round(b["sum_pred"] / b["n"], 4),
            "observed_rate": round(b["sum_actual"] / b["n"], 4),
        })
    return table


def expected_calibration_error(labels, probs, n_bins=10) -> float:
    table = reliability_table(labels, probs, n_bins)
    n_total = sum(b["n"] for b in table)
    if n_total == 0:
        return None
    return round(sum(b["n"] * abs(b["avg_predicted"] - b["observed_rate"]) for b in table if b["n"] > 0) / n_total, 4)


def run_calibration_summary(samples: list) -> dict:
    """Reliability table + ECE for the full_current_model family, per
    rolling-origin fold - diagnostic only, no calibration transform is
    fitted or applied anywhere in this project."""
    folds = rm.rolling_origin_splits(samples)
    results = []
    for fold in folds:
        weights = new_weights(feature_names=FAMILY_DEFINITIONS["full_current_model"])
        train_epochs(weights, fold["train"], EPOCHS)
        validate = fold["validate"]
        probs = [model_score(s["features"], weights) for s in validate]
        labels = [s["outcome"] for s in validate]
        results.append({
            "fold": fold["name"], "kind": fold["kind"],
            "n": len(validate),
            "expected_calibration_error": expected_calibration_error(labels, probs),
            "reliability_table": reliability_table(labels, probs),
        })
    return results


def _write_markdown_summary(report: dict, path: str):
    lines = [f"# Station analysis report ({report['generated_at']})", ""]
    lines.append(f"Commit: `{report['commit_sha']}`")
    lines.append("")
    lines.append("## Family comparison - 2026 reference fold, full window ROC AUC")
    lines.append("")
    lines.append("| family | ROC AUC |")
    lines.append("|---|---|")
    for name, folds in report["rolling_origin_family_comparison"].items():
        ref = next((f for f in folds if f["kind"] == "reference"), None)
        auc = ref["full_window"].get("roc_auc") if ref else None
        lines.append(f"| {name} | {auc} |")
    lines.append("")
    lines.append("## Warnings")
    for w in report["warnings"]:
        lines.append(f"- {w}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    weights_mtime_before = os.path.getmtime(WEIGHTS_PATH) if os.path.exists(WEIGHTS_PATH) else None

    samples = load_dataset()
    print(f"Loaded {len(samples)} labeled hours from {DATASET_PATH}.")

    archives = load_station_archives()
    dates = [s["date"][:10] for s in samples]
    print("Computing station family scores per calendar date...")
    scores_by_date = compute_family_scores_by_date(dates, archives)
    augmented = augment_samples_with_family_scores(samples, scores_by_date)

    print("Running correlation analysis...")
    correlation = run_correlation_analysis(augmented)

    print("Running rolling-origin fixed-family comparison (10 families)...")
    family_comparison = run_rolling_origin_family_comparison(augmented)

    print("Running calibration reliability summary (production feature set)...")
    calibration = run_calibration_summary(augmented)

    missing_summary = {}
    for name in NEW_FAMILY_FEATURES:
        missing_dates = sum(1 for d in scores_by_date.values() if d["_missing"].get(name))
        missing_summary[name] = {"missing_dates": missing_dates, "total_dates": len(scores_by_date)}

    warnings = [
        "2026 is a repeatedly-inspected reference fold, not a pristine holdout - see CLAUDE.md.",
        "source_heating_score, summit_support_score, radiation_family_score, and "
        "competing_flow_score have ZERO real historical coverage (no confirmed source_region/"
        "pass/summit station yet) - their family comparisons structurally cannot show "
        "incremental value and must not be read as a negative physical finding.",
        "pressure_family_score is the only new family with genuine historical coverage "
        "(lug/sma are both confirmed, enabled stations).",
    ]
    for name, info in missing_summary.items():
        if info["missing_dates"] == info["total_dates"]:
            warnings.append(f"{name}: missing for all {info['total_dates']} dates in this dataset.")

    report = research_report.new_report(
        script_name="station_analysis",
        config={"epochs": EPOCHS, "station_cutoff": STATION_CUTOFF, "families": list(FAMILY_DEFINITIONS)},
        data_sources={"backtest_dataset": DATASET_PATH},
        warnings=warnings,
        limitations=[
            "Rolling-origin folds are the primary evidence; the 2026 reference fold alone is not.",
            "Family score coverage is documented in 'family_score_coverage' below.",
        ],
    )
    report["correlation"] = correlation
    report["rolling_origin_family_comparison"] = family_comparison
    report["calibration"] = calibration
    report["family_score_coverage"] = missing_summary

    json_path = research_report.save_report(report, "station_analysis")
    md_path = json_path.replace(".json", ".md")
    _write_markdown_summary(report, md_path)
    print(f"Report written to {json_path}")
    print(f"Markdown summary written to {md_path}")

    weights_mtime_after = os.path.getmtime(WEIGHTS_PATH) if os.path.exists(WEIGHTS_PATH) else None
    assert weights_mtime_before == weights_mtime_after, "station_analysis.py must never modify weights.json"

    return 0


if __name__ == "__main__":
    sys.exit(main())
