"""
model_comparison_sia_features.py - RESEARCH-ONLY test of three candidate
SIA issue-time features (the deferred "Model C" experiment). Never touches
features.py or weights.json (asserted in main()).

Candidate features, all computed ONLY from SIA observations available by
the earliest (07:00 local) issue cutoff - mirroring the exact
_lookup_morning_obs semantics samedan_morning_score already uses in
production, so a promotion needs no new time-safety machinery:

  sia_morning_wind_score : SIA wind nearest 07:00 local (+/-90min), /10 m/s
  sia_morning_trend      : (wind@~07:00 - wind@~04:00) / 5 m/s, clamped [-1,1]
  sia_gust_factor        : morning gust/wind ratio - 1, clamped [0,2], /2

Protocol: day-grouped chronological expanding-window YEAR folds on the
SIA-labeled backtest dataset (train 2024 -> validate 2025; train
2024+2025 -> validate 2026). Model C0 = the 22 production features,
fresh; Model C = the same plus the 3 SIA features, fresh - both from
model.new_weights() with explicit feature lists, same seed/epochs as
production. The delta C - C0 is the features' incremental value under
identical labels, training and folds.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import metrics
import model
from features import FEATURE_NAMES, _lookup_morning_obs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(BASE_DIR, "logs", "backtest_dataset.jsonl")
SIA_CACHE_PATH = os.path.join(BASE_DIR, "logs", "raw_cache", "generic_sia.json")
REPORT_DIR = os.path.join(BASE_DIR, "logs", "historical", "reports")

EPOCHS = 40
ZURICH_TZ = ZoneInfo("Europe/Zurich")

SIA_FEATURES = ("sia_morning_wind_score", "sia_morning_trend", "sia_gust_factor")


def load_sia_obs():
    with open(SIA_CACHE_PATH) as f:
        cached = json.load(f)
    return {datetime.fromisoformat(k): v for k, v in cached.items()}


def _pre_dawn_obs(obs, date):
    """SIA observation nearest 04:00 local (same tolerance ladder as
    _lookup_morning_obs) - the trend baseline, 3h before the morning obs."""
    dawn_local = date.replace(hour=4, tzinfo=ZURICH_TZ)
    dawn_utc = dawn_local.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for offset_min in (0, 60, -60, 90, -90):
        cand = dawn_utc + timedelta(minutes=offset_min)
        if cand in obs:
            return obs[cand]
    return None


def sia_issue_time_features(sia_obs, target_date_naive_midnight):
    """The three candidate features for one calendar day. Neutral 0.0
    fallbacks on missing data, matching samedan_morning_score's
    convention - a missing morning reading must not poison the row."""
    morning = _lookup_morning_obs(sia_obs, target_date_naive_midnight)
    feats = {name: 0.0 for name in SIA_FEATURES}
    if morning is None or morning.get("wind_speed_ms") is None:
        return feats
    wind = morning["wind_speed_ms"]
    feats["sia_morning_wind_score"] = min(wind / 10.0, 1.5)
    gust = morning.get("wind_gust_ms")
    if gust is not None and wind > 0.5:
        feats["sia_gust_factor"] = min(max(gust / wind - 1.0, 0.0), 2.0) / 2.0
    dawn = _pre_dawn_obs(sia_obs, target_date_naive_midnight)
    if dawn is not None and dawn.get("wind_speed_ms") is not None:
        feats["sia_morning_trend"] = max(-1.0, min(1.0, (wind - dawn["wind_speed_ms"]) / 5.0))
    return feats


def evaluate(rows, weights):
    labels = [r["outcome"] for r in rows]
    probs = [model.score(r["features"], weights) for r in rows]
    return metrics.classification_report(labels, probs, threshold=0.5)


def main(argv=None):
    weights_before = open(os.path.join(BASE_DIR, "weights.json"), "rb").read()

    with open(DATASET_PATH) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    rows.sort(key=lambda r: r["date"])
    sia_obs = load_sia_obs()

    # Augment a COPY of each row's features with the 3 candidates,
    # computed once per calendar day (same morning obs for every hour of
    # that day - it is a per-day issue-time signal, exactly like
    # samedan_morning_score).
    per_day = {}
    n_missing_morning = 0
    augmented = []
    for r in rows:
        day = r["date"][:10]
        if day not in per_day:
            midnight = datetime.fromisoformat(day)
            per_day[day] = sia_issue_time_features(sia_obs, midnight)
            if per_day[day]["sia_morning_wind_score"] == 0.0:
                n_missing_morning += 1
        augmented.append({**r, "features": {**r["features"], **per_day[day]}})

    extended_names = list(FEATURE_NAMES) + list(SIA_FEATURES)
    years = sorted({r["date"][:4] for r in rows})
    fold_reports = []
    for boundary in years[1:]:
        train_base = [r for r in rows if r["date"][:4] < boundary]
        val_base = [r for r in rows if r["date"][:4] == boundary]
        train_aug = [r for r in augmented if r["date"][:4] < boundary]
        val_aug = [r for r in augmented if r["date"][:4] == boundary]
        assert not ({r["date"][:10] for r in train_base} & {r["date"][:10] for r in val_base})

        base = model.new_weights()
        model.train_epochs(base, train_base, epochs=EPOCHS, seed=model.DEFAULT_TRAIN_SEED)
        ext = model.new_weights(extended_names)
        model.train_epochs(ext, train_aug, epochs=EPOCHS, seed=model.DEFAULT_TRAIN_SEED)

        fold_reports.append({
            "validation_year": boundary,
            "n_train": len(train_base), "n_validate": len(val_base),
            "model_c0_production_features": evaluate(val_base, base),
            "model_c_plus_sia_features": evaluate(val_aug, ext),
            "sia_feature_weights_learned": {k: round(ext["weights"][k], 4) for k in SIA_FEATURES},
        })

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_features": list(SIA_FEATURES),
        "n_rows": len(rows), "n_days": len(per_day),
        "n_days_missing_morning_obs": n_missing_morning,
        "epochs": EPOCHS, "seed": model.DEFAULT_TRAIN_SEED,
        "folds": fold_reports,
        "weights_modified": False,
    }
    os.makedirs(REPORT_DIR, exist_ok=True)
    out = os.path.join(REPORT_DIR, f"sia_features_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)

    assert weights_before == open(os.path.join(BASE_DIR, "weights.json"), "rb").read(), \
        "model_comparison_sia_features.py must never modify weights.json"

    print(json.dumps({"report": out, "n_days_missing_morning_obs": n_missing_morning}, indent=2))
    for fr in fold_reports:
        a, b = fr["model_c0_production_features"], fr["model_c_plus_sia_features"]
        print(f"\nfold validate={fr['validation_year']} (train {fr['n_train']} -> validate {fr['n_validate']})")
        for name, m in (("C0 (22 feats)", a), ("C  (+3 SIA)  ", b)):
            print(f"  {name}: acc={m['accuracy']} bal={m['balanced_accuracy']} auc={m['roc_auc']} "
                  f"pr_auc={m['pr_auc']} brier={m['brier_score']}")
        print(f"  learned SIA weights: {fr['sia_feature_weights_learned']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
