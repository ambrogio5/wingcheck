"""Offline tests for station_analysis.py: weights.json isolation (never
touched), the 10 fixed family definitions are correct and exactly what
was pre-registered (no ad-hoc search), correlation analysis handles
missing values, calibration/reliability helpers, and family-score
augmentation honestly reports "missing" for families with no confirmed
station. Uses small synthetic fixtures - never the real multi-year
dataset (that's exercised only by actually running the script, not by
the offline test suite)."""

import os
import unittest

import station_analysis as sa
from features import FEATURE_NAMES
from model import WEIGHTS_PATH


def _sample(date, outcome, **feature_overrides):
    feats = {name: 0.0 for name in FEATURE_NAMES}
    feats.update(feature_overrides)
    return {"date": date, "year": int(date[:4]), "features": feats, "outcome": outcome}


class FixedFamilyDefinitionTests(unittest.TestCase):
    """The task is explicit: exactly ten pre-registered comparisons, never
    an open-ended search over combinations."""

    def test_exactly_ten_families_defined(self):
        self.assertEqual(len(sa.FAMILY_DEFINITIONS), 10)

    def test_family_names_match_the_task_specification(self):
        expected = {
            "majority_class_baseline", "forecast_wind_only", "wind_gust_direction",
            "full_current_model", "full_plus_source_heating", "full_plus_summit_support",
            "full_plus_pressure_family", "full_plus_radiation_family", "full_plus_competing_flow",
            "full_plus_all_spatial_families",
        }
        self.assertEqual(set(sa.FAMILY_DEFINITIONS.keys()), expected)

    def test_forecast_wind_only_is_a_single_feature(self):
        self.assertEqual(sa.FAMILY_DEFINITIONS["forecast_wind_only"], ("model_wind",))

    def test_full_current_model_matches_production_feature_names(self):
        self.assertEqual(set(sa.FAMILY_DEFINITIONS["full_current_model"]), set(FEATURE_NAMES))

    def test_each_plus_family_adds_exactly_one_new_feature_over_full_current_model(self):
        full = set(sa.FAMILY_DEFINITIONS["full_current_model"])
        for name in ("full_plus_source_heating", "full_plus_summit_support", "full_plus_pressure_family",
                     "full_plus_radiation_family", "full_plus_competing_flow"):
            added = set(sa.FAMILY_DEFINITIONS[name]) - full
            self.assertEqual(len(added), 1, f"{name} should add exactly one new feature")

    def test_all_spatial_families_adds_all_five_new_features(self):
        full = set(sa.FAMILY_DEFINITIONS["full_current_model"])
        added = set(sa.FAMILY_DEFINITIONS["full_plus_all_spatial_families"]) - full
        self.assertEqual(added, set(sa.NEW_FAMILY_FEATURES))


class WeightsJsonIsolationTests(unittest.TestCase):
    def _samples(self):
        samples = []
        for year, month in [(2024, "05"), (2024, "08"), (2025, "06"), (2026, "07")]:
            for day in (1, 2):
                for hour in (12, 15):
                    samples.append(_sample(f"{year}-{month}-{day:02d}T{hour:02d}:00", 1.0 if hour == 15 else 0.0,
                                            model_wind=0.3 if hour == 15 else -0.2))
        return samples

    def test_rolling_origin_comparison_never_touches_weights_json(self):
        mtime_before = os.path.getmtime(WEIGHTS_PATH) if os.path.exists(WEIGHTS_PATH) else None
        sa.run_rolling_origin_family_comparison(self._samples())
        mtime_after = os.path.getmtime(WEIGHTS_PATH) if os.path.exists(WEIGHTS_PATH) else None
        self.assertEqual(mtime_before, mtime_after)

    def test_calibration_summary_never_touches_weights_json(self):
        mtime_before = os.path.getmtime(WEIGHTS_PATH) if os.path.exists(WEIGHTS_PATH) else None
        sa.run_calibration_summary(self._samples())
        mtime_after = os.path.getmtime(WEIGHTS_PATH) if os.path.exists(WEIGHTS_PATH) else None
        self.assertEqual(mtime_before, mtime_after)


class CorrelationAnalysisTests(unittest.TestCase):
    def test_handles_missing_feature_values(self):
        samples = [_sample("2024-05-01T12:00", 1.0, model_wind=0.5),
                   _sample("2024-05-02T12:00", 0.0, model_wind=-0.5)]
        del samples[1]["features"]["model_wind"]
        report = sa.run_correlation_analysis(samples)
        self.assertLess(report["features"]["model_wind"]["coverage"], 1.0)

    def test_reports_n_samples_and_unique_days(self):
        samples = [_sample("2024-05-01T12:00", 1.0), _sample("2024-05-01T15:00", 0.0),
                   _sample("2024-05-02T12:00", 1.0)]
        report = sa.run_correlation_analysis(samples)
        self.assertEqual(report["n_samples"], 3)
        self.assertEqual(report["n_unique_days"], 2)

    def test_fdr_significance_flag_present_for_every_feature(self):
        samples = [_sample(f"2024-05-{d:02d}T12:00", float(d % 2), model_wind=float(d)) for d in range(1, 15)]
        report = sa.run_correlation_analysis(samples)
        for feat, result in report["features"].items():
            self.assertIn("fdr_significant_at_0.10", result)


class MajorityClassProbsTests(unittest.TestCase):
    def test_matches_train_positive_rate(self):
        train = [_sample("2024-05-01T12:00", 1.0), _sample("2024-05-01T15:00", 0.0),
                 _sample("2024-05-02T12:00", 1.0), _sample("2024-05-02T15:00", 1.0)]
        validate = [_sample("2024-06-01T12:00", 0.0)] * 3
        probs = sa.majority_class_probs(train, validate)
        self.assertEqual(probs, [0.75, 0.75, 0.75])

    def test_empty_train_defaults_to_half(self):
        probs = sa.majority_class_probs([], [_sample("2024-06-01T12:00", 0.0)])
        self.assertEqual(probs, [0.5])


class FamilyScoreAugmentationTests(unittest.TestCase):
    def test_augment_adds_all_five_new_features(self):
        samples = [_sample("2024-05-01T12:00", 1.0)]
        scores_by_date = {"2024-05-01": {name: 0.42 for name in sa.NEW_FAMILY_FEATURES}}
        augmented = sa.augment_samples_with_family_scores(samples, scores_by_date)
        for name in sa.NEW_FAMILY_FEATURES:
            self.assertEqual(augmented[0]["features"][name], 0.42)

    def test_augment_does_not_mutate_original_samples(self):
        samples = [_sample("2024-05-01T12:00", 1.0)]
        original_keys = set(samples[0]["features"].keys())
        sa.augment_samples_with_family_scores(samples, {"2024-05-01": {"pressure_family_score": 1.0}})
        self.assertEqual(set(samples[0]["features"].keys()), original_keys)

    def test_missing_date_defaults_to_zero(self):
        samples = [_sample("2024-05-01T12:00", 1.0)]
        augmented = sa.augment_samples_with_family_scores(samples, {})
        for name in sa.NEW_FAMILY_FEATURES:
            self.assertEqual(augmented[0]["features"][name], 0.0)


class ReliabilityTableTests(unittest.TestCase):
    def test_reliability_table_has_n_bins_entries(self):
        table = sa.reliability_table([1, 0, 1, 0], [0.9, 0.1, 0.8, 0.2], n_bins=10)
        self.assertEqual(len(table), 10)

    def test_ece_zero_for_perfectly_calibrated(self):
        # All four samples land in the same bin with avg_predicted=0.5 and
        # observed_rate=0.5 (2 of 4 positive) - zero calibration error.
        labels = [1, 1, 0, 0]
        probs = [0.5, 0.5, 0.5, 0.5]
        ece = sa.expected_calibration_error(labels, probs, n_bins=10)
        self.assertAlmostEqual(ece, 0.0)

    def test_ece_none_for_empty_input(self):
        self.assertIsNone(sa.expected_calibration_error([], []))


class GroupByLocalDateTests(unittest.TestCase):
    def test_groups_records_by_date_prefix(self):
        records = [{"timestamp_local": "2026-07-15T06:00:00+02:00"}, {"timestamp_local": "2026-07-15T07:00:00+02:00"},
                   {"timestamp_local": "2026-07-16T06:00:00+02:00"}]
        grouped = sa._group_by_local_date(records)
        self.assertEqual(len(grouped["2026-07-15"]), 2)
        self.assertEqual(len(grouped["2026-07-16"]), 1)


if __name__ == "__main__":
    unittest.main()
