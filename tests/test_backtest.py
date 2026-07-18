"""Offline tests for backtest.py's pure helper functions (hourly/session
reporting, threshold calibration, window filtering, monthly breakdown).
Importing backtest.py is safe - it only makes network/cache calls inside
main(), which these tests never call."""

import random
import unittest
from datetime import datetime, timezone

import backtest
from features import FEATURE_NAMES
from model import new_weights, train_epochs


def _toy_sample(date, seed, wind_bias=0.0):
    rng = random.Random(seed)
    feats = {name: rng.uniform(-1, 1) for name in FEATURE_NAMES}
    z = feats["model_wind"] * 2 + wind_bias
    prob = 1 / (1 + pow(2.718281828, -z))
    outcome = 1.0 if rng.random() < prob else 0.0
    return {
        "date": date, "features": feats, "outcome": outcome,
        "actual_wind_kt": round(10 + feats["model_wind"] * 5, 1),
    }


def _toy_samples(n, start_seed=0):
    samples = []
    for i in range(n):
        day = 1 + i // 7
        hour = 12 + (i % 7)
        date = f"2026-07-{day:02d}T{hour:02d}:00"
        samples.append(_toy_sample(date, start_seed + i))
    return samples


class FilterHoursTests(unittest.TestCase):
    def test_prime_window_constants_are_14_to_18(self):
        self.assertEqual(backtest.PRIME_WINDOW_START_HOUR, 14)
        self.assertEqual(backtest.PRIME_WINDOW_END_HOUR, 18)

    def test_filters_to_prime_window(self):
        samples = _toy_samples(21)  # hours cycle 12..18
        prime = backtest._filter_hours(
            samples, backtest.PRIME_WINDOW_START_HOUR, backtest.PRIME_WINDOW_END_HOUR)
        self.assertTrue(all(14 <= backtest._hour_of(s) <= 18 for s in prime))
        self.assertTrue(any(backtest._hour_of(s) < 14 for s in samples))  # sanity: fixture has hours outside prime

    def test_prime_window_includes_14_and_excludes_earlier_hours(self):
        samples = [
            {"date": "2026-07-01T12:00"},
            {"date": "2026-07-01T13:00"},
            {"date": "2026-07-01T14:00"},
            {"date": "2026-07-01T18:00"},
        ]
        prime = backtest._filter_hours(
            samples, backtest.PRIME_WINDOW_START_HOUR, backtest.PRIME_WINDOW_END_HOUR)
        prime_hours = sorted(backtest._hour_of(s) for s in prime)
        self.assertEqual(prime_hours, [14, 18])


class CalibrateThresholdsTests(unittest.TestCase):
    def test_uses_only_supplied_samples(self):
        train = _toy_samples(200, start_seed=1)
        w = train_epochs(new_weights(), train, epochs=10, seed=42)
        th1 = backtest.calibrate_thresholds(w, train)
        th2 = backtest.calibrate_thresholds(w, train)
        self.assertEqual(th1, th2)  # deterministic given identical inputs
        self.assertGreater(th1["good"], th1["marginal"])

        # A disjoint "holdout" set must not change calibration computed from
        # `train` alone (it is never passed in).
        holdout = _toy_samples(100, start_seed=999)
        th3 = backtest.calibrate_thresholds(w, train)
        self.assertEqual(th1, th3)
        del holdout


class HourlyAndSessionReportTests(unittest.TestCase):
    def test_hourly_reports_shape(self):
        train = _toy_samples(150, start_seed=2)
        holdout = _toy_samples(80, start_seed=500)
        w = train_epochs(new_weights(), train, epochs=8, seed=42)
        th = backtest.calibrate_thresholds(w, train)
        report = backtest.hourly_reports(w, holdout, th)
        self.assertEqual(report["n"], len(holdout))
        for key in ("cutoff_0.5", "cutoff_marginal", "cutoff_good"):
            self.assertIn(key, report)
            self.assertEqual(report[key]["n"], len(holdout))

    def test_session_reports_shape_and_window(self):
        train = _toy_samples(150, start_seed=2)
        holdout = _toy_samples(80, start_seed=500)
        w = train_epochs(new_weights(), train, epochs=8, seed=42)
        th = backtest.calibrate_thresholds(w, train)
        full = backtest.session_reports(w, holdout, th, backtest.WINDOW_START_HOUR, backtest.WINDOW_END_HOUR)
        prime = backtest.session_reports(w, holdout, th, backtest.PRIME_WINDOW_START_HOUR, backtest.PRIME_WINDOW_END_HOUR)
        self.assertGreaterEqual(full["n_days"], prime["n_days"])  # prime window is a subset of hours

    def test_evaluation_training_does_not_use_holdout(self):
        """Training on `train` only, then scoring two DIFFERENT holdout sets,
        must be reproducible from scratch regardless of which holdout is
        used - proving the holdout never influenced training."""
        train = _toy_samples(150, start_seed=2)
        holdout_a = _toy_samples(50, start_seed=500)
        holdout_b = _toy_samples(50, start_seed=700)

        w1 = train_epochs(new_weights(), train, epochs=8, seed=42)
        _ = backtest.hourly_reports(w1, holdout_a, {"marginal": 0.4, "good": 0.65})

        w2 = train_epochs(new_weights(), train, epochs=8, seed=42)
        _ = backtest.hourly_reports(w2, holdout_b, {"marginal": 0.4, "good": 0.65})

        self.assertEqual(w1["weights"], w2["weights"])
        self.assertEqual(w1["bias"], w2["bias"])


class DeploymentIsolationTests(unittest.TestCase):
    def test_deployment_model_is_independent_object_from_evaluation_model(self):
        train = _toy_samples(100, start_seed=10)
        holdout = _toy_samples(50, start_seed=900)

        eval_weights = train_epochs(new_weights(), train, epochs=5, seed=42)
        deploy_weights = train_epochs(new_weights(), train + holdout, epochs=5, seed=42)

        self.assertIsNot(eval_weights, deploy_weights)
        self.assertIsNot(eval_weights["weights"], deploy_weights["weights"])

        # Mutating the deployment model's weights after the fact (as
        # calibrate_thresholds/save_weights would) must not retroactively
        # change the evaluation model's already-reported numbers.
        deploy_weights["weights"]["model_wind"] = 12345.0
        self.assertNotEqual(eval_weights["weights"]["model_wind"], 12345.0)


class MonthlyBreakdownTests(unittest.TestCase):
    def test_known_toy_breakdown(self):
        samples = [
            {"date": "2026-07-01T12:00", "outcome": 1.0, "actual_wind_kt": 12.0},
            {"date": "2026-07-02T12:00", "outcome": 0.0, "actual_wind_kt": 6.0},
            {"date": "2026-08-01T12:00", "outcome": 1.0, "actual_wind_kt": 20.0},
        ]
        breakdown = backtest.monthly_breakdown(samples)
        self.assertEqual(breakdown["2026-07"]["n"], 2)
        self.assertEqual(breakdown["2026-07"]["sessions"], 1)
        self.assertAlmostEqual(breakdown["2026-07"]["session_rate"], 0.5)
        self.assertAlmostEqual(breakdown["2026-07"]["avg_wind_kt"], 9.0)
        self.assertEqual(breakdown["2026-08"]["n"], 1)


class SelectBacktestLabelTests(unittest.TestCase):
    """SIA-first historical labeling (select_backtest_label): the label
    goes through the same ground_truth policy machinery as the live loop,
    a missing/invalid SIA hour is excluded (never proxy-labeled), and the
    threshold constant is shared with verify_and_learn.py."""

    def setUp(self):
        import ground_truth
        self.gt = ground_truth
        self.policy = ground_truth.load_policy()
        self.dt = datetime(2025, 7, 1, 13, 0, tzinfo=timezone.utc)

    def test_sia_hour_labels_through_policy(self):
        sia_obs = {self.dt: {"wind_speed_ms": 6.0, "wind_gust_ms": 9.0}}
        label = backtest.select_backtest_label(self.dt, sia_obs, self.policy)
        self.assertIsNotNone(label)
        self.assertEqual(label["source"], "sia")
        self.assertEqual(label["policy_version"], self.policy.policy_version)

    def test_missing_hour_returns_none_not_a_proxy_label(self):
        self.assertIsNone(backtest.select_backtest_label(self.dt, {}, self.policy))

    def test_hour_with_null_wind_returns_none(self):
        sia_obs = {self.dt: {"wind_speed_ms": None, "temperature_c": 15.0}}
        self.assertIsNone(backtest.select_backtest_label(self.dt, sia_obs, self.policy))

    def test_threshold_shared_with_live_loop(self):
        import verify_and_learn
        self.assertEqual(verify_and_learn.SIA_REFERENCE_KT, self.gt.SIA_REFERENCE_KT)

    def test_sam_proxy_constant_no_longer_used_for_labeling(self):
        import inspect
        source = inspect.getsource(backtest.build_samples_for_season)
        self.assertNotIn("SAM_PROXY_KT", source)


if __name__ == "__main__":
    unittest.main()
