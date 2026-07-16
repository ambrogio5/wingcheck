"""Offline tests for continuous_target_analysis.py: linear regression
correctness/determinism, regression metrics on known toy examples, daily
session dataset construction (aggregation rules), and that no research
function here ever touches weights.json. Synthetic fixtures only."""

import os
import random
import unittest
from datetime import datetime, timedelta

import continuous_target_analysis as cta
from features import FEATURE_NAMES


def _make_samples(n, seed=0, start_date="2024-06-01"):
    rng = random.Random(seed)
    samples = []
    for i in range(n):
        feats = {name: rng.uniform(-1, 1) for name in FEATURE_NAMES}
        wind_kt = 10 + feats["model_wind"] * 5 + rng.uniform(-1, 1)
        outcome = 1.0 if wind_kt >= 10 else 0.0
        day = i // 5
        hour = 12 + (i % 5)
        date = (datetime.fromisoformat(start_date) + timedelta(days=day)).strftime("%Y-%m-%d")
        samples.append({
            "date": f"{date}T{hour:02d}:00", "year": int(date[:4]), "features": feats,
            "outcome": outcome, "actual_wind_kt": round(wind_kt, 2), "actual_gust_kt": round(wind_kt * 1.4, 2),
        })
    return samples


class LinearRegressionTests(unittest.TestCase):
    def test_deterministic_given_seed(self):
        samples = [{"features": {"x": v}, "target": 2 * v + 1} for v in [1, 2, 3, 4, 5]]
        m1 = cta.train_linear_regression(samples, ("x",), epochs=50, seed=1)
        m2 = cta.train_linear_regression(samples, ("x",), epochs=50, seed=1)
        self.assertEqual(m1, m2)

    def test_learns_approximately_correct_slope(self):
        rng = random.Random(0)
        samples = [{"features": {"x": v}, "target": 3 * v - 2} for v in [rng.uniform(-5, 5) for _ in range(200)]]
        model = cta.train_linear_regression(samples, ("x",), epochs=300, learning_rate=0.01, seed=2)
        self.assertAlmostEqual(model["weights"]["x"], 3.0, delta=0.3)
        self.assertAlmostEqual(model["bias"], -2.0, delta=0.5)

    def test_empty_samples_returns_zero_model(self):
        model = cta.train_linear_regression([], ("x",), epochs=10)
        self.assertEqual(model["bias"], 0.0)
        self.assertEqual(model["weights"]["x"], 0.0)

    def test_does_not_mutate_input(self):
        samples = [{"features": {"x": 1.0}, "target": 2.0}]
        original = [dict(s) for s in samples]
        cta.train_linear_regression(samples, ("x",), epochs=5)
        self.assertEqual(samples, original)


class RegressionMetricsTests(unittest.TestCase):
    def test_perfect_prediction_zero_error(self):
        m = cta._regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        self.assertEqual(m["mae"], 0.0)
        self.assertEqual(m["rmse"], 0.0)
        self.assertEqual(m["bias"], 0.0)
        self.assertAlmostEqual(m["rank_correlation"], 1.0)

    def test_known_mae_rmse(self):
        actual = [0.0, 0.0]
        predicted = [3.0, 4.0]
        m = cta._regression_metrics(actual, predicted)
        self.assertAlmostEqual(m["mae"], 3.5)
        self.assertAlmostEqual(m["rmse"], (3.0 ** 2 / 2 + 4.0 ** 2 / 2) ** 0.5, places=3)

    def test_positive_bias_means_overprediction(self):
        m = cta._regression_metrics([5.0, 5.0], [7.0, 7.0])
        self.assertEqual(m["bias"], 2.0)

    def test_empty_input(self):
        self.assertEqual(cta._regression_metrics([], []), {"n": 0})


class DailySessionDatasetTests(unittest.TestCase):
    def test_any_rideable_and_counts(self):
        samples = [
            {"date": "2026-07-01T12:00", "outcome": 0.0, "actual_wind_kt": 5.0, "actual_gust_kt": 8.0,
             "features": {name: 0.1 for name in FEATURE_NAMES}},
            {"date": "2026-07-01T14:00", "outcome": 1.0, "actual_wind_kt": 12.0, "actual_gust_kt": 18.0,
             "features": {name: 0.5 for name in FEATURE_NAMES}},
            {"date": "2026-07-01T16:00", "outcome": 1.0, "actual_wind_kt": 14.0, "actual_gust_kt": 20.0,
             "features": {name: 0.9 for name in FEATURE_NAMES}},
        ]
        rows = cta.build_daily_session_dataset(samples)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["any_rideable"], 1.0)
        self.assertEqual(r["n_rideable_hours"], 2)
        self.assertEqual(r["max_wind_kt"], 14.0)
        self.assertEqual(r["first_rideable_hour"], 14)
        self.assertEqual(r["last_rideable_hour"], 16)
        self.assertEqual(r["session_duration_hours"], 3)  # 16-14+1

    def test_no_rideable_hours(self):
        samples = [{"date": "2026-07-01T12:00", "outcome": 0.0, "actual_wind_kt": 3.0, "actual_gust_kt": 5.0,
                    "features": {name: 0.0 for name in FEATURE_NAMES}}]
        rows = cta.build_daily_session_dataset(samples)
        self.assertEqual(rows[0]["any_rideable"], 0.0)
        self.assertIsNone(rows[0]["first_rideable_hour"])
        self.assertEqual(rows[0]["session_duration_hours"], 0)

    def test_features_are_max_aggregated(self):
        samples = [
            {"date": "2026-07-01T12:00", "outcome": 0.0, "actual_wind_kt": 5.0, "actual_gust_kt": 8.0,
             "features": {**{name: 0.1 for name in FEATURE_NAMES}, "model_wind": 0.2}},
            {"date": "2026-07-01T14:00", "outcome": 1.0, "actual_wind_kt": 12.0, "actual_gust_kt": 18.0,
             "features": {**{name: 0.1 for name in FEATURE_NAMES}, "model_wind": 0.9}},
        ]
        rows = cta.build_daily_session_dataset(samples)
        self.assertEqual(rows[0]["features"]["model_wind"], 0.9)

    def test_multiple_days_produce_multiple_rows(self):
        samples = _make_samples(20)
        rows = cta.build_daily_session_dataset(samples)
        dates = {r["date"] for r in rows}
        self.assertGreater(len(dates), 1)


class WeightsJsonIsolationTests(unittest.TestCase):
    def test_daily_session_analysis_never_touches_weights_json(self):
        import model
        mtime_before = os.path.getmtime(model.WEIGHTS_PATH) if os.path.exists(model.WEIGHTS_PATH) else None
        samples = _make_samples(120, seed=3)
        cta.run_daily_session_analysis(samples)
        if mtime_before is not None:
            self.assertEqual(os.path.getmtime(model.WEIGHTS_PATH), mtime_before)

    def test_continuous_wind_analysis_never_touches_weights_json(self):
        import model
        mtime_before = os.path.getmtime(model.WEIGHTS_PATH) if os.path.exists(model.WEIGHTS_PATH) else None
        samples = _make_samples(120, seed=4)
        cta.run_continuous_wind_analysis(samples)
        if mtime_before is not None:
            self.assertEqual(os.path.getmtime(model.WEIGHTS_PATH), mtime_before)


if __name__ == "__main__":
    unittest.main()
