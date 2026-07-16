"""Offline tests for model_regularized.py: standardization fits only on
training data, missing-value indicators work, L2 regularization shrinks
weights, training is deterministic, and convergence diagnostics behave
sensibly. Synthetic data only, no network."""

import random
import unittest

from model_regularized import (
    standardize_fit, standardize_apply, train_l2_logistic, score_l2_logistic, fit_and_score,
)

FEATURES = ("x1", "x2")


def _linear_samples(n, seed, coef=(2.0, -1.0)):
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        x1, x2 = rng.uniform(-3, 3), rng.uniform(-3, 3)
        z = coef[0] * x1 + coef[1] * x2
        p = 1 / (1 + pow(2.718281828, -z))
        y = 1.0 if rng.random() < p else 0.0
        samples.append({"features": {"x1": x1, "x2": x2}, "outcome": y})
    return samples


class StandardizationTests(unittest.TestCase):
    def test_fit_computes_mean_and_std_from_given_samples(self):
        samples = [{"features": {"x1": 1.0}}, {"features": {"x1": 3.0}}]
        stats = standardize_fit(samples, ("x1",))
        self.assertAlmostEqual(stats["x1"]["mean"], 2.0)
        self.assertAlmostEqual(stats["x1"]["std"], 1.0)

    def test_constant_feature_gets_std_one_not_zero_division(self):
        samples = [{"features": {"x1": 5.0}}, {"features": {"x1": 5.0}}]
        stats = standardize_fit(samples, ("x1",))
        self.assertEqual(stats["x1"]["std"], 1.0)

    def test_missing_values_excluded_from_mean_std_computation(self):
        samples = [{"features": {"x1": 1.0}}, {"features": {"x1": None}}, {"features": {"x1": 3.0}}]
        stats = standardize_fit(samples, ("x1",))
        self.assertAlmostEqual(stats["x1"]["mean"], 2.0)  # only 1.0 and 3.0 count

    def test_apply_produces_missing_indicator(self):
        stats = {"x1": {"mean": 0.0, "std": 1.0}}
        out = standardize_apply({"x1": None}, ("x1",), stats)
        self.assertEqual(out["x1__missing"], 1.0)
        self.assertEqual(out["x1"], 0.0)

    def test_apply_standardizes_present_value(self):
        stats = {"x1": {"mean": 10.0, "std": 2.0}}
        out = standardize_apply({"x1": 14.0}, ("x1",), stats)
        self.assertAlmostEqual(out["x1"], 2.0)  # (14-10)/2
        self.assertEqual(out["x1__missing"], 0.0)

    def test_standardization_does_not_use_validation_data(self):
        """The core leakage guard: stats must come ONLY from what's passed
        to standardize_fit - calling it with just the training split must
        give different (and correct) results than if validation data had
        been included."""
        train = [{"features": {"x1": 1.0}}, {"features": {"x1": 3.0}}]
        train_plus_validate = train + [{"features": {"x1": 100.0}}]
        stats_train_only = standardize_fit(train, ("x1",))
        stats_leaked = standardize_fit(train_plus_validate, ("x1",))
        self.assertNotEqual(stats_train_only["x1"]["mean"], stats_leaked["x1"]["mean"])
        self.assertAlmostEqual(stats_train_only["x1"]["mean"], 2.0)


class TrainL2LogisticTests(unittest.TestCase):
    def test_deterministic_given_seed(self):
        samples = _linear_samples(100, seed=1)
        stats = standardize_fit(samples, FEATURES)
        std_samples = [{"features": standardize_apply(s["features"], FEATURES, stats), "outcome": s["outcome"]}
                       for s in samples]
        m1, _ = train_l2_logistic(std_samples, FEATURES, l2=0.1, epochs=30, seed=42)
        m2, _ = train_l2_logistic(std_samples, FEATURES, l2=0.1, epochs=30, seed=42)
        self.assertEqual(m1, m2)

    def test_does_not_mutate_input_samples(self):
        samples = _linear_samples(20, seed=1)
        stats = standardize_fit(samples, FEATURES)
        std_samples = [{"features": standardize_apply(s["features"], FEATURES, stats), "outcome": s["outcome"]}
                       for s in samples]
        original = [dict(s) for s in std_samples]
        train_l2_logistic(std_samples, FEATURES, epochs=10, seed=1)
        self.assertEqual(std_samples, original)

    def test_higher_l2_shrinks_weight_magnitude(self):
        samples = _linear_samples(300, seed=3)
        stats = standardize_fit(samples, FEATURES)
        std_samples = [{"features": standardize_apply(s["features"], FEATURES, stats), "outcome": s["outcome"]}
                       for s in samples]
        m_low, _ = train_l2_logistic(std_samples, FEATURES, l2=0.001, epochs=80, seed=5)
        m_high, _ = train_l2_logistic(std_samples, FEATURES, l2=5.0, epochs=80, seed=5)
        mag_low = sum(abs(v) for v in m_low["weights"].values())
        mag_high = sum(abs(v) for v in m_high["weights"].values())
        self.assertLess(mag_high, mag_low)

    def test_empty_samples_returns_zero_model_not_crash(self):
        model, diagnostics = train_l2_logistic([], FEATURES, epochs=10)
        self.assertEqual(model["bias"], 0.0)
        self.assertFalse(diagnostics["converged"])

    def test_learns_correct_sign_of_coefficients(self):
        samples = _linear_samples(400, seed=7, coef=(2.0, -1.0))
        stats = standardize_fit(samples, FEATURES)
        std_samples = [{"features": standardize_apply(s["features"], FEATURES, stats), "outcome": s["outcome"]}
                       for s in samples]
        model, _ = train_l2_logistic(std_samples, FEATURES, l2=0.01, epochs=150, seed=9)
        self.assertGreater(model["weights"]["x1"], 0)
        self.assertLess(model["weights"]["x2"], 0)


class FitAndScoreTests(unittest.TestCase):
    def test_end_to_end_reasonable_auc_on_separable_data(self):
        train = _linear_samples(300, seed=1)
        validate = _linear_samples(100, seed=2)
        result = fit_and_score(train, validate, FEATURES, l2=0.01, epochs=100, seed=11)
        from metrics import roc_auc
        auc = roc_auc(result["validate_labels"], result["validate_probs"])
        self.assertGreater(auc, 0.8)

    def test_missing_feature_in_validation_does_not_crash(self):
        train = _linear_samples(50, seed=1)
        validate = [{"features": {"x1": None, "x2": 1.0}, "outcome": 1.0}]
        result = fit_and_score(train, validate, FEATURES, epochs=20)
        self.assertEqual(len(result["validate_probs"]), 1)
        self.assertTrue(0.0 <= result["validate_probs"][0] <= 1.0)


if __name__ == "__main__":
    unittest.main()
