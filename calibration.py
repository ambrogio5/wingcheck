"""
calibration.py - reusable probability-calibration helpers: reliability
tables, Brier score / log loss / expected & maximum calibration error, and
two calibration-fitting methods (Platt scaling, isotonic regression via
pool-adjacent-violators) - all pure stdlib, consistent with this project's
no-heavy-dependency philosophy.

Every fitting function here takes a TRAINING (or inner-validation) split
explicitly and returns a small, serializable calibration mapping; the
caller is responsible for applying it only to a genuinely separate
evaluation split - these functions have no notion of a holdout and will
happily overfit to whatever they're given, exactly like
metrics.calibrate_marginal_threshold's documented contract.

Beta calibration was investigated for Phase 8 but not implemented in this
pass - Platt scaling and isotonic regression already cover the two most
common failure modes (systematic over/under-confidence, and non-sigmoidal
miscalibration) with much less code, and adding a third parametric family
without evidence the first two are insufficient would cut against this
project's "add complexity only when it demonstrates value" principle. See
docs/DATA_ARCHITECTURE.md's calibration section.
"""

import math


def reliability_table(labels: list, probs: list, n_bins: int = 10) -> list:
    """Splits [0,1] into n_bins equal-width bins (0-10%, 10-20%, ...) and
    reports, per bin: prediction count, average predicted probability,
    observed positive rate, and calibration error (|avg_pred - observed|).
    A bin with zero predictions reports counts of 0 and null rates rather
    than being omitted, so every report has the same fixed bin structure."""
    bins = [{"low": i / n_bins, "high": (i + 1) / n_bins, "probs": [], "labels": []} for i in range(n_bins)]
    for p, y in zip(probs, labels):
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx]["probs"].append(p)
        bins[idx]["labels"].append(y)

    table = []
    for b in bins:
        n = len(b["probs"])
        avg_pred = sum(b["probs"]) / n if n else None
        observed_rate = sum(b["labels"]) / n if n else None
        error = abs(avg_pred - observed_rate) if n else None
        table.append({
            "bin_low": round(b["low"], 2), "bin_high": round(b["high"], 2),
            "n": n, "avg_predicted": round(avg_pred, 4) if avg_pred is not None else None,
            "observed_rate": round(observed_rate, 4) if observed_rate is not None else None,
            "calibration_error": round(error, 4) if error is not None else None,
        })
    return table


def expected_calibration_error(labels: list, probs: list, n_bins: int = 10) -> float:
    """Weighted average of each bin's |avg_predicted - observed_rate|,
    weighted by the bin's share of all predictions - the standard ECE
    definition."""
    table = reliability_table(labels, probs, n_bins)
    total = sum(b["n"] for b in table)
    if total == 0:
        return None
    return sum(b["n"] * b["calibration_error"] for b in table if b["n"]) / total


def maximum_calibration_error(labels: list, probs: list, n_bins: int = 10) -> float:
    table = reliability_table(labels, probs, n_bins)
    errors = [b["calibration_error"] for b in table if b["n"]]
    return max(errors) if errors else None


def log_loss(labels: list, probs: list) -> float:
    if not labels:
        return None
    eps = 1e-12
    total = 0.0
    for y, p in zip(labels, probs):
        p = min(1 - eps, max(eps, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(labels)


# ---------------------------------------------------------------------------
# Platt scaling: p_calibrated = sigmoid(a * logit(p_raw) + b), fit by simple
# gradient descent on the training split's (raw_prob, outcome) pairs.
# ---------------------------------------------------------------------------

def _logit(p, eps=1e-6):
    p = min(1 - eps, max(eps, p))
    return math.log(p / (1 - p))


def _sigmoid(z):
    z = max(-30, min(30, z))
    return 1.0 / (1.0 + math.exp(-z))


def fit_platt_scaling(train_labels: list, train_probs: list, epochs: int = 200, learning_rate: float = 0.05) -> dict:
    """Fits a=slope, b=intercept on the logit of train_probs against
    train_labels via plain gradient descent (deterministic - this is a
    2-parameter convex problem, no shuffling/seed needed). Returns
    {"a":, "b":} - a=1,b=0 (the identity mapping on the logit scale, i.e.
    passthrough) if there's no training data."""
    if not train_labels:
        return {"a": 1.0, "b": 0.0}
    logits = [_logit(p) for p in train_probs]
    a, b = 1.0, 0.0
    n = len(train_labels)
    for _ in range(epochs):
        grad_a = grad_b = 0.0
        for x, y in zip(logits, train_labels):
            p = _sigmoid(a * x + b)
            error = y - p
            grad_a += error * x
            grad_b += error
        a += learning_rate * grad_a / n
        b += learning_rate * grad_b / n
    return {"a": a, "b": b}


def apply_platt_scaling(probs: list, platt_model: dict) -> list:
    return [_sigmoid(platt_model["a"] * _logit(p) + platt_model["b"]) for p in probs]


# ---------------------------------------------------------------------------
# Isotonic regression via Pool Adjacent Violators (PAVA) - a monotone
# non-decreasing step function fit to (sorted-by-raw-prob) training data.
# ---------------------------------------------------------------------------

def fit_isotonic_regression(train_labels: list, train_probs: list) -> list:
    """Returns a list of {"x": raw_prob_threshold, "y": calibrated_value}
    breakpoints, sorted by x, forming a monotone non-decreasing step
    function (the classic PAVA result). Empty list if there's no training
    data - apply_isotonic_regression then passes inputs through unchanged."""
    if not train_labels:
        return []
    pairs = sorted(zip(train_probs, train_labels), key=lambda pair: pair[0])
    # Each "block" starts as a single point; whenever a block's mean would
    # violate monotonicity with the previous block, merge them - repeat
    # until the whole sequence is non-decreasing. Blocks store (x_repr,
    # y_mean, weight) so a merge is an O(1) weighted-average.
    blocks = [[x, y, 1] for x, y in pairs]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            w1, w2 = blocks[i][2], blocks[i + 1][2]
            merged_y = (blocks[i][1] * w1 + blocks[i + 1][1] * w2) / (w1 + w2)
            merged_x = blocks[i + 1][0]  # keep the later block's x as the step's right edge
            blocks[i:i + 2] = [[merged_x, merged_y, w1 + w2]]
            i = max(0, i - 1)
        else:
            i += 1
    return [{"x": b[0], "y": b[1]} for b in blocks]


def apply_isotonic_regression(probs: list, breakpoints: list) -> list:
    """Step-function lookup: each prob maps to the y-value of the first
    breakpoint whose x is >= it (i.e. the smallest enclosing step), or the
    last breakpoint's y if prob exceeds every threshold. Passes inputs
    through unchanged if breakpoints is empty (no training data was fit)."""
    if not breakpoints:
        return list(probs)
    xs = [b["x"] for b in breakpoints]
    out = []
    for p in probs:
        idx = 0
        while idx < len(xs) - 1 and xs[idx] < p:
            idx += 1
        out.append(breakpoints[idx]["y"])
    return out


def calibration_summary(labels: list, probs: list, n_bins: int = 10) -> dict:
    """The standard bundle of calibration numbers for one (labels, probs)
    pair - used for "uncalibrated"/"platt"/"isotonic" side-by-side
    comparison in calibration_analysis.py."""
    return {
        "brier_score": round(sum((p - y) ** 2 for y, p in zip(labels, probs)) / len(labels), 4) if labels else None,
        "log_loss": round(log_loss(labels, probs), 4) if labels else None,
        "expected_calibration_error": round(expected_calibration_error(labels, probs, n_bins), 4) if labels else None,
        "maximum_calibration_error": round(maximum_calibration_error(labels, probs, n_bins), 4) if labels else None,
        "reliability_table": reliability_table(labels, probs, n_bins),
    }
