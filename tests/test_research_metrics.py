"""Offline tests for research_metrics.py: correlation functions on known
toy examples, day-level (not row-level) bootstrap resampling, chronological
rolling-origin splits with no day appearing in both train and validation,
and multiple-comparison (FDR) control."""

import unittest

import research_metrics as rm


class CorrelationTests(unittest.TestCase):
    def test_pearson_perfect_positive(self):
        self.assertAlmostEqual(rm.pearson_correlation([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)

    def test_pearson_perfect_negative(self):
        self.assertAlmostEqual(rm.pearson_correlation([1, 2, 3, 4], [8, 6, 4, 2]), -1.0)

    def test_pearson_constant_series_is_none(self):
        self.assertIsNone(rm.pearson_correlation([1, 1, 1, 1], [1, 2, 3, 4]))

    def test_pearson_empty_is_none(self):
        self.assertIsNone(rm.pearson_correlation([], []))

    def test_spearman_perfect_monotonic_nonlinear(self):
        # Nonlinear but perfectly monotonic - Spearman should be 1.0 even
        # though Pearson on the raw values would be less than 1.0.
        xs = [1, 2, 3, 4, 5]
        ys = [1, 4, 9, 16, 25]
        self.assertAlmostEqual(rm.spearman_correlation(xs, ys), 1.0)

    def test_point_biserial_matches_pearson(self):
        binary = [0.0, 0.0, 1.0, 1.0]
        continuous = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(
            rm.point_biserial_correlation(binary, continuous),
            rm.pearson_correlation(continuous, binary),
        )

    def test_coverage_pct(self):
        self.assertEqual(rm.coverage_pct([1, None, 2, None, 3]), 0.6)

    def test_coverage_pct_empty(self):
        self.assertEqual(rm.coverage_pct([]), 0.0)

    def test_correlation_report_excludes_missing_but_counts_coverage(self):
        features = [1.0, None, 2.0, 3.0, None]
        outcomes = [1.0, 0.0, 1.0, 0.0, 1.0]
        report = rm.correlation_report(features, outcomes)
        self.assertEqual(report["n_used"], 3)
        self.assertEqual(report["coverage"], 0.6)
        self.assertIsNotNone(report["pearson"])

    def test_correlation_report_all_missing(self):
        report = rm.correlation_report([None, None], [1.0, 0.0])
        self.assertEqual(report["coverage"], 0.0)
        self.assertIsNone(report["pearson"])


class DayLevelBootstrapTests(unittest.TestCase):
    def test_resamples_whole_days_not_individual_rows(self):
        # 2 days, one with 10 rows all value=1.0, one with 1 row value=0.0.
        # If resampling were row-level, the huge day would almost always
        # dominate every resample and the CI would be extremely tight
        # around 1.0. Day-level resampling must show real spread, because
        # roughly half the resamples should draw the day with value=0.0
        # much more equally (each day has an equal 50% chance of being
        # picked per draw, regardless of its row count).
        day_ids = ["a"] * 10 + ["b"] * 1
        values = [1.0] * 10 + [0.0] * 1
        point, lo, hi = rm.bootstrap_ci_by_day(day_ids, values, statistic_fn=lambda vs: sum(vs) / len(vs),
                                                 n_resamples=300, seed=1)
        self.assertAlmostEqual(point, 10 / 11, places=3)
        # With day-level resampling, some resamples draw "b" twice (or
        # zero times), producing means far from the point estimate -
        # confirm the CI has real width, not degenerate to a point.
        self.assertLess(lo, point)
        self.assertGreater(hi, lo)

    def test_empty_input_returns_none_triplet(self):
        self.assertEqual(rm.bootstrap_ci_by_day([], [], lambda vs: 0), (None, None, None))

    def test_deterministic_given_seed(self):
        day_ids = ["a", "a", "b", "b", "c"]
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        r1 = rm.bootstrap_ci_by_day(day_ids, values, lambda vs: sum(vs) / len(vs), n_resamples=50, seed=7)
        r2 = rm.bootstrap_ci_by_day(day_ids, values, lambda vs: sum(vs) / len(vs), n_resamples=50, seed=7)
        self.assertEqual(r1, r2)


class MultiSeriesDayLevelBootstrapTests(unittest.TestCase):
    def test_resamples_paired_series_together(self):
        day_ids = ["a", "a", "b", "b", "c", "c"]
        xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]  # ys = 2*xs, perfect correlation
        point, lo, hi = rm.bootstrap_ci_by_day_multi(
            day_ids, [xs, ys], statistic_fn=rm.pearson_correlation, n_resamples=100, seed=3)
        self.assertAlmostEqual(point, 1.0, places=6)
        # Any day-consistent resample of a perfectly linear relationship
        # is still perfectly linear, so the CI should stay at/near 1.0.
        self.assertGreater(lo, 0.9)

    def test_empty_input(self):
        result = rm.bootstrap_ci_by_day_multi([], [[], []], rm.pearson_correlation)
        self.assertEqual(result, (None, None, None))

    def test_deterministic_given_seed(self):
        day_ids = ["a", "a", "b", "b", "c"]
        xs = [1.0, 2.0, 3.0, 1.0, 2.0]
        ys = [0.0, 1.0, 1.0, 0.0, 1.0]
        r1 = rm.bootstrap_ci_by_day_multi(day_ids, [xs, ys], rm.pearson_correlation, n_resamples=50, seed=9)
        r2 = rm.bootstrap_ci_by_day_multi(day_ids, [xs, ys], rm.pearson_correlation, n_resamples=50, seed=9)
        self.assertEqual(r1, r2)


class RollingOriginSplitTests(unittest.TestCase):
    def _make_samples(self, dates):
        return [{"date": d} for d in dates]

    def test_no_day_appears_in_both_train_and_validate_within_any_fold(self):
        dates = [
            "2024-06-15T14:00", "2024-08-20T14:00", "2024-09-10T14:00",
            "2025-05-15T14:00", "2025-07-20T14:00", "2025-09-15T14:00",
            "2026-06-01T14:00",
        ]
        samples = self._make_samples(dates)
        folds = rm.rolling_origin_splits(samples)
        for fold in folds:
            train_days = {s["date"][:10] for s in fold["train"]}
            validate_days = {s["date"][:10] for s in fold["validate"]}
            self.assertEqual(train_days & validate_days, set(),
                              f"fold {fold['name']} has overlapping days between train and validate")

    def test_folds_are_chronological_train_before_validate(self):
        dates = ["2024-06-01T14:00", "2024-08-15T14:00", "2024-09-15T14:00", "2026-06-01T14:00"]
        samples = self._make_samples(dates)
        folds = rm.rolling_origin_splits(samples)
        for fold in folds:
            max_train_date = max(s["date"][:10] for s in fold["train"])
            min_validate_date = min(s["date"][:10] for s in fold["validate"])
            self.assertLess(max_train_date, min_validate_date,
                             f"fold {fold['name']}: train must chronologically precede validate")

    def test_2026_fold_is_labeled_reference_not_holdout(self):
        samples = self._make_samples(["2024-06-01T14:00", "2026-06-01T14:00"])
        folds = rm.rolling_origin_splits(samples)
        ref_folds = [f for f in folds if f["kind"] == "reference"]
        self.assertTrue(ref_folds)
        for f in ref_folds:
            self.assertIn("2026", f["name"])

    def test_empty_samples_produce_no_folds(self):
        self.assertEqual(rm.rolling_origin_splits([]), [])

    def test_folds_missing_data_are_excluded_not_erroring(self):
        # Only 2026 data - none of the 2024/2025 rolling folds have any
        # train/validate rows, so they must be silently excluded, not
        # returned as empty (which would look like "0% accuracy on 0 samples").
        samples = self._make_samples(["2026-06-01T14:00", "2026-07-01T14:00"])
        folds = rm.rolling_origin_splits(samples)
        for f in folds:
            self.assertTrue(f["train"])
            self.assertTrue(f["validate"])


class MultipleComparisonTests(unittest.TestCase):
    def test_benjamini_hochberg_known_case(self):
        # Classic textbook-style example: small p-values should survive,
        # large ones should not, at a reasonable FDR level.
        p_values = [0.001, 0.008, 0.039, 0.041, 0.5, 0.9]
        result = rm.benjamini_hochberg(p_values, alpha=0.05)
        self.assertTrue(result[0])   # smallest p-value should survive
        self.assertFalse(result[-1])  # largest should not

    def test_none_p_values_are_never_significant(self):
        result = rm.benjamini_hochberg([0.001, None, 0.9], alpha=0.10)
        self.assertEqual(result[1], False)

    def test_empty_input(self):
        self.assertEqual(rm.benjamini_hochberg([]), [])

    def test_all_none_input(self):
        self.assertEqual(rm.benjamini_hochberg([None, None]), [False, False])

    def test_corr_to_p_value_strong_correlation_is_small(self):
        p = rm.corr_to_p_value_approx(0.9, 100)
        self.assertLess(p, 0.01)

    def test_corr_to_p_value_zero_correlation_is_large(self):
        p = rm.corr_to_p_value_approx(0.0, 100)
        self.assertGreater(p, 0.5)

    def test_corr_to_p_value_none_correlation(self):
        self.assertEqual(rm.corr_to_p_value_approx(None, 100), 1.0)


if __name__ == "__main__":
    unittest.main()
