"""Offline tests for research_metrics.py: correlation measures on toy
data, day-level (not row-level) bootstrap resampling, chronological
day-grouped rolling-origin split invariants (no day in both train and
validate, 2026 labeled 'reference' not 'holdout'), and Benjamini-Hochberg
FDR correction."""

import unittest

import research_metrics as rm


class CorrelationTests(unittest.TestCase):
    def test_perfect_positive_pearson(self):
        self.assertAlmostEqual(rm.pearson_correlation([1, 2, 3, 4], [1, 2, 3, 4]), 1.0)

    def test_perfect_negative_pearson(self):
        self.assertAlmostEqual(rm.pearson_correlation([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)

    def test_pearson_ignores_none_pairs(self):
        r = rm.pearson_correlation([1, 2, None, 4], [1, 2, 3, 4])
        self.assertIsNotNone(r)

    def test_pearson_none_with_insufficient_data(self):
        self.assertIsNone(rm.pearson_correlation([1], [1]))

    def test_pearson_none_with_zero_variance(self):
        self.assertIsNone(rm.pearson_correlation([1, 1, 1], [1, 2, 3]))

    def test_spearman_monotonic_nonlinear(self):
        r = rm.spearman_correlation([1, 2, 3, 4], [1, 4, 9, 16])
        self.assertAlmostEqual(r, 1.0)

    def test_point_biserial_matches_pearson_with_binary_labels(self):
        labels = [0, 0, 1, 1]
        xs = [1, 2, 3, 4]
        self.assertAlmostEqual(rm.point_biserial_correlation(labels, xs), rm.pearson_correlation(xs, labels))


class CoverageTests(unittest.TestCase):
    def test_full_coverage(self):
        self.assertEqual(rm.coverage_pct([1, 2, 3]), 1.0)

    def test_partial_coverage(self):
        self.assertAlmostEqual(rm.coverage_pct([1, None, 3, None]), 0.5)

    def test_empty_list_is_zero(self):
        self.assertEqual(rm.coverage_pct([]), 0.0)


class DayLevelBootstrapTests(unittest.TestCase):
    def test_resamples_whole_days_not_individual_rows(self):
        # 10 rows on day A (all value=0), 1 row on day B (value=100). A
        # row-level bootstrap would rarely draw enough of day B's single
        # row to move the mean much; a day-level bootstrap draws day B
        # as a WHOLE unit with the same probability as day A, so the CI
        # must be wide enough to sometimes include values near 100.
        days = ["A"] * 10 + ["B"]
        values = [0] * 10 + [100]
        ci = rm.bootstrap_ci_by_day(days, values, lambda vals: sum(vals) / len(vals), n_resamples=300, seed=7)
        self.assertGreater(ci[1], 10)  # upper bound reflects day B being resampled as a full unit

    def test_empty_days_returns_none(self):
        self.assertIsNone(rm.bootstrap_ci_by_day([], [], lambda v: 0))

    def test_multi_series_bootstrap_resamples_pairs_together(self):
        days = ["A", "A", "B", "B"]
        pairs = [(1, 1), (2, 2), (10, 10), (20, 20)]

        def stat(resampled):
            xs = [p[0] for p in resampled]
            ys = [p[1] for p in resampled]
            return rm.pearson_correlation(xs, ys)

        ci = rm.bootstrap_ci_by_day_multi(days, pairs, stat, n_resamples=100, seed=3)
        self.assertIsNotNone(ci)


class RollingOriginSplitTests(unittest.TestCase):
    def _samples(self):
        samples = []
        for year, month, day_range in [(2024, "05", range(1, 3)), (2024, "08", range(1, 3)),
                                        (2024, "09", range(1, 3)), (2025, "06", range(1, 3)),
                                        (2025, "08", range(1, 3)), (2025, "10", range(1, 3)),
                                        (2026, "07", range(1, 3))]:
            for day in day_range:
                for hour in (12, 15):
                    samples.append({"date": f"{year}-{month}-{day:02d}T{hour:02d}:00", "outcome": 1.0})
        return samples

    def test_no_day_appears_in_both_train_and_validate_within_any_fold(self):
        folds = rm.rolling_origin_splits(self._samples())
        self.assertTrue(folds)
        for fold in folds:
            train_days = {s["date"][:10] for s in fold["train"]}
            validate_days = {s["date"][:10] for s in fold["validate"]}
            self.assertFalse(train_days & validate_days, f"fold {fold['name']} leaks a day")

    def test_2026_fold_is_labeled_reference_not_holdout(self):
        folds = rm.rolling_origin_splits(self._samples())
        reference_folds = [f for f in folds if f["kind"] == "reference"]
        self.assertTrue(reference_folds)
        for f in reference_folds:
            self.assertNotIn("holdout", f["name"].lower())

    def test_folds_with_no_matching_data_are_excluded_not_erroring(self):
        # Only 2024 data - the 2025/2026 folds should simply not appear.
        samples = [{"date": "2024-05-01T12:00", "outcome": 1.0}, {"date": "2024-08-01T12:00", "outcome": 0.0}]
        folds = rm.rolling_origin_splits(samples)
        for f in folds:
            self.assertTrue(f["train"])
            self.assertTrue(f["validate"])

    def test_empty_input_returns_no_folds(self):
        self.assertEqual(rm.rolling_origin_splits([]), [])

    def test_custom_date_key(self):
        samples = [{"day": "2024-05-01T12:00", "outcome": 1.0}, {"day": "2024-08-01T12:00", "outcome": 0.0}]
        folds = rm.rolling_origin_splits(samples, date_key="day")
        self.assertTrue(any(f["train"] for f in folds))


class BenjaminiHochbergTests(unittest.TestCase):
    def test_all_significant_when_all_p_values_tiny(self):
        significance = rm.benjamini_hochberg([0.001, 0.002, 0.003], alpha=0.10)
        self.assertTrue(all(significance))

    def test_none_significant_when_all_p_values_large(self):
        significance = rm.benjamini_hochberg([0.9, 0.8, 0.95], alpha=0.10)
        self.assertFalse(any(significance))

    def test_empty_input(self):
        self.assertEqual(rm.benjamini_hochberg([]), [])

    def test_known_mixed_case(self):
        # classic textbook-style example: some clearly significant, some not
        p_values = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216, 0.222, 0.251, 0.319, 0.324, 0.5]
        significance = rm.benjamini_hochberg(p_values, alpha=0.05)
        self.assertTrue(significance[0])
        self.assertFalse(significance[-1])


class PValueApproxTests(unittest.TestCase):
    def test_strong_correlation_gives_small_p_value(self):
        p = rm.corr_to_p_value_approx(0.9, 100)
        self.assertLess(p, 0.05)

    def test_zero_correlation_gives_large_p_value(self):
        p = rm.corr_to_p_value_approx(0.01, 100)
        self.assertGreater(p, 0.5)

    def test_none_correlation_returns_one(self):
        self.assertEqual(rm.corr_to_p_value_approx(None, 100), 1.0)

    def test_small_n_returns_one(self):
        self.assertEqual(rm.corr_to_p_value_approx(0.9, 2), 1.0)


if __name__ == "__main__":
    unittest.main()
