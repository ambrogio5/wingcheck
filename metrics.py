"""
metrics.py - reusable evaluation-metric helpers for a binary classifier,
implemented in pure Python (stdlib only - no numpy/pandas/scikit-learn) to
keep this project's no-framework, no-heavy-dependency philosophy intact.

Used by backtest.py for the hourly (full + prime window), session-level,
operational-threshold, and feature-ablation evaluations. Every function
here takes plain lists of labels/scores/dates - nothing backtest.py- or
Malojawind-specific - so they're independently testable against toy
examples (see tests/test_metrics.py) and reusable anywhere else a simple
binary classifier needs evaluating.

All functions handle degenerate inputs (empty lists, single-class labels)
by returning None for undefined quantities rather than raising or dividing
by zero.
"""

import math


def safe_div(numerator, denominator):
    return numerator / denominator if denominator else None


def confusion_counts(labels, predictions):
    """labels/predictions: parallel sequences of 0.0/1.0. Returns
    (tp, fp, tn, fn)."""
    tp = fp = tn = fn = 0
    for y, p in zip(labels, predictions):
        if p == 1.0 and y == 1.0:
            tp += 1
        elif p == 1.0 and y == 0.0:
            fp += 1
        elif p == 0.0 and y == 0.0:
            tn += 1
        else:
            fn += 1
    return tp, fp, tn, fn


def brier_score(labels, probs):
    """Mean squared error between predicted probability and actual 0/1
    outcome - lower is better calibrated. None if there are no samples."""
    if not labels:
        return None
    return sum((p - y) ** 2 for y, p in zip(labels, probs)) / len(labels)


def roc_auc(labels, scores):
    """ROC AUC via the Mann-Whitney U statistic (rank-based, ties get the
    average rank of their block) - equivalent to sklearn's roc_auc_score
    without needing sklearn. None if only one class is present (AUC is
    undefined, not 0.5, when there's nothing to rank against)."""
    n = len(labels)
    n_pos = sum(1 for y in labels if y == 1.0)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return None

    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and scores[order[j]] == scores[order[i]]:
            j += 1
        # 1-indexed average rank for this tied block (i..j-1 in sorted order)
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        i = j

    rank_sum_pos = sum(ranks[i] for i in range(n) if labels[i] == 1.0)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def average_precision(labels, scores):
    """PR-AUC, computed as average precision: sum over recall increments of
    precision-at-that-point (the standard step-function definition used by
    sklearn's average_precision_score), by sweeping thresholds from the
    highest score down. None if there are no positive labels (precision is
    undefined with nothing to find)."""
    n = len(labels)
    n_pos = sum(1 for y in labels if y == 1.0)
    if n_pos == 0:
        return None

    order = sorted(range(n), key=lambda i: -scores[i])
    tp = fp = 0
    prev_recall = 0.0
    ap = 0.0
    i = 0
    while i < n:
        j = i
        while j < n and scores[order[j]] == scores[order[i]]:
            j += 1
        block_pos = sum(1 for k in range(i, j) if labels[order[k]] == 1.0)
        block_neg = (j - i) - block_pos
        tp += block_pos
        fp += block_neg
        recall = tp / n_pos
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        ap += precision * (recall - prev_recall)
        prev_recall = recall
        i = j
    return ap


def _round_or_none(x, ndigits=4):
    return round(x, ndigits) if x is not None else None


def classification_report(labels, probs, threshold: float = 0.5) -> dict:
    """Full metric dict for one probability threshold: sample count,
    accuracy, balanced accuracy, precision, recall, specificity, F1,
    tp/fp/tn/fn, positive rate, majority-class baseline accuracy, Brier
    score, ROC AUC, and PR AUC (average precision). AUC/PR-AUC/Brier are
    threshold-independent (computed once from the raw probabilities) and
    repeated verbatim across every threshold's report for convenience."""
    n = len(labels)
    if n == 0:
        return {"n": 0}

    preds = [1.0 if p >= threshold else 0.0 for p in probs]
    tp, fp, tn, fn = confusion_counts(labels, preds)

    accuracy = (tp + tn) / n
    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    specificity = safe_div(tn, tn + fp)
    f1 = safe_div(2 * precision * recall, precision + recall) if (precision is not None and recall is not None and (precision + recall) > 0) else None
    balanced_accuracy = (recall + specificity) / 2 if (recall is not None and specificity is not None) else None
    positive_rate = sum(labels) / n
    majority_baseline_accuracy = max(positive_rate, 1 - positive_rate)

    return {
        "n": n,
        "threshold": threshold,
        "accuracy": _round_or_none(accuracy),
        "balanced_accuracy": _round_or_none(balanced_accuracy),
        "precision": _round_or_none(precision),
        "recall": _round_or_none(recall),
        "specificity": _round_or_none(specificity),
        "f1": _round_or_none(f1),
        "true_positive": tp, "false_positive": fp, "true_negative": tn, "false_negative": fn,
        "positive_rate": _round_or_none(positive_rate),
        "majority_baseline_accuracy": _round_or_none(majority_baseline_accuracy),
        "brier_score": _round_or_none(brier_score(labels, probs)),
        "roc_auc": _round_or_none(roc_auc(labels, probs)),
        "pr_auc": _round_or_none(average_precision(labels, probs)),
    }


def calibrate_marginal_threshold(labels, probs, grid=None) -> float:
    """The threshold maximizing balanced accuracy ((recall + specificity) /
    2) - a documented objective that doesn't reward always-predicting-the-
    majority-class the way raw accuracy can under class imbalance. `labels`
    and `probs` should be ONLY the calibration split (e.g. 2024+2025 for
    evaluation thresholds) - this function has no knowledge of any holdout
    and will happily overfit to whatever it's given, so it's the caller's
    job to keep the holdout out."""
    grid = grid if grid is not None else [i / 100 for i in range(5, 96)]
    best_th, best_bal = grid[0], -1.0
    for th in grid:
        preds = [1.0 if p >= th else 0.0 for p in probs]
        tp, fp, tn, fn = confusion_counts(labels, preds)
        rec = safe_div(tp, tp + fn) or 0.0
        spec = safe_div(tn, tn + fp) or 0.0
        bal = (rec + spec) / 2
        if bal > best_bal:
            best_bal, best_th = bal, th
    return best_th


def calibrate_good_threshold(labels, probs, target_precision: float = 0.75,
                              min_positive: int = 20, grid=None, marginal_threshold=None) -> float:
    """The lowest threshold meeting target_precision with at least
    min_positive predicted-positive samples (so the estimate isn't just a
    lucky handful of predictions) - "when it says GOOD, trust it
    target_precision of the time." Also guaranteed to land strictly above
    marginal_threshold when one is given (a GOOD tier at or below MARGINAL
    would be nonsensical) - falls back to marginal_threshold + 0.15 (capped
    at 0.9) if nothing on the grid clears both bars, or if the first
    qualifying threshold isn't actually above marginal_threshold. Falls
    back to 0.9 if no marginal_threshold was given either. Like
    calibrate_marginal_threshold, only pass the calibration split, never
    the holdout."""
    grid = grid if grid is not None else [i / 100 for i in range(5, 96)]
    found = None
    for th in grid:
        preds = [1.0 if p >= th else 0.0 for p in probs]
        tp, fp, tn, fn = confusion_counts(labels, preds)
        n_pos_pred = tp + fp
        prec = safe_div(tp, n_pos_pred)
        if prec is not None and prec >= target_precision and n_pos_pred >= min_positive:
            found = th
            break
    if found is not None and (marginal_threshold is None or found > marginal_threshold):
        return found
    if marginal_threshold is not None:
        return min(0.9, marginal_threshold + 0.15)
    return 0.9


def build_session_samples(dates, outcomes, probs, window_start_hour: int, window_end_hour: int):
    """Aggregates hourly (date, outcome, probability) rows into one
    session-level row per local calendar day, restricted to hours in
    [window_start_hour, window_end_hour]. `dates` are naive-local
    "YYYY-MM-DDTHH:MM..." strings (the same format used throughout this
    project for target/sample times).

    Aggregation rule (documented, not just implemented): a day's outcome is
    1.0 if AT LEAST ONE hour in the window was a rideable outcome - the
    practical question is "will there be at least one session today", not
    "is every individual hour classified correctly". A day's predicted
    probability is the MAXIMUM hourly probability in the window, matching
    the same logic ("if any hour looks promising, today looks promising").

    Returns (session_outcomes, session_probs, sorted_day_strings) - three
    parallel sequences ready to pass into classification_report."""
    by_day = {}
    for date, outcome, prob in zip(dates, outcomes, probs):
        hour = int(date[11:13])
        if not (window_start_hour <= hour <= window_end_hour):
            continue
        day = date[:10]
        entry = by_day.setdefault(day, {"outcome": 0.0, "prob": 0.0})
        entry["outcome"] = max(entry["outcome"], outcome)
        entry["prob"] = max(entry["prob"], prob)

    days = sorted(by_day)
    session_outcomes = [by_day[d]["outcome"] for d in days]
    session_probs = [by_day[d]["prob"] for d in days]
    return session_outcomes, session_probs, days
