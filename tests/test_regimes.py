"""Offline tests for regimes.py's rule-based weather-regime classifier:
every defined regime is reachable, priority ordering is respected
(disruptive regimes take precedence), and missing features degrade
gracefully rather than crashing."""

import unittest

from regimes import classify_regime, classify_samples, REGIME_NAMES


def _features(**overrides):
    base = {
        "thermal_excess": 0.0, "pressure_signal": 0.0, "upper_wind_alignment": 0.0,
        "upper_wind_speed_score": 0.0, "cape_penalty": 0.0, "precip_penalty": 0.0,
        "surface_dir_alignment": 0.0,
    }
    base.update(overrides)
    return base


class RegimeClassificationTests(unittest.TestCase):
    def test_high_cape_penalty_is_convective_disruption(self):
        self.assertEqual(classify_regime(_features(cape_penalty=-0.8)), "convective_storm_disruption")

    def test_high_precip_penalty_is_cloudy_rain_suppressed(self):
        self.assertEqual(classify_regime(_features(precip_penalty=-0.8)), "cloudy_rain_suppressed_thermal")

    def test_cape_takes_priority_over_precip(self):
        # Both conditions true - storm risk must win (checked first).
        result = classify_regime(_features(cape_penalty=-0.8, precip_penalty=-0.8))
        self.assertEqual(result, "convective_storm_disruption")

    def test_misaligned_upper_wind_with_sw_surface_is_easterly(self):
        result = classify_regime(_features(upper_wind_alignment=-0.5, upper_wind_speed_score=0.5, surface_dir_alignment=0.3))
        self.assertEqual(result, "easterly_suppression")

    def test_misaligned_upper_wind_without_sw_surface_is_northerly(self):
        result = classify_regime(_features(upper_wind_alignment=-0.5, upper_wind_speed_score=0.5, surface_dir_alignment=-0.3))
        self.assertEqual(result, "northerly_suppression")

    def test_strong_aligned_upper_wind_with_pressure_is_synoptic_sw(self):
        result = classify_regime(_features(upper_wind_alignment=0.6, upper_wind_speed_score=0.6, pressure_signal=0.5))
        self.assertEqual(result, "strong_synoptic_southwest")

    def test_thermal_with_aligned_upper_wind_is_supportive_sw(self):
        result = classify_regime(_features(thermal_excess=0.3, upper_wind_alignment=0.3))
        self.assertEqual(result, "thermal_supportive_southwest")

    def test_thermal_alone_is_clean_thermal(self):
        result = classify_regime(_features(thermal_excess=0.3))
        self.assertEqual(result, "clean_thermal_maloja")

    def test_nothing_matches_is_uncertain_mixed(self):
        result = classify_regime(_features())
        self.assertEqual(result, "uncertain_mixed")

    def test_missing_features_default_gracefully(self):
        # An empty features dict must not raise - every .get() has a default.
        result = classify_regime({})
        self.assertEqual(result, "uncertain_mixed")

    def test_every_regime_is_reachable(self):
        """Every name in REGIME_NAMES must be producible by some input -
        an unreachable regime would be dead, misleading documentation."""
        reachable = set()
        reachable.add(classify_regime(_features(cape_penalty=-0.8)))
        reachable.add(classify_regime(_features(precip_penalty=-0.8)))
        reachable.add(classify_regime(_features(upper_wind_alignment=-0.5, upper_wind_speed_score=0.5, surface_dir_alignment=0.3)))
        reachable.add(classify_regime(_features(upper_wind_alignment=-0.5, upper_wind_speed_score=0.5, surface_dir_alignment=-0.3)))
        reachable.add(classify_regime(_features(upper_wind_alignment=0.6, upper_wind_speed_score=0.6, pressure_signal=0.5)))
        reachable.add(classify_regime(_features(thermal_excess=0.3, upper_wind_alignment=0.3)))
        reachable.add(classify_regime(_features(thermal_excess=0.3)))
        reachable.add(classify_regime(_features()))
        self.assertEqual(reachable, set(REGIME_NAMES))

    def test_classify_samples_preserves_order_and_does_not_mutate(self):
        samples = [{"features": _features(thermal_excess=0.3)}, {"features": _features(cape_penalty=-0.8)}]
        original = [dict(s) for s in samples]
        labels = classify_samples(samples)
        self.assertEqual(labels, ["clean_thermal_maloja", "convective_storm_disruption"])
        self.assertEqual(samples, original)


if __name__ == "__main__":
    unittest.main()
