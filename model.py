"""
model.py - the learnable part of the forecast.

Combines the engineered features (see features.py) into a probability via a
simple logistic model: p = sigmoid(bias + sum(weight_i * feature_i)).

Weights are updated by verify_and_learn.py every time we find out what
actually happened, using a plain gradient-descent step:

    error = actual_outcome - predicted_probability
    weight_i += learning_rate * error * feature_i
    bias     += learning_rate * error

This is intentionally simple (no ML framework needed) - it's the same
learning rule as a single logistic regression unit trained online, one
sample at a time. Good enough for the current feature set (see
features.FEATURE_NAMES for exactly how many) and a slow-changing physical
system; revisit if you want something fancier once you have a real dataset.

new_weights() is the ONLY sanctioned way to construct a "blank slate" model.
Before it existed, backtest.py's evaluation step loaded weights.json (an
ALREADY-TRAINED model that may have seen the 2026 holdout in a previous
retrain) and only reset the bias - the per-feature weights kept whatever
they'd learned before, so a "held-out" evaluation could be quietly
contaminated by information from the very data it was supposed to be
untouched by. Every fresh-start path (an evaluation model trained only on
past years, a deployment model trained on everything, a deliberate reset)
must go through new_weights() instead of touching weights.json's existing
coefficients or partially resetting fields by hand.
"""

import json
import math
import os
import random

from features import FEATURE_NAMES

WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "weights.json")
LEARNING_RATE = 0.05

# Bumped whenever FEATURE_NAMES changes in a way that isn't backward
# compatible (a schema reset, not an online nudge) - see weights.json's
# "_comment" field for the human-readable changelog of past resets.
SCHEMA_VERSION = 3

DEFAULT_BIAS = -0.5
DEFAULT_TIER_THRESHOLDS = {"good": 0.65, "marginal": 0.40}

# Default reproducibility seed for batch/epoch training (backtest.py,
# ablation.py). A single online update() step (verify_and_learn.py) has no
# shuffling to seed - this only matters for train_epochs()'s multi-sample,
# multi-epoch loop, where shuffle order affects final weights.
DEFAULT_TRAIN_SEED = 20260716


def new_weights(feature_names=None) -> dict:
    """A completely fresh weights structure: trained_samples=0, every
    feature weight at 0.0, a deliberate initial bias, default tier
    thresholds, and the current schema version. Two calls never share
    mutable state (each builds its own dict/nested dict from scratch).

    feature_names defaults to features.FEATURE_NAMES - the full production
    schema. Pass a subset (e.g. for ablation.py's feature-group comparisons)
    to build a smaller model; the returned weights dict only contains
    entries for exactly those names, and model.score()/update() already
    tolerate features not present in weights (via dict.get / `in` checks),
    so a subset model naturally ignores any extra keys a caller's features
    dict might have."""
    names = tuple(feature_names) if feature_names is not None else FEATURE_NAMES
    return {
        "_comment": (
            "Logistic-regression-style weights for the Malojawind model. "
            "bias + one weight per engineered feature. Updated automatically "
            "by verify_and_learn.py (online) and backtest.py (from scratch, "
            "via model.new_weights()) as real outcomes come in. Edit by hand "
            "only if you want to force a reset - back up first."
        ),
        "version": SCHEMA_VERSION,
        "trained_samples": 0,
        "bias": DEFAULT_BIAS,
        "weights": {name: 0.0 for name in names},
        "tier_thresholds": dict(DEFAULT_TIER_THRESHOLDS),
    }


def validate_schema(weights: dict, feature_names=None) -> None:
    """Raise ValueError if weights["weights"]'s keys don't exactly match
    feature_names (defaults to features.FEATURE_NAMES) - catches silent
    drift between the engineered-feature schema and a model's weight
    schema before it corrupts a training run. Call this right after
    building a fresh evaluation/deployment model in backtest.py."""
    expected = set(feature_names) if feature_names is not None else set(FEATURE_NAMES)
    declared = set(weights.get("weights", {}))
    missing = expected - declared
    extra = declared - expected
    if missing or extra:
        raise ValueError(
            "weights schema does not match the expected feature schema: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


def load_weights():
    with open(WEIGHTS_PATH) as f:
        return json.load(f)


def save_weights(w):
    with open(WEIGHTS_PATH, "w") as f:
        json.dump(w, f, indent=2)


def sigmoid(x):
    x = max(-30, min(30, x))  # avoid overflow
    return 1.0 / (1.0 + math.exp(-x))


def score(features: dict, weights: dict = None) -> float:
    """Returns probability (0-1) that this hour will be a good session."""
    if weights is None:
        weights = load_weights()
    z = weights["bias"]
    for name, value in features.items():
        z += weights["weights"].get(name, 0.0) * value
    return sigmoid(z)


def update(features: dict, actual_outcome: float, weights: dict = None):
    """One online gradient-descent step. actual_outcome is 1.0 (session
    happened, wind reached MARGINAL_KT+) or 0.0 (it didn't). Returns and
    persists the updated weights."""
    if weights is None:
        weights = load_weights()

    predicted = score(features, weights)
    error = actual_outcome - predicted

    # Decaying learning rate: early samples move the model a lot, but after
    # thousands of training samples each new one should only nudge it -
    # otherwise the model keeps jumping around forever instead of converging.
    n = weights.get("trained_samples", 0)
    lr = LEARNING_RATE / (1.0 + n / 500.0) ** 0.5

    weights["bias"] += lr * error
    for name, value in features.items():
        if name in weights["weights"]:
            weights["weights"][name] += lr * error * value

    weights["trained_samples"] = weights.get("trained_samples", 0) + 1
    save_weights(weights)
    return weights


def train_epochs(weights: dict, samples: list, epochs: int, learning_rate: float = LEARNING_RATE,
                  seed: int = DEFAULT_TRAIN_SEED) -> dict:
    """Batch trainer used by backtest.py/ablation.py: runs `epochs` passes
    of plain (non-decaying) gradient descent over `samples`, reshuffling
    each epoch with a locally-seeded random generator so two runs over
    identical data produce identical weights - no reliance on the global
    `random` module's state, which a caller might seed differently or not
    at all elsewhere in the same process.

    Does NOT mutate the caller's `samples` list (shuffles a local copy) and
    does NOT mutate the caller's `weights` dict in place beyond what it
    returns - callers should treat the return value as the updated model.
    Sets weights["trained_samples"] to len(samples) (what THIS training
    call saw), not an accumulated total across previous calls - a model
    fully retrained from new_weights() should report exactly how many
    samples it was actually trained on, not a cumulative counter inherited
    from a previous run's weights.json."""
    rng = random.Random(seed)
    local_samples = list(samples)
    for _ in range(epochs):
        rng.shuffle(local_samples)
        for s in local_samples:
            predicted = score(s["features"], weights)
            error = s["outcome"] - predicted
            weights["bias"] += learning_rate * error
            for name, value in s["features"].items():
                if name in weights["weights"]:
                    weights["weights"][name] += learning_rate * error * value
    weights["trained_samples"] = len(local_samples)
    return weights
