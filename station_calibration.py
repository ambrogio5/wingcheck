"""Measure SIA/Windsurfcenter agreement before permitting label substitution.

This command is analysis-only: it never edits weights or ground-truth policy.
Rows are paired on UTC timestamps, optionally testing bounded whole-hour lags.
Direction errors use circular distance rather than ordinary subtraction.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

import ground_truth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "logs", "historical", "reports")


def pearson(xs, ys):
    if len(xs) < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return numerator / denominator if denominator else None


def _rank(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + j - 1) / 2 + 1
        for index in order[i:j]:
            ranks[index] = rank
        i = j
    return ranks


def spearman(xs, ys):
    return pearson(_rank(xs), _rank(ys)) if len(xs) >= 2 else None


def circular_error(a, b):
    return abs((a - b + 180) % 360 - 180)


PAIRING_TOLERANCE_MINUTES = 30


def _nearest_hour(ts: datetime):
    floored = ts.replace(minute=0, second=0, microsecond=0)
    if ts - floored >= timedelta(minutes=30):
        return floored + timedelta(hours=1), (floored + timedelta(hours=1)) - ts
    return floored, ts - floored


def pair_records(records, source_a="windsurfcenter", source_b="sia", lag_hours=0):
    """Pairs source_a and source_b observations on the same UTC hour
    (nearest-hour bucketing within PAIRING_TOLERANCE_MINUTES - real lake
    scrapes land at arbitrary minutes, so exact-equality matching would
    silently produce zero pairs). When several readings fall in one hour
    bucket, the one closest to the top of the hour wins - never an
    interpolated or forward-filled value."""
    by_source = defaultdict(dict)
    for row in records:
        if row.get("wind_speed_ms") is None:
            continue
        ts = datetime.fromisoformat(row["timestamp_utc"])
        hour, distance = _nearest_hour(ts)
        if distance > timedelta(minutes=PAIRING_TOLERANCE_MINUTES):
            continue
        bucket = by_source[row["source"]]
        if hour not in bucket or distance < bucket[hour][1]:
            bucket[hour] = (row, distance)
    pairs = []
    lag = timedelta(hours=lag_hours)
    for hour, (left, _) in by_source[source_a].items():
        right = by_source[source_b].get(hour + lag)
        if right:
            pairs.append((left, right[0]))
    return pairs


def metrics_for_pairs(pairs):
    if not pairs:
        return {"n": 0}
    actual = [a["wind_speed_ms"] for a, _ in pairs]
    reference = [b["wind_speed_ms"] for _, b in pairs]
    errors = [b - a for a, b in zip(actual, reference)]
    direction_errors = [
        circular_error(a["wind_direction_deg"], b["wind_direction_deg"])
        for a, b in pairs
        if a.get("wind_direction_deg") is not None and b.get("wind_direction_deg") is not None
    ]
    return {
        "n": len(pairs),
        "pearson": pearson(actual, reference),
        "spearman": spearman(actual, reference),
        "mae_ms": statistics.fmean(abs(e) for e in errors),
        "rmse_ms": math.sqrt(statistics.fmean(e * e for e in errors)),
        "bias_sia_minus_lake_ms": statistics.fmean(errors),
        "median_bias_ms": statistics.median(errors),
        "circular_direction_mae_deg": statistics.fmean(direction_errors) if direction_errors else None,
        "direction_n": len(direction_errors),
    }


# Minimum-reporting maturity gates by INDEPENDENT overlapping days (not raw
# pair count - 100 pairs from 2 days is 2 days of weather, not 100 samples
# of independent evidence). These gate what may be REPORTED, they never
# automatically prove equivalence or change policy.
MATURITY_INSUFFICIENT_DAYS = 14
MATURITY_CALIBRATION_CANDIDATE_DAYS = 42


def independent_days(pairs) -> int:
    return len({datetime.fromisoformat(a["timestamp_utc"]).date() for a, _ in pairs})


def calibration_maturity(n_days: int) -> str:
    if n_days < MATURITY_INSUFFICIENT_DAYS:
        return "insufficient"
    if n_days < MATURITY_CALIBRATION_CANDIDATE_DAYS:
        return "preliminary"
    return "calibration_candidate"


def classify_relationship(metrics, n_days=0):
    """Conservative descriptive classification into the reviewed taxonomy
    (near_equivalent / calibrated_equivalent / regime_dependent_reference /
    predictor_only / insufficient_evidence) - never an automatic policy
    change, and never a classification stronger than the maturity gate
    allows. The suggestive thresholds below are candidates for human
    review, not automatic rules - a passing number here still requires
    seasonal coverage, stable bias direction, and regime analysis before
    any policy edit (see config/ground_truth_policy.json's reviewer_note)."""
    if n_days < MATURITY_INSUFFICIENT_DAYS or metrics.get("n", 0) == 0:
        return "insufficient_evidence"
    corr = metrics.get("pearson")
    mae = metrics.get("mae_ms")
    bias = abs(metrics.get("bias_sia_minus_lake_ms", math.inf))
    if corr is not None and corr >= 0.95 and mae <= 1.0 and bias <= 0.5 \
            and n_days >= MATURITY_CALIBRATION_CANDIDATE_DAYS:
        return "near_equivalent"
    if corr is not None and corr >= 0.85 and mae <= 2.0 \
            and n_days >= MATURITY_CALIBRATION_CANDIDATE_DAYS:
        return "calibrated_equivalent"
    if n_days < MATURITY_CALIBRATION_CANDIDATE_DAYS:
        return "insufficient_evidence"
    return "predictor_only"


def analyze(records, source_a="windsurfcenter", source_b="sia", maximum_lag_hours=3):
    lag_results = {}
    for lag in range(-maximum_lag_hours, maximum_lag_hours + 1):
        lag_results[str(lag)] = metrics_for_pairs(pair_records(records, source_a, source_b, lag))
    best_lag = max(lag_results, key=lambda key: (
        lag_results[key].get("pearson") is not None,
        lag_results[key].get("pearson") or -2,
        lag_results[key].get("n", 0),
    ))
    zero_pairs = pair_records(records, source_a, source_b, 0)
    zero = lag_results["0"]
    n_days = independent_days(zero_pairs)
    by_month = {}
    for month in range(1, 13):
        pairs = [pair for pair in zero_pairs
                 if datetime.fromisoformat(pair[0]["timestamp_utc"]).month == month]
        by_month[str(month)] = metrics_for_pairs(pairs)
    return {
        "schema_version": 2,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_a": source_a,
        "source_b": source_b,
        "zero_lag": zero,
        "independent_overlapping_days": n_days,
        "calibration_maturity": calibration_maturity(n_days),
        "lag_results_hours": lag_results,
        "best_lag_hours": int(best_lag),
        "monthly": by_month,
        "classification": classify_relationship(zero, n_days),
        "policy_changed": False,
        "warning": "Exploratory report. Review overlap, seasonality and sensor definitions before enabling SIA substitution.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Calibrate two ground-truth station sources")
    parser.add_argument("--registry", default=ground_truth.DEFAULT_REGISTRY_PATH)
    parser.add_argument("--source-a", default="windsurfcenter")
    parser.add_argument("--source-b", default="sia")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = analyze(ground_truth.load_jsonl(args.registry), args.source_a, args.source_b)
    output = args.output
    if not output:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        output = os.path.join(REPORT_DIR, f"station_calibration_{args.source_a}_{args.source_b}_{stamp}.json")
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({"report": output, **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
