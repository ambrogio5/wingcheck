"""Prepare provenance-preserving labels for the existing backtest samples.

This intentionally stops before training.  It lets reviewers inspect label
coverage/source/confidence and station calibration before production weights are
touched.  Use ``backtest.py`` only after the ground-truth policy is approved.
"""

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

import ground_truth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FEATURE_ROWS = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")
DEFAULT_OUTPUT = os.path.join(BASE_DIR, "logs", "historical", "datasets", "retraining_samples.jsonl")


def _hour_key(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("Europe/Zurich"))
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def prepare(feature_rows, observations, policy):
    by_hour = defaultdict(list)
    for obs in observations:
        by_hour[_hour_key(obs["timestamp_utc"])].append(obs)
    output, excluded = [], Counter()
    for sample in feature_rows:
        key = _hour_key(sample["date"])
        label = ground_truth.select_label(by_hour.get(key, []), policy)
        if label is None:
            excluded["no_acceptable_ground_truth"] += 1
            continue
        row = dict(sample)
        speed_ms = label["wind_speed_ms"]
        row["actual_wind_kt"] = round(speed_ms * 1.943844, 2)
        row["outcome"] = 1.0 if row["actual_wind_kt"] >= 10.0 else 0.0
        row["label_provenance"] = {
            "source": label["source"], "station_id": label["station_id"],
            "confidence": label.get("confidence"), "quality_flags": label.get("quality_flags", []),
            "source_provenance": label.get("provenance", {}),
        }
        output.append(row)
    return output, dict(excluded)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare retraining rows from the ground-truth registry")
    parser.add_argument("--features", default=DEFAULT_FEATURE_ROWS)
    parser.add_argument("--registry", default=ground_truth.DEFAULT_REGISTRY_PATH)
    parser.add_argument("--policy", default=ground_truth.DEFAULT_POLICY_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    rows = ground_truth.load_jsonl(args.features)
    observations = ground_truth.load_jsonl(args.registry)
    prepared, excluded = prepare(rows, observations, ground_truth.load_policy(args.policy))
    ground_truth.write_jsonl(args.output, prepared)
    sources = Counter(row["label_provenance"]["source"] for row in prepared)
    summary = {"output": args.output, "n": len(prepared), "labels_by_source": dict(sources),
               "excluded": excluded, "weights_modified": False}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
