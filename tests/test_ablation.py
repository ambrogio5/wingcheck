"""Offline tests for ablation.py's feature-group definitions and
run_ablation(). No network calls - synthetic samples only."""

import random
import unittest

import ablation
from features import FEATURE_NAMES


def _toy_samples(n, seed):
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        feats = {name: rng.uniform(-1, 1) for name in FEATURE_NAMES}
        z = feats["model_wind"] * 2
        prob = 1 / (1 + pow(2.718281828, -z))
        outcome = 1.0 if rng.random() < prob else 0.0
        samples.append({"features": feats, "outcome": outcome})
    return samples


class GroupDefinitionTests(unittest.TestCase):
    def test_seven_groups_defined(self):
        self.assertEqual(len(ablation.ABLATION_GROUPS), 7)

    def test_full_minus_pressure_nowcast_excludes_exactly_that_feature(self):
        excluded = set(FEATURE_NAMES) - set(ablation.FULL_MINUS_PRESSURE_NOWCAST)
        self.assertEqual(excluded, {"pressure_nowcast_score"})

    def test_full_minus_samedan_morning_excludes_exactly_that_feature(self):
        excluded = set(FEATURE_NAMES) - set(ablation.FULL_MINUS_SAMEDAN_MORNING)
        self.assertEqual(excluded, {"samedan_morning_score"})

    def test_core_physical_features_is_a_strict_subset(self):
        self.assertTrue(set(ablation.CORE_PHYSICAL_FEATURES) < set(FEATURE_NAMES))
        self.assertEqual(len(ablation.CORE_PHYSICAL_FEATURES), 8)

    def test_wind_only_is_single_feature(self):
        self.assertEqual(ablation.WIND_ONLY_FEATURES, ("model_wind",))

    def test_majority_class_group_has_no_features(self):
        group = ablation.ABLATION_GROUPS[0]
        self.assertEqual(group.name, "majority_class_baseline")
        self.assertIsNone(group.features)

    def test_full_current_model_group_uses_every_feature(self):
        full_group = next(g for g in ablation.ABLATION_GROUPS if g.name == "full_current_model")
        self.assertEqual(set(full_group.features), set(FEATURE_NAMES))


class RunAblationTests(unittest.TestCase):
    def test_returns_one_result_per_group_with_correct_feature_counts(self):
        train = _toy_samples(120, seed=1)
        holdout = _toy_samples(60, seed=2)
        results = ablation.run_ablation(train, holdout, epochs=5, seed=3)
        self.assertEqual(len(results), len(ablation.ABLATION_GROUPS))
        by_name = {r["name"]: r for r in results}
        self.assertEqual(by_name["majority_class_baseline"]["n_features"], 0)
        self.assertEqual(by_name["forecast_wind_only"]["n_features"], 1)
        self.assertEqual(by_name["wind_gust_direction"]["n_features"], 3)
        self.assertEqual(by_name["previous_core_physical_set"]["n_features"], 8)
        self.assertEqual(by_name["full_current_model"]["n_features"], len(FEATURE_NAMES))
        self.assertEqual(by_name["full_minus_pressure_nowcast"]["n_features"], len(FEATURE_NAMES) - 1)
        self.assertEqual(by_name["full_minus_samedan_morning"]["n_features"], len(FEATURE_NAMES) - 1)

    def test_majority_class_baseline_has_no_discriminative_power(self):
        train = _toy_samples(120, seed=1)
        holdout = _toy_samples(60, seed=2)
        results = ablation.run_ablation(train, holdout, epochs=5, seed=3)
        baseline = next(r for r in results if r["name"] == "majority_class_baseline")
        self.assertEqual(baseline["roc_auc"], 0.5)

    def test_groups_are_trained_independently(self):
        """Each group must train its own fresh model - one group's result
        must not depend on another group having (not) run first."""
        train = _toy_samples(80, seed=5)
        holdout = _toy_samples(40, seed=6)
        results_full_run = ablation.run_ablation(train, holdout, epochs=4, seed=9)

        # Re-running just the full-feature group in isolation (via new_weights
        # + train_epochs directly) must reproduce the same metrics as when it
        # ran alongside six other groups in run_ablation.
        from model import new_weights, train_epochs, score
        from metrics import classification_report
        w = train_epochs(new_weights(), train, epochs=4, seed=9)
        holdout_labels = [s["outcome"] for s in holdout]
        holdout_probs = [score(s["features"], w) for s in holdout]
        solo_report = classification_report(holdout_labels, holdout_probs, threshold=0.5)

        full_result = next(r for r in results_full_run if r["name"] == "full_current_model")
        self.assertEqual(full_result["accuracy"], solo_report["accuracy"])
        self.assertEqual(full_result["roc_auc"], solo_report["roc_auc"])


if __name__ == "__main__":
    unittest.main()
