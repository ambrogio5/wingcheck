"""
research_metrics.py - statistical building blocks shared by station_analysis.py:
correlation measures, day-level bootstrap confidence intervals, chronological
day-grouped rolling-origin (expanding-window) evaluation splits, and
Benjamini-Hochberg FDR correction for multiple comparisons.

Every function here is pure/stateless and network-free - safe to unit test
with small synthetic fixtures.
"""

import math
import random


def pearson_correlation(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 2:
        return None
    xs2, ys2 = zip(*pairs)
    n = len(xs2)
    mean_x, mean_y = sum(xs2) / n, sum(ys2) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs2, ys2))
    var_x = sum((x - mean_x) ** 2 for x in xs2)
    var_y = sum((y - mean_y) ** 2 for y in ys2)
    if var_x == 0 or var_y == 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman_correlation(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 2:
        return None
    xs2, ys2 = zip(*pairs)
    return pearson_correlation(_rank(xs2), _rank(ys2))


def point_biserial_correlation(binary_labels, xs):
    """binary_labels: 0/1 outcomes; xs: a continuous feature."""
    return pearson_correlation(xs, binary_labels)


def coverage_pct(values):
    if not values:
        return 0.0
    return sum(1 for v in values if v is not None) / len(values)


def bootstrap_ci_by_day(day_keys, values, statistic_fn, n_resamples=500, ci=0.90, seed=1234):
    """Day-level (not row-level) bootstrap: resamples whole days with
    replacement, then recomputes statistic_fn over every row belonging to
    the resampled days - so within-day correlation between rows doesn't
    understate the true uncertainty."""
    rng = random.Random(seed)
    unique_days = sorted(set(day_keys))
    if not unique_days:
        return None
    by_day = {}
    for d, v in zip(day_keys, values):
        by_day.setdefault(d, []).append(v)

    stats = []
    for _ in range(n_resamples):
        sample_days = [rng.choice(unique_days) for _ in unique_days]
        resampled = [v for d in sample_days for v in by_day[d]]
        stat = statistic_fn(resampled)
        if stat is not None:
            stats.append(stat)
    if not stats:
        return None
    stats.sort()
    lo_idx = int((1 - ci) / 2 * len(stats))
    hi_idx = int((1 - (1 - ci) / 2) * len(stats)) - 1
    hi_idx = min(hi_idx, len(stats) - 1)
    return [stats[lo_idx], stats[hi_idx]]


def bootstrap_ci_by_day_multi(day_keys, value_tuples, statistic_fn, n_resamples=500, ci=0.90, seed=1234):
    """Same as bootstrap_ci_by_day but for a statistic needing multiple
    parallel series resampled together (e.g. correlation of feature vs
    outcome - both must be resampled from the SAME days, in lockstep)."""
    rng = random.Random(seed)
    unique_days = sorted(set(day_keys))
    if not unique_days:
        return None
    by_day = {}
    for d, tup in zip(day_keys, value_tuples):
        by_day.setdefault(d, []).append(tup)

    stats = []
    for _ in range(n_resamples):
        sample_days = [rng.choice(unique_days) for _ in unique_days]
        resampled = [tup for d in sample_days for tup in by_day[d]]
        stat = statistic_fn(resampled)
        if stat is not None:
            stats.append(stat)
    if not stats:
        return None
    stats.sort()
    lo_idx = int((1 - ci) / 2 * len(stats))
    hi_idx = int((1 - (1 - ci) / 2) * len(stats)) - 1
    hi_idx = min(hi_idx, len(stats) - 1)
    return [stats[lo_idx], stats[hi_idx]]


def rolling_origin_splits(samples, date_key="date"):
    """Chronological, day-grouped expanding-window folds. Never puts the
    same calendar day in both train and validate. 2026 is reported
    separately with kind='reference' (not 'holdout') since station/feature
    research inspects it repeatedly across many comparisons - see
    docs/STATION_RESEARCH.md and CLAUDE.md's 2026-reuse warning."""
    def day_of(s):
        return s[date_key][:10]

    def year_of(s):
        return int(s[date_key][:4])

    def month_of(s):
        return s[date_key][5:7]

    fold_specs = [
        ("2024-08_validate", lambda s: year_of(s) == 2024 and day_of(s) < "2024-08-01",
         lambda s: year_of(s) == 2024 and month_of(s) == "08", "rolling"),
        ("2024-09_validate", lambda s: year_of(s) == 2024 and day_of(s) < "2024-09-01",
         lambda s: year_of(s) == 2024 and month_of(s) == "09", "rolling"),
        ("2025-05_06_validate", lambda s: year_of(s) == 2024,
         lambda s: year_of(s) == 2025 and month_of(s) in ("05", "06"), "rolling"),
        ("2025-07_08_validate", lambda s: year_of(s) == 2024 or (year_of(s) == 2025 and month_of(s) in ("05", "06")),
         lambda s: year_of(s) == 2025 and month_of(s) in ("07", "08"), "rolling"),
        ("2025-09_10_validate", lambda s: year_of(s) == 2024 or (year_of(s) == 2025 and month_of(s) in ("05", "06", "07", "08")),
         lambda s: year_of(s) == 2025 and month_of(s) in ("09", "10"), "rolling"),
        ("2026_reference", lambda s: year_of(s) in (2024, 2025),
         lambda s: year_of(s) == 2026, "reference"),
    ]

    folds = []
    for name, train_pred, val_pred, kind in fold_specs:
        train = [s for s in samples if train_pred(s)]
        validate = [s for s in samples if val_pred(s)]
        if not train or not validate:
            continue
        train_days = {day_of(s) for s in train}
        validate_days = {day_of(s) for s in validate}
        assert not (train_days & validate_days), f"fold {name} leaks day(s) across train/validate"
        folds.append({"name": name, "kind": kind, "train": train, "validate": validate})
    return folds


def benjamini_hochberg(p_values, alpha=0.10):
    """Returns a list of booleans (same order as input) marking which
    p-values are significant after Benjamini-Hochberg FDR correction."""
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(range(n), key=lambda i: p_values[i])
    significant = [False] * n
    max_k = 0
    for rank, i in enumerate(indexed, start=1):
        threshold = (rank / n) * alpha
        if p_values[i] <= threshold:
            max_k = rank
    for rank, i in enumerate(indexed, start=1):
        if rank <= max_k:
            significant[i] = True
    return significant


def corr_to_p_value_approx(r, n):
    """Approximate two-sided p-value for a Pearson/Spearman correlation via
    the standard t-approximation - good enough for a diagnostic FDR pass,
    not a precise inferential claim."""
    if r is None or n < 3 or abs(r) >= 1:
        return 1.0 if r is not None else 1.0
    t = r * math.sqrt((n - 2) / (1 - r * r))
    # Two-sided p-value from a t-distribution approximated via a normal
    # tail for simplicity (n is large enough in this project's datasets
    # that the approximation error is negligible for a diagnostic FDR pass).
    p = 2 * (1 - _normal_cdf(abs(t)))
    return max(0.0, min(1.0, p))


def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
