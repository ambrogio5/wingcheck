"""
ablation.py - lightweight, reproducible feature-ablation comparison, always
trained fresh on 2024+2025 and evaluated on the untouched 2026 holdout.

This is a DIAGNOSTIC comparison, not a model-selection procedure: every
group below is scored once against the same holdout, so picking the
best-looking row here and reporting ITS holdout number as "the" model's
unbiased accuracy would itself be a form of holdout leakage (you'd have
used 2026 to choose among 7 candidates, then reported a 2026 result as if
it were still unbiased). backtest.py does not do that - the deployed model
is always the full feature set, trained separately; this module only
explains, after the fact, roughly how much each feature group is
contributing on data none of them trained on.

CORE_PHYSICAL_FEATURES documents the "previous core physical feature set"
explicitly (this repo's history is ambiguous otherwise): the 8 hand-designed
physical-driver features that existed before this session's ensemble,
persistence, interaction, and real-station-nowcast additions (cloud_score,
a 9th driver from that original set, was later dropped for having ~0
correlation with outcome - see CLAUDE.md's accuracy-ceiling section - so it
is deliberately excluded here too).
"""

from typing import NamedTuple

from features import FEATURE_NAMES
from metrics import classification_report
from model import new_weights, score, train_epochs, DEFAULT_TRAIN_SEED

CORE_PHYSICAL_FEATURES = (
    "thermal_excess",
    "pressure_signal",
    "upper_wind_alignment",
    "upper_wind_speed_score",
    "dewpoint_score",
    "cape_penalty",
    "freezing_level_score",
    "precip_penalty",
)

WIND_ONLY_FEATURES = ("model_wind",)
WIND_GUST_DIRECTION_FEATURES = ("model_wind", "model_gust", "surface_dir_alignment")

FULL_MINUS_PRESSURE_NOWCAST = tuple(f for f in FEATURE_NAMES if f != "pressure_nowcast_score")
FULL_MINUS_SAMEDAN_MORNING = tuple(f for f in FEATURE_NAMES if f != "samedan_morning_score")


class AblationGroup(NamedTuple):
    name: str
    features: tuple  # None means "majority-class baseline, no features trained"


ABLATION_GROUPS = (
    AblationGroup("majority_class_baseline", None),
    AblationGroup("forecast_wind_only", WIND_ONLY_FEATURES),
    AblationGroup("wind_gust_direction", WIND_GUST_DIRECTION_FEATURES),
    AblationGroup("previous_core_physical_set", CORE_PHYSICAL_FEATURES),
    AblationGroup("full_current_model", FEATURE_NAMES),
    AblationGroup("full_minus_pressure_nowcast", FULL_MINUS_PRESSURE_NOWCAST),
    AblationGroup("full_minus_samedan_morning", FULL_MINUS_SAMEDAN_MORNING),
)


def _subset_features(sample_features: dict, feature_subset: tuple) -> dict:
    return {name: sample_features[name] for name in feature_subset}


def run_ablation(train_samples: list, holdout_samples: list, epochs: int,
                  seed: int = DEFAULT_TRAIN_SEED) -> list:
    """Trains one fresh model per group in ABLATION_GROUPS on train_samples
    only, scores it on holdout_samples, and returns a list of result dicts
    (name, n_features, plus the threshold-independent metrics: roc_auc,
    pr_auc, accuracy/balanced_accuracy/brier_score at the standard 0.5
    cutoff). Every group gets its own from-scratch model - no group's
    training can leak into another's."""
    holdout_labels = [s["outcome"] for s in holdout_samples]
    results = []

    for group in ABLATION_GROUPS:
        if group.features is None:
            train_positive_rate = sum(s["outcome"] for s in train_samples) / len(train_samples)
            holdout_probs = [train_positive_rate] * len(holdout_samples)
            n_features = 0
        else:
            weights = new_weights(group.features)
            train_subset = [
                {"features": _subset_features(s["features"], group.features), "outcome": s["outcome"]}
                for s in train_samples
            ]
            weights = train_epochs(weights, train_subset, epochs=epochs, seed=seed)
            holdout_probs = [
                score(_subset_features(s["features"], group.features), weights)
                for s in holdout_samples
            ]
            n_features = len(group.features)

        report = classification_report(holdout_labels, holdout_probs, threshold=0.5)
        results.append({
            "name": group.name,
            "n_features": n_features,
            "roc_auc": report.get("roc_auc"),
            "pr_auc": report.get("pr_auc"),
            "accuracy": report.get("accuracy"),
            "balanced_accuracy": report.get("balanced_accuracy"),
            "brier_score": report.get("brier_score"),
        })

    return results
