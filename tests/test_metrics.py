"""Offline tests for metrics.py against hand-computed toy examples. No
network calls, no dependency on real logs/weights."""

import unittest

import metrics


class ConfusionAndBrierTests(unittest.TestCase):
    def test_confusion_counts(self):
        labels = [1.0, 1.0, 0.0, 0.0]
        preds = [1.0, 0.0, 1.0, 0.0]
        tp, fp, tn, fn = metrics.confusion_counts(labels, preds)
        self.assertEqual((tp, fp, tn, fn), (1, 1, 1, 1))

    def test_brier_score_perfect(self):
        self.assertEqual(metrics.brier_score([1.0, 0.0], [1.0, 0.0]), 0.0)

    def test_brier_score_worst(self):
        self.assertEqual(metrics.brier_score([1.0, 0.0], [0.0, 1.0]), 1.0)

    def test_brier_score_known_value(self):
        # (0.8-1)^2 + (0.3-0)^2 = 0.04 + 0.09 = 0.13, /2 = 0.065
        self.assertAlmostEqual(metrics.brier_score([1.0, 0.0], [0.8, 0.3]), 0.065)

    def test_brier_score_empty(self):
        self.assertIsNone(metrics.brier_score([], []))


class RocAucTests(unittest.TestCase):
    def test_perfect_ranking(self):
        labels = [0.0, 0.0, 1.0, 1.0]
        scores = [0.1, 0.2, 0.8, 0.9]
        self.assertEqual(metrics.roc_auc(labels, scores), 1.0)

    def test_inverted_ranking(self):
        labels = [0.0, 0.0, 1.0, 1.0]
        scores = [0.9, 0.8, 0.2, 0.1]
        self.assertEqual(metrics.roc_auc(labels, scores), 0.0)

    def test_constant_scores_is_half(self):
        labels = [0.0, 1.0, 0.0, 1.0]
        scores = [0.5, 0.5, 0.5, 0.5]
        self.assertEqual(metrics.roc_auc(labels, scores), 0.5)

    def test_single_class_is_undefined(self):
        self.assertIsNone(metrics.roc_auc([1.0, 1.0, 1.0], [0.1, 0.5, 0.9]))
        self.assertIsNone(metrics.roc_auc([0.0, 0.0], [0.1, 0.9]))

    def test_empty_is_undefined(self):
        self.assertIsNone(metrics.roc_auc([], []))


class AveragePrecisionTests(unittest.TestCase):
    def test_perfect_ranking_is_one(self):
        labels = [0.0, 0.0, 1.0, 1.0]
        scores = [0.1, 0.2, 0.8, 0.9]
        self.assertEqual(metrics.average_precision(labels, scores), 1.0)

    def test_no_positives_is_undefined(self):
        self.assertIsNone(metrics.average_precision([0.0, 0.0], [0.1, 0.9]))

    def test_worst_ranking_is_low(self):
        labels = [0.0, 0.0, 1.0, 1.0]
        scores = [0.9, 0.8, 0.2, 0.1]  # negatives ranked above positives
        ap = metrics.average_precision(labels, scores)
        self.assertLess(ap, 0.7)


class ClassificationReportTests(unittest.TestCase):
    def test_known_toy_example(self):
        # 4 positives, 4 negatives; threshold 0.5 -> 3 TP, 1 FP, 3 TN, 1 FN
        labels = [1, 1, 1, 1, 0, 0, 0, 0]
        probs = [0.9, 0.8, 0.7, 0.3, 0.6, 0.2, 0.1, 0.4]
        r = metrics.classification_report(labels, probs, threshold=0.5)
        self.assertEqual(r["n"], 8)
        self.assertEqual((r["true_positive"], r["false_positive"], r["true_negative"], r["false_negative"]),
                          (3, 1, 3, 1))
        self.assertAlmostEqual(r["accuracy"], 6 / 8)
        self.assertAlmostEqual(r["precision"], 3 / 4)
        self.assertAlmostEqual(r["recall"], 3 / 4)
        self.assertAlmostEqual(r["majority_baseline_accuracy"], 0.5)

    def test_empty_returns_n_zero(self):
        self.assertEqual(metrics.classification_report([], []), {"n": 0})


class ThresholdCalibrationTests(unittest.TestCase):
    def test_marginal_maximizes_balanced_accuracy(self):
        # Perfectly separable at 0.5: all positives >= 0.6, all negatives < 0.6
        labels = [1.0] * 10 + [0.0] * 10
        probs = [0.9] * 10 + [0.1] * 10
        th = metrics.calibrate_marginal_threshold(labels, probs)
        # Any threshold strictly between 0.1 and 0.9 achieves perfect
        # balanced accuracy on this fixture - the grid's first such value
        # is what gets returned (ties broken by iteration order).
        preds = [1.0 if p >= th else 0.0 for p in probs]
        tp, fp, tn, fn = metrics.confusion_counts(labels, preds)
        self.assertEqual(fp, 0)
        self.assertEqual(fn, 0)

    def test_calibration_uses_only_supplied_samples(self):
        # The function is a pure function of its arguments - calling it
        # with a strict subset must not "see" data that wasn't passed in
        # (i.e. it cannot depend on some hidden holdout/global state).
        labels_a = [1.0, 1.0, 0.0, 0.0] * 5
        probs_a = [0.9, 0.8, 0.2, 0.1] * 5
        th_a1 = metrics.calibrate_marginal_threshold(labels_a, probs_a)
        th_a2 = metrics.calibrate_marginal_threshold(labels_a, probs_a)
        self.assertEqual(th_a1, th_a2)  # deterministic given the same input

        # A held-out set with an opposite-looking distribution must not
        # change the calibration result computed from set A alone.
        labels_holdout = [0.0, 0.0, 1.0, 1.0] * 5
        probs_holdout = [0.9, 0.8, 0.2, 0.1] * 5
        th_a3 = metrics.calibrate_marginal_threshold(labels_a, probs_a)
        self.assertEqual(th_a1, th_a3)
        # (labels_holdout/probs_holdout are deliberately never passed in.)
        del labels_holdout, probs_holdout

    def test_good_threshold_always_above_marginal(self):
        # Adversarial-ish toy data that previously produced good <= marginal
        # before calibrate_good_threshold's fallback was fixed.
        labels = [1.0] * 30 + [0.0] * 30
        probs = ([0.55] * 15 + [0.9] * 15) + ([0.5] * 20 + [0.1] * 10)
        marginal = metrics.calibrate_marginal_threshold(labels, probs)
        good = metrics.calibrate_good_threshold(labels, probs, marginal_threshold=marginal)
        self.assertGreater(good, marginal)

    def test_good_threshold_falls_back_when_ungapped(self):
        # No amount of thresholding reaches 0.75 precision with >=20
        # positives predicted - must fall back to marginal + 0.15 (capped).
        labels = [1.0] * 5 + [0.0] * 95
        probs = [0.9] * 5 + [0.85] * 95  # noisy overlap, low precision everywhere
        marginal = metrics.calibrate_marginal_threshold(labels, probs)
        good = metrics.calibrate_good_threshold(labels, probs, marginal_threshold=marginal)
        self.assertGreater(good, marginal)
        self.assertLessEqual(good, 0.9)


class SessionAggregationTests(unittest.TestCase):
    def test_any_positive_hour_and_max_probability(self):
        dates = [
            "2026-07-01T12:00", "2026-07-01T13:00",  # day 1: one positive hour
            "2026-07-02T12:00", "2026-07-02T13:00",  # day 2: no positive hour
            "2026-07-03T20:00",  # outside window, must be excluded
        ]
        outcomes = [0.0, 1.0, 0.0, 0.0, 1.0]
        probs = [0.2, 0.7, 0.3, 0.4, 0.99]
        session_outcomes, session_probs, days = metrics.build_session_samples(
            dates, outcomes, probs, window_start_hour=12, window_end_hour=18)
        self.assertEqual(days, ["2026-07-01", "2026-07-02"])
        self.assertEqual(session_outcomes, [1.0, 0.0])
        self.assertEqual(session_probs, [0.7, 0.4])

    def test_empty_input(self):
        outcomes, probs, days = metrics.build_session_samples([], [], [], 12, 18)
        self.assertEqual((outcomes, probs, days), ([], [], []))


if __name__ == "__main__":
    unittest.main()
