"""Offline tests for model.py's fresh-model construction, schema validation,
and deterministic batch training. No network calls, no real weights.json
access - everything here builds its own in-memory weights dicts."""

import unittest

from features import FEATURE_NAMES
import model


class NewWeightsTests(unittest.TestCase):
    def test_fresh_defaults(self):
        w = model.new_weights()
        self.assertEqual(w["trained_samples"], 0)
        self.assertEqual(w["bias"], model.DEFAULT_BIAS)
        self.assertEqual(w["version"], model.SCHEMA_VERSION)
        self.assertEqual(w["tier_thresholds"], model.DEFAULT_TIER_THRESHOLDS)
        self.assertEqual(set(w["weights"]), set(FEATURE_NAMES))
        self.assertTrue(all(v == 0.0 for v in w["weights"].values()))

    def test_subset_schema(self):
        subset = ("model_wind", "model_gust")
        w = model.new_weights(subset)
        self.assertEqual(set(w["weights"]), set(subset))

    def test_independent_objects(self):
        a = model.new_weights()
        b = model.new_weights()
        self.assertIsNot(a, b)
        self.assertIsNot(a["weights"], b["weights"])
        self.assertIsNot(a["tier_thresholds"], b["tier_thresholds"])

        a["weights"]["model_wind"] = 5.0
        a["bias"] = 99.0
        a["tier_thresholds"]["good"] = 0.99
        self.assertEqual(b["weights"]["model_wind"], 0.0)
        self.assertEqual(b["bias"], model.DEFAULT_BIAS)
        self.assertEqual(b["tier_thresholds"]["good"], model.DEFAULT_TIER_THRESHOLDS["good"])


class ValidateSchemaTests(unittest.TestCase):
    def test_matching_schema_ok(self):
        w = model.new_weights()
        model.validate_schema(w)  # must not raise

    def test_missing_feature_raises(self):
        w = model.new_weights()
        del w["weights"][FEATURE_NAMES[0]]
        with self.assertRaises(ValueError):
            model.validate_schema(w)

    def test_extra_feature_raises(self):
        w = model.new_weights()
        w["weights"]["not_a_real_feature"] = 0.0
        with self.assertRaises(ValueError):
            model.validate_schema(w)

    def test_subset_schema_validates_against_same_subset(self):
        subset = ("model_wind", "model_gust")
        w = model.new_weights(subset)
        model.validate_schema(w, feature_names=subset)  # must not raise
        with self.assertRaises(ValueError):
            model.validate_schema(w)  # against the full FEATURE_NAMES, must fail


def _toy_samples(n, seed=1):
    """Deterministic synthetic samples, all real FEATURE_NAMES set, with an
    outcome correlated to model_wind so training actually moves the weights."""
    import random
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        feats = {name: rng.uniform(-1, 1) for name in FEATURE_NAMES}
        z = feats["model_wind"] * 2
        prob = 1 / (1 + pow(2.718281828, -z))
        outcome = 1.0 if rng.random() < prob else 0.0
        samples.append({"features": feats, "outcome": outcome})
    return samples


class TrainEpochsTests(unittest.TestCase):
    def test_deterministic_same_seed(self):
        samples = _toy_samples(60)
        w1 = model.train_epochs(model.new_weights(), samples, epochs=5, seed=42)
        w2 = model.train_epochs(model.new_weights(), samples, epochs=5, seed=42)
        self.assertEqual(w1["bias"], w2["bias"])
        self.assertEqual(w1["weights"], w2["weights"])

    def test_different_seed_can_differ(self):
        samples = _toy_samples(60)
        w1 = model.train_epochs(model.new_weights(), samples, epochs=5, seed=1)
        w2 = model.train_epochs(model.new_weights(), samples, epochs=5, seed=2)
        # Not a strict guarantee for all data, but true for this fixture -
        # shuffle order affects the online-GD trajectory.
        self.assertNotEqual(w1["weights"], w2["weights"])

    def test_does_not_mutate_caller_sample_list(self):
        samples = _toy_samples(20)
        original_order = list(samples)
        model.train_epochs(model.new_weights(), samples, epochs=3, seed=7)
        self.assertEqual(samples, original_order)

    def test_trained_samples_reflects_this_call_not_cumulative(self):
        samples = _toy_samples(15)
        w = model.new_weights()
        w["trained_samples"] = 9999  # simulate a model loaded with stale state
        w = model.train_epochs(w, samples, epochs=2, seed=3)
        self.assertEqual(w["trained_samples"], len(samples))

    def test_fresh_model_unaffected_by_a_previously_trained_ones_weights(self):
        """Two independently-built new_weights() models trained on
        DIFFERENT data must not share any mutable state - this is the
        property that makes the evaluation/deployment split in backtest.py
        safe."""
        train_a = _toy_samples(40, seed=11)
        train_b = _toy_samples(40, seed=22)
        eval_model = model.train_epochs(model.new_weights(), train_a, epochs=5, seed=42)
        deploy_model = model.train_epochs(model.new_weights(), train_a + train_b, epochs=5, seed=42)
        # Retraining eval_model's data through a fresh model must reproduce
        # eval_model exactly, proving deploy_model's construction didn't
        # reach back and mutate anything eval_model depended on.
        eval_again = model.train_epochs(model.new_weights(), train_a, epochs=5, seed=42)
        self.assertEqual(eval_model["weights"], eval_again["weights"])
        self.assertEqual(eval_model["bias"], eval_again["bias"])


if __name__ == "__main__":
    unittest.main()
