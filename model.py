"""
model.py - the learnable part of the forecast.

Combines the engineered features (see features.py) into a probability via a
simple logistic model: p = sigmoid(bias + sum(weight_i * feature_i)).

Weights start from physically-reasoned defaults (weights.json) and get
nudged by verify_and_learn.py every time we find out what actually happened,
using a plain gradient-descent step:

    error = actual_outcome - predicted_probability
    weight_i += learning_rate * error * feature_i
    bias     += learning_rate * error

This is intentionally simple (no ML framework needed) - it's the same
learning rule as a single logistic regression unit trained online, one
sample at a time. Good enough for ~9 features and a slow-changing physical
system; revisit if you want something fancier once you have a real dataset.
"""

import json
import math
import os

WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "weights.json")
LEARNING_RATE = 0.05


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
