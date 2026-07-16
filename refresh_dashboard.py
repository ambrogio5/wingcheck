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
DASHBOARD_DATA_PATH = os.path.join(BASE_DIR, "docs", "dashboard_data.json")
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
            {"date": e["date"], "actual_kt": e["actual_kt"],
             "probability": e["probability"], "year": e["year"], "source": e["source"]}
            for e in entries
        ],
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
