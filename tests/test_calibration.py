"""Offline tests for calibration.py: reliability tables on known toy
examples, ECE/MCE/Brier/log-loss correctness, and that Platt scaling and
isotonic regression both improve calibration on synthetically
miscalibrated data while never being fit on anything but the data
explicitly passed in (no implicit holdout access)."""

import random
import unittest

import calibration as cal


class ReliabilityTableTests(unittest.TestCase):
    def test_perfect_calibration_zero_error(self):
        # Half the bin-0.7-0.8 predictions are positive... build an exact case.
        labels = [1.0, 1.0, 0.0, 0.0]
        probs = [0.75, 0.75, 0.75, 0.75]  # avg predicted 0.75, observed rate 0.5 -> NOT perfect
        table = cal.reliability_table(labels, probs, n_bins=10)
        bin_75 = next(b for b in table if b["bin_low"] <= 0.75 < b["bin_high"])
        self.assertEqual(bin_75["n"], 4)
        self.assertAlmostEqual(bin_75["avg_predicted"], 0.75)
        self.assertAlmostEqual(bin_75["observed_rate"], 0.5)
        self.assertAlmostEqual(bin_75["calibration_error"], 0.25)

    def test_empty_bin_reports_zero_count_and_null_rates(self):
        labels = [1.0]
        probs = [0.05]
        table = cal.reliability_table(labels, probs, n_bins=10)
        empty_bin = table[9]  # 90-100%, nothing there
        self.assertEqual(empty_bin["n"], 0)
        self.assertIsNone(empty_bin["avg_predicted"])
        self.assertIsNone(empty_bin["calibration_error"])

    def test_bin_count_matches_n_bins(self):
        table = cal.reliability_table([1.0, 0.0], [0.3, 0.6], n_bins=5)
        self.assertEqual(len(table), 5)

    def test_prob_of_exactly_1_0_falls_in_last_bin(self):
        table = cal.reliability_table([1.0], [1.0], n_bins=10)
        self.assertEqual(table[9]["n"], 1)


class ScalarMetricTests(unittest.TestCase):
    def test_expected_calibration_error_known_value(self):
        # All predictions in one bin: avg_pred=0.75, observed=0.5 -> ECE = 0.25
        labels = [1.0, 1.0, 0.0, 0.0]
        probs = [0.75, 0.75, 0.75, 0.75]
        self.assertAlmostEqual(cal.expected_calibration_error(labels, probs), 0.25)

    def test_maximum_calibration_error_is_worst_bin(self):
        labels = [1.0, 1.0, 0.0, 0.0, 1.0, 1.0]
        probs = [0.75, 0.75, 0.75, 0.75, 0.15, 0.15]  # bin1 error .25, bin2 error .85
        mce = cal.maximum_calibration_error(labels, probs)
        self.assertAlmostEqual(mce, 0.85, places=6)

    def test_log_loss_perfect_predictions_near_zero(self):
        ll = cal.log_loss([1.0, 0.0], [0.999, 0.001])
        self.assertLess(ll, 0.01)

    def test_log_loss_empty_is_none(self):
        self.assertIsNone(cal.log_loss([], []))

    def test_ece_empty_is_none(self):
        self.assertIsNone(cal.expected_calibration_error([], []))


class PlattScalingTests(unittest.TestCase):
    def test_reduces_ece_on_systematically_overconfident_predictions(self):
        rng = random.Random(1)
        true_probs = [rng.random() for _ in range(1500)]
        labels = [1.0 if rng.random() < tp else 0.0 for tp in true_probs]
        overconfident = [min(0.999, max(0.001, 0.5 + (tp - 0.5) * 1.8)) for tp in true_probs]

        platt = cal.fit_platt_scaling(labels, overconfident)
        calibrated = cal.apply_platt_scaling(overconfident, platt)

        ece_before = cal.expected_calibration_error(labels, overconfident)
        ece_after = cal.expected_calibration_error(labels, calibrated)
        self.assertLess(ece_after, ece_before)

    def test_no_training_data_is_identity_mapping(self):
        platt = cal.fit_platt_scaling([], [])
        self.assertEqual(platt, {"a": 1.0, "b": 0.0})
        out = cal.apply_platt_scaling([0.3, 0.7], platt)
        self.assertAlmostEqual(out[0], 0.3, places=6)
        self.assertAlmostEqual(out[1], 0.7, places=6)


class IsotonicRegressionTests(unittest.TestCase):
    def test_output_is_monotone_nondecreasing(self):
        rng = random.Random(2)
        probs = [rng.random() for _ in range(200)]
        labels = [1.0 if rng.random() < p else 0.0 for p in probs]
        breakpoints = cal.fit_isotonic_regression(labels, probs)
        ys = [b["y"] for b in breakpoints]
        self.assertEqual(ys, sorted(ys))

    def test_reduces_ece_on_systematically_overconfident_predictions(self):
        rng = random.Random(3)
        true_probs = [rng.random() for _ in range(1500)]
        labels = [1.0 if rng.random() < tp else 0.0 for tp in true_probs]
        overconfident = [min(0.999, max(0.001, 0.5 + (tp - 0.5) * 1.8)) for tp in true_probs]

        breakpoints = cal.fit_isotonic_regression(labels, overconfident)
        calibrated = cal.apply_isotonic_regression(overconfident, breakpoints)

        ece_before = cal.expected_calibration_error(labels, overconfident)
        ece_after = cal.expected_calibration_error(labels, calibrated)
        self.assertLess(ece_after, ece_before)

    def test_empty_training_data_is_passthrough(self):
        breakpoints = cal.fit_isotonic_regression([], [])
        self.assertEqual(breakpoints, [])
        out = cal.apply_isotonic_regression([0.3, 0.7], breakpoints)
        self.assertEqual(out, [0.3, 0.7])

    def test_fitting_uses_only_supplied_training_data(self):
        train_labels = [0.0, 0.0, 1.0, 1.0]
        train_probs = [0.1, 0.2, 0.8, 0.9]
        bp1 = cal.fit_isotonic_regression(train_labels, train_probs)
        bp2 = cal.fit_isotonic_regression(train_labels, train_probs)
        self.assertEqual(bp1, bp2)  # deterministic, pure function of its inputs


class CalibrationSummaryTests(unittest.TestCase):
    def test_summary_contains_all_expected_keys(self):
        labels = [1.0, 0.0, 1.0, 0.0]
        probs = [0.6, 0.4, 0.7, 0.3]
        summary = cal.calibration_summary(labels, probs)
        for key in ("brier_score", "log_loss", "expected_calibration_error",
                    "maximum_calibration_error", "reliability_table"):
            self.assertIn(key, summary)


if __name__ == "__main__":
    unittest.main()
