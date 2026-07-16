"""
research_metrics.py - statistical helpers for station_analysis.py and
friends that don't belong in metrics.py (which is scoped to classifier
evaluation: accuracy/AUC/calibration at a threshold). This module covers
correlation, resampling-based uncertainty, chronological/rolling-origin
splitting, and multiple-comparison control - the machinery Phase 5/6
needs to screen candidate station features without either fabricating
precision or repeatedly mining the same 2026 holdout unrestricted.

Pure stdlib (no numpy/scipy), consistent with this project's lightweight,
no-heavy-dependency philosophy. Every function operates on plain lists.
"""

import math
import random


def _mean(xs):
    return sum(xs) / len(xs)


def pearson_correlation(xs: list, ys: list):
    """Standard Pearson product-moment correlation. None if either series
    is constant (undefined) or the inputs are empty/mismatched."""
    n = len(xs)
    if n == 0 or n != len(ys) or n < 2:
        return None
    mx, my = _mean(xs), _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    varx = sum((x - mx) ** 2 for x in xs)
    vary = sum((y - my) ** 2 for y in ys)
    if varx == 0 or vary == 0:
        return None
    return cov / math.sqrt(varx * vary)


def _rank(xs: list) -> list:
    """Average ranks (1-indexed, ties share the mean rank of their block)."""
    n = len(xs)
    order = sorted(range(n), key=lambda i: xs[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and xs[order[j]] == xs[order[i]]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        i = j
    return ranks


def spearman_correlation(xs: list, ys: list):
    """Spearman rank correlation - Pearson correlation of the ranks. None
    under the same degenerate conditions as pearson_correlation."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    return pearson_correlation(_rank(xs), _rank(ys))


def point_biserial_correlation(binary_ys: list, continuous_xs: list):
    """Correlation between a continuous variable and a binary (0.0/1.0)
    outcome - mathematically identical to Pearson correlation, exposed
    under this name because that's the standard term when one side is
    binary, and because callers finding it under this name is expected."""
    return pearson_correlation(continuous_xs, binary_ys)


def coverage_pct(values: list) -> float:
    """Fraction of `values` that are not None - the honesty check every
    candidate feature must report alongside its correlation."""
    if not values:
        return 0.0
    return sum(1 for v in values if v is not None) / len(values)


def _fill_or_drop(pairs):
    """Drops (x, y) pairs where either side is None - correlation
    functions above have no notion of missingness, so callers must filter
    first; this is the shared filter every call site in this module uses."""
    return [(x, y) for x, y in pairs if x is not None and y is not None]


def correlation_report(feature_values: list, outcomes: list) -> dict:
    """One feature's full correlation diagnostic: Pearson, Spearman,
    point-biserial (same as Pearson here, kept for naming clarity), and
    coverage. Missing (None) feature values are excluded from the
    correlation computation but still counted in coverage."""
    coverage = coverage_pct(feature_values)
    pairs = _fill_or_drop(list(zip(feature_values, outcomes)))
    if len(pairs) < 2:
        return {"pearson": None, "spearman": None, "point_biserial": None,
                "coverage": coverage, "n_used": len(pairs)}
    xs, ys = zip(*pairs)
    return {
        "pearson": pearson_correlation(list(xs), list(ys)),
        "spearman": spearman_correlation(list(xs), list(ys)),
        "point_biserial": point_biserial_correlation(list(ys), list(xs)),
        "coverage": coverage,
        "n_used": len(pairs),
    }


# ---------------------------------------------------------------------------
# Day-level bootstrap - the project's samples are hourly but strongly
# correlated within a day, so resampling individual hours overstates
# precision (a handful of "easy" days would get counted as hundreds of
# independent votes). Resample whole days instead.
# ---------------------------------------------------------------------------

def bootstrap_ci_by_day(day_ids: list, values: list, statistic_fn, n_resamples: int = 500,
                         seed: int = 20260716, alpha: float = 0.05):
    """Resamples whole days (with replacement) `n_resamples` times, applies
    `statistic_fn(resampled_values)` each time, and returns
    (point_estimate, ci_low, ci_high) using the percentile method.

    day_ids/values are parallel sequences (one entry per original sample,
    e.g. per hour); a resampled dataset is built by drawing len(unique
    days) day IDs with replacement and taking ALL of that day's original
    rows each time a day is drawn - this is what keeps a resample's
    "sample size" honest instead of pretending 3000 hourly rows are 3000
    independent days."""
    by_day = {}
    for d, v in zip(day_ids, values):
        by_day.setdefault(d, []).append(v)
    days = list(by_day)
    if not days:
        return None, None, None

    point_estimate = statistic_fn(values)
    rng = random.Random(seed)
    stats = []
    for _ in range(n_resamples):
        drawn_days = [rng.choice(days) for _ in days]
        resampled = [v for d in drawn_days for v in by_day[d]]
        try:
            stats.append(statistic_fn(resampled))
        except (ZeroDivisionError, ValueError):
            continue
    stats = [s for s in stats if s is not None]
    if not stats:
        return point_estimate, None, None
    stats.sort()
    lo_idx = int(len(stats) * (alpha / 2))
    hi_idx = min(len(stats) - 1, int(len(stats) * (1 - alpha / 2)))
    return point_estimate, stats[lo_idx], stats[hi_idx]


def bootstrap_ci_by_day_multi(day_ids: list, value_lists: list, statistic_fn, n_resamples: int = 500,
                               seed: int = 20260716, alpha: float = 0.05):
    """Like bootstrap_ci_by_day, but resamples MULTIPLE parallel series
    together (e.g. a feature's values and the matching outcomes) so a
    statistic that needs both (like a correlation) is computed from
    genuinely paired, day-consistently-resampled data. `value_lists` is a
    list of equal-length sequences, all aligned with day_ids; `statistic_fn`
    is called as statistic_fn(*resampled_lists)."""
    by_day = {}
    for i, d in enumerate(day_ids):
        by_day.setdefault(d, []).append(i)
    days = list(by_day)
    if not days:
        return None, None, None

    def _apply(indices):
        return statistic_fn(*[[vl[i] for i in indices] for vl in value_lists])

    all_indices = list(range(len(day_ids)))
    point_estimate = _apply(all_indices)

    rng = random.Random(seed)
    stats = []
    for _ in range(n_resamples):
        drawn_days = [rng.choice(days) for _ in days]
        indices = [i for d in drawn_days for i in by_day[d]]
        try:
            result = _apply(indices)
        except (ZeroDivisionError, ValueError):
            continue
        if result is not None:
            stats.append(result)
    if not stats:
        return point_estimate, None, None
    stats.sort()
    lo_idx = int(len(stats) * (alpha / 2))
    hi_idx = min(len(stats) - 1, int(len(stats) * (1 - alpha / 2)))
    return point_estimate, stats[lo_idx], stats[hi_idx]


# ---------------------------------------------------------------------------
# Rolling-origin (expanding-window) chronological splits
# ---------------------------------------------------------------------------

def rolling_origin_splits(samples: list, date_key="date") -> list:
    """Builds a fixed, documented set of expanding-window chronological
    folds over `samples` (each a dict with a date_key of the form
    "YYYY-MM-DDTHH:MM..."). NEVER splits within a day, and never puts a
    day in both a fold's train and validation set.

    Folds (matching Phase 6.1's suggested structure):
      1. train May-Jul 2024, validate Aug 2024
      2. train May-Aug 2024, validate Sep 2024
      3. train 2024, validate May-Jun 2025
      4. train 2024 + May-Jun 2025, validate Jul-Aug 2025
      5. train 2024 + May-Aug 2025, validate Sep-Oct 2025
      6. train 2024+2025, retain 2026 as the FINAL REFERENCE evaluation -
         labeled "reference", not "holdout", since 2026 has already been
         inspected repeatedly by earlier work in this project (see
         docs/DATA_ARCHITECTURE.md's "2026 holdout reuse" warning) and can
         no longer be treated as a pristine untouched test set.

    Returns a list of {"name", "kind" ("rolling" or "reference"),
    "train": [...], "validate": [...]} dicts. Samples with a date outside
    all defined ranges are simply not included in that fold (not an error).
    """
    def _in_range(sample, start, end):
        d = sample[date_key][:10]
        return start <= d <= end

    def _select(start, end):
        return [s for s in samples if _in_range(s, start, end)]

    folds = [
        {"name": "2024-08_validate", "kind": "rolling",
         "train": _select("2024-05-01", "2024-07-31"), "validate": _select("2024-08-01", "2024-08-31")},
        {"name": "2024-09_validate", "kind": "rolling",
         "train": _select("2024-05-01", "2024-08-31"), "validate": _select("2024-09-01", "2024-09-30")},
        {"name": "2025-05_06_validate", "kind": "rolling",
         "train": _select("2024-05-01", "2024-10-31"), "validate": _select("2025-05-01", "2025-06-30")},
        {"name": "2025-07_08_validate", "kind": "rolling",
         "train": _select("2024-05-01", "2024-10-31") + _select("2025-05-01", "2025-06-30"),
         "validate": _select("2025-07-01", "2025-08-31")},
        {"name": "2025-09_10_validate", "kind": "rolling",
         "train": _select("2024-05-01", "2024-10-31") + _select("2025-05-01", "2025-08-31"),
         "validate": _select("2025-09-01", "2025-10-31")},
        {"name": "2026_reference", "kind": "reference",
         "train": _select("2024-05-01", "2025-10-31"), "validate": _select("2026-01-01", "2026-12-31")},
    ]
    return [f for f in folds if f["train"] and f["validate"]]


# ---------------------------------------------------------------------------
# Multiple-comparison control
# ---------------------------------------------------------------------------

def benjamini_hochberg(p_values: list, alpha: float = 0.10) -> list:
    """Benjamini-Hochberg false-discovery-rate correction. Returns a list
    of booleans (same order as p_values) - True where the finding survives
    at the given FDR level. None entries in p_values are never significant
    (pass through as False) rather than raising, so callers can pass a
    feature list where some correlations were undefined (e.g. zero
    coverage) without special-casing them first."""
    n = len(p_values)
    indexed = [(p, i) for i, p in enumerate(p_values) if p is not None]
    indexed.sort()
    m = len(indexed)
    significant = [False] * n
    if m == 0:
        return significant
    # Largest k such that p_(k) <= (k/m) * alpha; everything at or below
    # that rank is significant.
    largest_k = 0
    for rank, (p, _) in enumerate(indexed, start=1):
        if p <= (rank / m) * alpha:
            largest_k = rank
    for rank in range(largest_k):
        _, original_i = indexed[rank]
        significant[original_i] = True
    return significant


def corr_to_p_value_approx(r, n) -> float:
    """Approximate two-sided p-value for a Pearson/Spearman correlation
    coefficient via the standard t-distribution approximation
    (t = r*sqrt((n-2)/(1-r^2))), using a normal-approximation of the
    t-distribution's tail (adequate for the sample sizes here, n > 30) -
    deliberately simple rather than importing scipy for one distribution."""
    if r is None or n < 3 or abs(r) >= 1.0:
        return 1.0 if r is None else 0.0
    t = r * math.sqrt((n - 2) / (1 - r ** 2))
    # Normal-approximation two-sided tail probability.
    z = abs(t)
    p = math.erfc(z / math.sqrt(2))
    return max(0.0, min(1.0, p))
