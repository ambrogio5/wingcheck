"""
model_regularized.py - RESEARCH-ONLY L2-regularized logistic regression.

The production model stays exactly as it is (model.py / weights.json,
plain unregularized online gradient descent) - this module exists purely
so station_analysis.py and future research can compare it against a
regularized alternative, WITHOUT introducing scikit-learn or any other
heavy dependency into the live inference path. Pure stdlib, same
lightweight philosophy as the rest of this project.

Not wired into forecast_and_log.py / verify_and_learn.py / backtest.py in
any way - purely a research tool, deliberately isolated in its own file so
it's obvious at a glance that nothing production-facing depends on it.

Key differences from model.py's train_epochs():
  - L2 penalty on the feature weights (not the bias)
  - features are standardized first (zero mean, unit variance), fit ONLY
    on the training split passed in - never on validation/holdout data
  - missing feature values get an explicit __missing indicator dummy
    variable AND are imputed to the (already-centered) mean, rather than
    silently defaulting to 0.0 the way the raw model.score() does
  - returns convergence diagnostics (loss history, whether it plateaued)
    so a caller can tell a genuine fit from one that never converged
"""

import math
import random


def standardize_fit(samples: list, feature_names: tuple) -> dict:
    """Computes {feature_name: {"mean":, "std":}} from `samples` ONLY -
    callers must pass just the training split. std of a constant (or
    all-missing) feature is treated as 1.0 to avoid division by zero."""
    stats = {}
    for name in feature_names:
        values = [s["features"].get(name) for s in samples]
        present = [v for v in values if v is not None]
        mean = sum(present) / len(present) if present else 0.0
        variance = sum((v - mean) ** 2 for v in present) / len(present) if present else 0.0
        std = math.sqrt(variance) if variance > 1e-12 else 1.0
        stats[name] = {"mean": mean, "std": std}
    return stats


def standardize_apply(features: dict, feature_names: tuple, stats: dict) -> dict:
    """Returns a flat dict: one standardized value per feature (imputed to
    0.0 - the standardized mean - when missing) PLUS one `<name>__missing`
    0.0/1.0 indicator per feature. This combined dict is what
    train_l2_logistic/score_l2_logistic operate on."""
    out = {}
    for name in feature_names:
        v = features.get(name)
        is_missing = v is None
        out[f"{name}__missing"] = 1.0 if is_missing else 0.0
        out[name] = 0.0 if is_missing else (v - stats[name]["mean"]) / stats[name]["std"]
    return out


def _augmented_feature_names(feature_names: tuple) -> tuple:
    aug = []
    for name in feature_names:
        aug.append(name)
        aug.append(f"{name}__missing")
    return tuple(aug)


def _sigmoid(z):
    z = max(-30, min(30, z))
    return 1.0 / (1.0 + math.exp(-z))


def train_l2_logistic(samples: list, feature_names: tuple, l2: float = 1.0, epochs: int = 200,
                       learning_rate: float = 0.1, seed: int = 20260716) -> tuple:
    """Batch (full-dataset, not online) gradient descent with an L2 penalty
    on every feature weight (never on the bias, and never on the
    missing-indicator weights' penalty term is included like any other
    weight - a station family that's frequently missing should be free to
    learn a real, if uncertain, missing-indicator coefficient).

    samples: list of {"features": <standardized+indicator dict from
    standardize_apply>, "outcome": 0.0/1.0}. Does not mutate its input.
    Deterministic given `seed` (a locally-scoped random.Random instance,
    never the global `random` module).

    Returns (model_dict, diagnostics) where model_dict = {"bias":,
    "weights": {name: coef}} and diagnostics reports the loss trajectory
    and a simple convergence flag."""
    aug_names = _augmented_feature_names(feature_names)
    rng = random.Random(seed)
    weights = {name: 0.0 for name in aug_names}
    bias = 0.0
    n = len(samples)
    if n == 0:
        return {"bias": 0.0, "weights": weights}, {"final_loss": None, "converged": False,
                                                     "loss_history": [], "epochs": epochs, "l2": l2,
                                                     "learning_rate": learning_rate, "seed": seed}

    local_samples = list(samples)
    loss_history = []
    for _ in range(epochs):
        rng.shuffle(local_samples)
        grad_w = {name: 0.0 for name in aug_names}
        grad_b = 0.0
        total_loss = 0.0
        eps = 1e-12
        for s in local_samples:
            z = bias + sum(weights[name] * s["features"][name] for name in aug_names)
            p = _sigmoid(z)
            error = s["outcome"] - p
            for name in aug_names:
                grad_w[name] += error * s["features"][name]
            grad_b += error
            total_loss += -(s["outcome"] * math.log(p + eps) + (1 - s["outcome"]) * math.log(1 - p + eps))

        for name in aug_names:
            weights[name] += learning_rate * (grad_w[name] / n - l2 * weights[name])
        bias += learning_rate * (grad_b / n)

        reg_term = (l2 / 2.0) * sum(w ** 2 for w in weights.values())
        loss_history.append(total_loss / n + reg_term)

    converged = len(loss_history) > 1 and abs(loss_history[-1] - loss_history[-2]) < 1e-5
    diagnostics = {
        "final_loss": loss_history[-1] if loss_history else None,
        "converged": converged,
        "loss_history_sampled": loss_history[::max(1, len(loss_history) // 20)] if loss_history else [],
        "epochs": epochs, "l2": l2, "learning_rate": learning_rate, "seed": seed,
    }
    return {"bias": bias, "weights": weights}, diagnostics


def score_l2_logistic(model: dict, standardized_features: dict) -> float:
    z = model["bias"] + sum(model["weights"].get(name, 0.0) * v for name, v in standardized_features.items())
    return _sigmoid(z)


def fit_and_score(train_samples: list, validate_samples: list, feature_names: tuple,
                   l2: float = 1.0, epochs: int = 200, learning_rate: float = 0.1, seed: int = 20260716) -> dict:
    """End-to-end research helper: fits standardization on train_samples
    ONLY, trains an L2-regularized model, scores validate_samples, and
    returns {"model", "diagnostics", "standardize_stats", "validate_probs",
    "validate_labels"} - the caller (station_analysis.py or a notebook)
    plugs validate_probs/labels into metrics.classification_report."""
    stats = standardize_fit(train_samples, feature_names)

    train_std = [{"features": standardize_apply(s["features"], feature_names, stats), "outcome": s["outcome"]}
                 for s in train_samples]
    model, diagnostics = train_l2_logistic(train_std, feature_names, l2=l2, epochs=epochs,
                                            learning_rate=learning_rate, seed=seed)

    validate_probs = [
        score_l2_logistic(model, standardize_apply(s["features"], feature_names, stats))
        for s in validate_samples
    ]
    validate_labels = [s["outcome"] for s in validate_samples]

    return {
        "model": model,
        "diagnostics": diagnostics,
        "standardize_stats": stats,
        "validate_probs": validate_probs,
        "validate_labels": validate_labels,
    }
