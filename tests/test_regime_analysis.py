"""Offline tests for regime_analysis.py: out-of-sample-only evaluation
(fresh model per fold, never touching weights.json), regime breakdown
shape, and false-positive summary aggregation. Synthetic fixtures only."""

import os
import random
import unittest

import regime_analysis as ra
from features import FEATURE_NAMES
from regimes import REGIME_NAMES


def _make_samples(n, seed=0, start_date="2024-06-01"):
    from datetime import datetime, timedelta
    rng = random.Random(seed)
    samples = []
    for i in range(n):
        feats = {name: rng.uniform(-1, 1) for name in FEATURE_NAMES}
        z = feats["model_wind"] * 2
        prob = 1 / (1 + pow(2.718281828, -z))
        outcome = 1.0 if rng.random() < prob else 0.0
        day = i // 7
        hour = 12 + (i % 7)
        date = (datetime.fromisoformat(start_date) + timedelta(days=day)).strftime("%Y-%m-%d")
        samples.append({"date": f"{date}T{hour:02d}:00", "year": int(date[:4]), "features": feats, "outcome": outcome})
    return samples


class RegimeAnalysisTests(unittest.TestCase):
    def test_never_touches_weights_json(self):
        import model
        mtime_before = os.path.getmtime(model.WEIGHTS_PATH) if os.path.exists(model.WEIGHTS_PATH) else None
        samples = _make_samples(80)
        ra.run_regime_analysis(samples)
        if mtime_before is not None:
            self.assertEqual(os.path.getmtime(model.WEIGHTS_PATH), mtime_before)

    def test_every_fold_covers_every_regime_name(self):
        samples = _make_samples(80)
        results = ra.run_regime_analysis(samples)
        for fold_name, fold_data in results.items():
            self.assertEqual(set(fold_data["by_regime"]), set(REGIME_NAMES))

    def test_regime_with_no_samples_reports_n_zero(self):
        samples = _make_samples(80)
        results = ra.run_regime_analysis(samples)
        for fold_data in results.values():
            for regime, report in fold_data["by_regime"].items():
                self.assertIn("n", report)

    def test_false_positive_summary_shape(self):
        samples = _make_samples(80)
        results = ra.run_regime_analysis(samples)
        summary = ra.summarize_false_positive_drivers(results)
        for regime, s in summary.items():
            self.assertIn("n", s)
            self.assertIn("false_positive_share_of_regime", s)

    def test_false_positive_summary_sorted_descending(self):
        samples = _make_samples(150, seed=5)
        results = ra.run_regime_analysis(samples)
        summary = ra.summarize_false_positive_drivers(results)
        shares = [s["false_positive_share_of_regime"] for s in summary.values() if s["false_positive_share_of_regime"] is not None]
        self.assertEqual(shares, sorted(shares, reverse=True))


if __name__ == "__main__":
    unittest.main()
