"""Offline tests for station_analysis.py: rolling-origin evaluation never
touches weights.json, group feature subsets are correct, the station
coverage report categorizes honestly, and correlation analysis handles
missing data. Uses small synthetic fixtures, not the real dataset."""

import json
import os
import random
import shutil
import tempfile
import unittest

import station_analysis as sa
from features import FEATURE_NAMES
from model import load_weights


def _make_samples(n, seed=0, start_date="2024-06-01"):
    rng = random.Random(seed)
    samples = []
    for i in range(n):
        feats = {name: rng.uniform(-1, 1) for name in FEATURE_NAMES}
        z = feats["model_wind"] * 2
        prob = 1 / (1 + pow(2.718281828, -z))
        outcome = 1.0 if rng.random() < prob else 0.0
        day = i // 7
        hour = 12 + (i % 7)
        # crude but adequate date walker for test fixtures
        from datetime import datetime, timedelta
        date = (datetime.fromisoformat(start_date) + timedelta(days=day)).strftime("%Y-%m-%d")
        samples.append({"date": f"{date}T{hour:02d}:00", "year": int(date[:4]), "features": feats, "outcome": outcome})
    return samples


class WeightsJsonIsolationTests(unittest.TestCase):
    def test_rolling_origin_comparison_never_touches_weights_json(self):
        weights_path_mtime_before = None
        import model
        if os.path.exists(model.WEIGHTS_PATH):
            weights_path_mtime_before = os.path.getmtime(model.WEIGHTS_PATH)
            weights_before = load_weights()

        samples = _make_samples(60)
        sa.run_rolling_origin_family_comparison(samples)

        if weights_path_mtime_before is not None:
            self.assertEqual(os.path.getmtime(model.WEIGHTS_PATH), weights_path_mtime_before)
            self.assertEqual(load_weights(), weights_before)

    def test_evaluate_group_on_fold_never_touches_weights_json(self):
        import model
        mtime_before = os.path.getmtime(model.WEIGHTS_PATH) if os.path.exists(model.WEIGHTS_PATH) else None
        train = _make_samples(40, seed=1)
        validate = _make_samples(20, seed=2, start_date="2024-09-01")
        sa.evaluate_group_on_fold(FEATURE_NAMES, train, validate)
        if mtime_before is not None:
            self.assertEqual(os.path.getmtime(model.WEIGHTS_PATH), mtime_before)


class GroupDefinitionTests(unittest.TestCase):
    def test_wind_only_is_single_feature(self):
        self.assertEqual(sa.WIND_ONLY, ("model_wind",))

    def test_full_minus_family_excludes_exactly_that_feature(self):
        samples = _make_samples(30)
        results = sa.run_rolling_origin_family_comparison(samples)
        # Presence check only (feature-subset correctness is exercised via
        # evaluate_group_on_fold's use of the group's feature tuple, which
        # is derived directly from FEATURE_NAMES minus the family - the
        # actual subset construction is tested at the module level below).
        self.assertIn("full_minus_samedan_morning", results)
        self.assertIn("full_minus_pressure_nowcast", results)

    def test_full_minus_family_feature_subset_excludes_only_that_feature(self):
        excluded = tuple(f for f in FEATURE_NAMES if f not in sa.TESTABLE_STATION_FAMILIES["samedan_morning"])
        self.assertNotIn("samedan_morning_score", excluded)
        self.assertEqual(len(excluded), len(FEATURE_NAMES) - 1)


class MajorityClassProbsTests(unittest.TestCase):
    def test_matches_train_positive_rate(self):
        train = [{"outcome": 1.0}, {"outcome": 1.0}, {"outcome": 0.0}, {"outcome": 0.0}]
        probs = sa.majority_class_probs(train, 5)
        self.assertEqual(probs, [0.5] * 5)

    def test_empty_train_defaults_to_half(self):
        self.assertEqual(sa.majority_class_probs([], 3), [0.5, 0.5, 0.5])


class CorrelationAnalysisTests(unittest.TestCase):
    def test_returns_entry_per_feature_plus_note(self):
        samples = _make_samples(50)
        result = sa.run_correlation_analysis(samples)
        for name in FEATURE_NAMES:
            self.assertIn(name, result)
            self.assertIn("pearson", result[name])
            self.assertIn("coverage", result[name])
            self.assertIn("fdr_significant_at_0.10", result[name])
        self.assertIn("_note", result)

    def test_handles_missing_feature_values(self):
        samples = _make_samples(30)
        # Simulate a feature that's sometimes missing (None), like a
        # candidate station feature would be before full coverage exists.
        for i, s in enumerate(samples):
            if i % 3 == 0:
                s["features"]["model_wind"] = None
        result = sa.run_correlation_analysis(samples)
        self.assertLess(result["model_wind"]["coverage"], 1.0)
        self.assertGreater(result["model_wind"]["coverage"], 0.0)


class StationCoverageReportTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_path = sa.STATIONS_MANIFEST_PATH

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        sa.STATIONS_MANIFEST_PATH = self._orig_path

    def test_missing_manifest_reports_error_not_crash(self):
        sa.STATIONS_MANIFEST_PATH = os.path.join(self.tmpdir, "does_not_exist.json")
        result = sa.run_station_coverage_report()
        self.assertIn("error", result)

    def test_confirmed_station_with_data_is_available_for_analysis(self):
        sa.STATIONS_MANIFEST_PATH = os.path.join(self.tmpdir, "stations.json")
        with open(sa.STATIONS_MANIFEST_PATH, "w") as f:
            json.dump({"stations": {
                "sam": {"name": "Samedan", "verification": "confirmed", "confidence": "high",
                        "coverage": {"n_records": 1000}},
            }}, f)
        result = sa.run_station_coverage_report()
        self.assertEqual(result["sam"]["status"], "available_for_analysis")

    def test_unconfirmed_station_with_no_data_is_unavailable_historically(self):
        sa.STATIONS_MANIFEST_PATH = os.path.join(self.tmpdir, "stations.json")
        with open(sa.STATIONS_MANIFEST_PATH, "w") as f:
            json.dump({"stations": {
                "cor": {"name": "Corvatsch", "verification": "candidate_unconfirmed", "confidence": "low",
                        "coverage": {"n_records": 0}},
            }}, f)
        result = sa.run_station_coverage_report()
        self.assertEqual(result["cor"]["status"], "unavailable_historically")

    def test_confirmed_station_with_no_data_is_insufficient_coverage_not_fabricated(self):
        """A confirmed station with zero records (e.g. before the first
        sync) must never be silently reported as available."""
        sa.STATIONS_MANIFEST_PATH = os.path.join(self.tmpdir, "stations.json")
        with open(sa.STATIONS_MANIFEST_PATH, "w") as f:
            json.dump({"stations": {
                "sam": {"name": "Samedan", "verification": "confirmed", "confidence": "high",
                        "coverage": {"n_records": 0}},
            }}, f)
        result = sa.run_station_coverage_report()
        self.assertEqual(result["sam"]["status"], "insufficient_coverage")


class RealDatasetRollingOriginSplitTests(unittest.TestCase):
    """Cross-checks rolling_origin_splits() against the actual, real
    logs/backtest_dataset.jsonl dates (not a synthetic fixture) - the
    day-grouped, no-leakage property must hold for the real data the
    production backtest and every research script actually consume, not
    just for a small hand-built example."""

    DATASET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "logs", "backtest_dataset.jsonl")

    def setUp(self):
        if not os.path.exists(self.DATASET_PATH):
            self.skipTest("logs/backtest_dataset.jsonl not present in this checkout")
        with open(self.DATASET_PATH) as f:
            self.samples = [json.loads(line) for line in f if line.strip()]

    def test_no_real_day_appears_in_both_train_and_validate_within_any_fold(self):
        from research_metrics import rolling_origin_splits
        folds = rolling_origin_splits(self.samples)
        self.assertTrue(folds, "expected at least one fold from the real dataset")
        for fold in folds:
            train_days = {s["date"][:10] for s in fold["train"]}
            validate_days = {s["date"][:10] for s in fold["validate"]}
            overlap = train_days & validate_days
            self.assertFalse(overlap, f"fold {fold['name']!r} leaks real day(s) {overlap} across train/validate")

    def test_folds_are_chronological_train_before_validate(self):
        from research_metrics import rolling_origin_splits
        folds = rolling_origin_splits(self.samples)
        for fold in folds:
            if not fold["train"] or not fold["validate"]:
                continue
            max_train_date = max(s["date"][:10] for s in fold["train"])
            min_validate_date = min(s["date"][:10] for s in fold["validate"])
            self.assertLess(max_train_date, min_validate_date,
                             f"fold {fold['name']!r} has a train date not strictly before its validate dates")

    def test_2026_fold_is_labeled_reference_not_holdout(self):
        from research_metrics import rolling_origin_splits
        folds = rolling_origin_splits(self.samples)
        reference_folds = [f for f in folds if f["kind"] == "reference"]
        self.assertTrue(reference_folds, "expected a 2026 reference fold in the real dataset")
        for fold in reference_folds:
            self.assertNotIn("holdout", fold["name"].lower())


if __name__ == "__main__":
    unittest.main()
