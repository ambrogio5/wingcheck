"""Offline tests for maloja_diagnostics.py: every diagnostic returns the
fixed {score, status, raw_values, sources, explanation_key, missing} shape,
honestly reports missing when no station data exists, and the summit
support nonlinear status (weak/supportive/excessive/opposing/missing) is
individually reachable."""

import unittest

import maloja_diagnostics as md


def _feats(**overrides):
    base = {"missing_indicator": 0.0, "latest_wind_speed": 5.0, "wind_u": 0.0, "wind_v": 5.0,
            "temperature_latest": 15.0, "temperature_change_since_sunrise": 2.0}
    base.update(overrides)
    return base


class ResultShapeTests(unittest.TestCase):
    def test_every_diagnostic_returns_the_fixed_shape(self):
        results = [
            md.source_heating({}, {}),
            md.pass_activation({}),
            md.summit_support({}),
            md.radiation_support({}),
            md.pressure_support({}, {}),
            md.competing_flow(None),
            md.data_health({}),
        ]
        for r in results:
            self.assertEqual(set(r.keys()), {"score", "status", "raw_values", "sources", "explanation_key", "missing"})
            self.assertIsInstance(r["score"], float)
            self.assertIsInstance(r["missing"], bool)


class SourceHeatingTests(unittest.TestCase):
    def test_missing_when_no_data(self):
        r = md.source_heating({}, {})
        self.assertTrue(r["missing"])
        self.assertEqual(r["explanation_key"], "source_heating_missing_station_data")

    def test_favourable_when_source_much_warmer(self):
        source = _feats(temperature_latest=20.0, temperature_change_since_sunrise=3.0)
        target = _feats(temperature_latest=13.0)
        r = md.source_heating(source, target)
        self.assertEqual(r["status"], "favourable")
        self.assertFalse(r["missing"])

    def test_unfavourable_when_source_cooler(self):
        source = _feats(temperature_latest=10.0)
        target = _feats(temperature_latest=15.0)
        r = md.source_heating(source, target)
        self.assertEqual(r["status"], "unfavourable")


class PassActivationTests(unittest.TestCase):
    def test_missing_with_no_speed(self):
        r = md.pass_activation({"missing_indicator": 0.0, "latest_wind_speed": None})
        self.assertTrue(r["missing"])

    def test_favourable_when_aligned_and_strong_enough(self):
        # dir≈220° (within PASS_ALIGNED_SECTOR 200-260)
        r = md.pass_activation(_feats(latest_wind_speed=5.0, wind_u=3.214, wind_v=3.830))
        self.assertEqual(r["status"], "favourable")

    def test_unfavourable_when_too_weak(self):
        r = md.pass_activation(_feats(latest_wind_speed=0.5, wind_u=0.0, wind_v=0.5))
        self.assertEqual(r["status"], "unfavourable")


class SummitSupportTests(unittest.TestCase):
    def test_missing_status_reachable(self):
        r = md.summit_support({})
        self.assertEqual(r["status"], "missing")

    def test_weak_status_reachable(self):
        r = md.summit_support(_feats(latest_wind_speed=1.0, wind_u=0.0, wind_v=1.0))
        self.assertEqual(r["status"], "weak")

    def test_supportive_status_reachable(self):
        r = md.summit_support(_feats(latest_wind_speed=6.0, wind_u=0.0, wind_v=6.0))  # dir=180, aligned
        self.assertEqual(r["status"], "supportive")

    def test_excessive_status_reachable(self):
        r = md.summit_support(_feats(latest_wind_speed=20.0, wind_u=0.0, wind_v=20.0))
        self.assertEqual(r["status"], "excessive")

    def test_opposing_status_reachable(self):
        # direction ~45 deg (NE) is in the opposing sector regardless of speed
        r = md.summit_support(_feats(latest_wind_speed=6.0, wind_u=-4.24, wind_v=-4.24))
        self.assertEqual(r["status"], "opposing")

    def test_all_five_statuses_are_individually_reachable(self):
        seen = {
            md.summit_support({})["status"],
            md.summit_support(_feats(latest_wind_speed=1.0, wind_u=0.0, wind_v=1.0))["status"],
            md.summit_support(_feats(latest_wind_speed=6.0, wind_u=0.0, wind_v=6.0))["status"],
            md.summit_support(_feats(latest_wind_speed=20.0, wind_u=0.0, wind_v=20.0))["status"],
            md.summit_support(_feats(latest_wind_speed=6.0, wind_u=-4.24, wind_v=-4.24))["status"],
        }
        self.assertEqual(seen, {"missing", "weak", "supportive", "excessive", "opposing"})


class RadiationSupportTests(unittest.TestCase):
    def test_missing_without_data(self):
        r = md.radiation_support({"missing_indicator": 0.0})
        self.assertTrue(r["missing"])

    def test_unfavourable_with_recent_rain(self):
        r = md.radiation_support({"missing_indicator": 0.0, "radiation_since_sunrise": 200.0, "precipitation_since_midnight": 5.0})
        self.assertEqual(r["status"], "unfavourable")

    def test_favourable_with_strong_radiation_no_rain(self):
        r = md.radiation_support({"missing_indicator": 0.0, "radiation_since_sunrise": 300.0, "precipitation_since_midnight": 0.0})
        self.assertEqual(r["status"], "favourable")


class PressureSupportTests(unittest.TestCase):
    def test_missing_without_both_stations(self):
        r = md.pressure_support({}, {"missing_indicator": 0.0, "pressure_latest": 1013.0})
        self.assertTrue(r["missing"])

    def test_favourable_gradient(self):
        lug = {"missing_indicator": 0.0, "pressure_latest": 1018.0}
        sma = {"missing_indicator": 0.0, "pressure_latest": 1013.0}
        r = md.pressure_support(lug, sma)
        self.assertEqual(r["status"], "favourable")

    def test_forecast_signal_reported_but_never_affects_score(self):
        lug = {"missing_indicator": 0.0, "pressure_latest": 1018.0}
        sma = {"missing_indicator": 0.0, "pressure_latest": 1013.0}
        r_no_forecast = md.pressure_support(lug, sma, forecast_pressure_signal=None)
        r_with_forecast = md.pressure_support(lug, sma, forecast_pressure_signal=-5.0)
        self.assertEqual(r_no_forecast["score"], r_with_forecast["score"])
        self.assertEqual(r_with_forecast["raw_values"]["forecast_pressure_signal"], -5.0)


class CompetingFlowTests(unittest.TestCase):
    def test_missing_without_direction(self):
        r = md.competing_flow(None)
        self.assertTrue(r["missing"])

    def test_easterly_flagged(self):
        r = md.competing_flow(90)
        self.assertEqual(r["status"], "easterly")

    def test_northerly_flagged(self):
        r = md.competing_flow(10)
        self.assertEqual(r["status"], "northerly")

    def test_clear_when_aligned_sw(self):
        r = md.competing_flow(220)
        self.assertEqual(r["status"], "clear")

    def test_misaligned_shear_flagged(self):
        r = md.competing_flow(220, summit_wind_dir_deg=350)
        self.assertEqual(r["status"], "misaligned_shear")


class DataHealthTests(unittest.TestCase):
    def test_critical_with_no_stations(self):
        r = md.data_health({})
        self.assertEqual(r["status"], "critical")

    def test_healthy_with_full_coverage(self):
        r = md.data_health({"sam": {"coverage": 1.0, "missing_indicator": 0.0}})
        self.assertEqual(r["status"], "healthy")

    def test_degraded_with_partial_coverage(self):
        r = md.data_health({"sam": {"coverage": 0.5, "missing_indicator": 0.0}})
        self.assertEqual(r["status"], "degraded")

    def test_critical_when_all_missing(self):
        r = md.data_health({"sam": {"coverage": 0.0, "missing_indicator": 1.0}})
        self.assertEqual(r["status"], "critical")


def _summit_feats(speed, direction_deg, **overrides):
    """Builds summit_feats with wind_u/wind_v derived from a compass
    direction, using the same meteorological convention as
    station_features._wind_vector (direction = where wind blows FROM)."""
    import math as _math
    rad = _math.radians(direction_deg)
    u = -speed * _math.sin(rad)
    v = -speed * _math.cos(rad)
    base = {"missing_indicator": 0.0, "latest_wind_speed": speed, "wind_u": u, "wind_v": v,
            "mean_morning_wind": speed, "max_morning_gust": speed + 2.0,
            "wind_speed_trend_1h": 0.5, "wind_speed_trend_3h": 1.0,
            "temperature_latest": 5.0, "coverage": 1.0}
    base.update(overrides)
    return base


class SummitWindDiagnosisTests(unittest.TestCase):
    """summit_wind_diagnosis (Part 6) - direction-vector reconstruction,
    each of the 5 statuses individually reachable, missing-COV behavior,
    and that every threshold used is echoed back verbatim."""

    def test_missing_when_no_feats(self):
        r = md.summit_wind_diagnosis({})
        self.assertEqual(r["status"], "missing")
        self.assertEqual(r["raw_values"], {})
        self.assertEqual(r["explanation_key"], "summit_wind_missing_station_data")
        self.assertEqual(r["source_station"], "cov")

    def test_missing_when_missing_indicator_set(self):
        feats = _summit_feats(5.0, 225.0, missing_indicator=1.0)
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["status"], "missing")

    def test_missing_when_no_latest_wind_speed(self):
        r = md.summit_wind_diagnosis({"missing_indicator": 0.0, "latest_wind_speed": None})
        self.assertEqual(r["status"], "missing")

    def test_direction_recovered_from_vector_matches_input(self):
        feats = _summit_feats(6.0, 225.0)
        r = md.summit_wind_diagnosis(feats)
        self.assertAlmostEqual(r["raw_values"]["wind_direction_deg"], 225.0, delta=0.5)

    def test_weak_wind_is_neutral(self):
        feats = _summit_feats(1.0, 225.0)  # below weak_max_ms=3.0
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["status"], "neutral")
        self.assertEqual(r["explanation_key"], "summit_wind_neutral")

    def test_moderate_sw_aligned_is_supportive(self):
        feats = _summit_feats(8.0, 225.0)  # in [3,12] and SW-aligned
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["status"], "supportive")
        self.assertEqual(r["explanation_key"], "summit_wind_supportive")

    def test_moderate_northerly_is_opposing(self):
        feats = _summit_feats(8.0, 0.0)  # moderate speed but northerly
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["status"], "opposing")
        self.assertEqual(r["explanation_key"], "summit_wind_opposing")

    def test_moderate_easterly_is_opposing(self):
        feats = _summit_feats(8.0, 90.0)
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["status"], "opposing")

    def test_very_strong_wind_is_excessive_regardless_of_direction(self):
        feats = _summit_feats(20.0, 225.0)  # >= excessive_min_ms=18, even though SW-aligned
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["status"], "excessive")
        self.assertEqual(r["explanation_key"], "summit_wind_excessive")

    def test_moderate_but_not_sw_aligned_and_not_opposing_is_neutral(self):
        feats = _summit_feats(8.0, 160.0)  # SSE - >60deg from SW/N/E alignment centers, all scores <0.5
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["status"], "neutral")

    def test_thresholds_are_echoed_back_verbatim(self):
        feats = _summit_feats(8.0, 225.0)
        r = md.summit_wind_diagnosis(feats)
        self.assertEqual(r["thresholds"], md.DEFAULT_SUMMIT_WIND_THRESHOLDS)

    def test_custom_thresholds_override_defaults(self):
        feats = _summit_feats(4.0, 225.0)
        custom = {"supportive_min_ms": 2.0}
        r = md.summit_wind_diagnosis(feats, thresholds=custom)
        self.assertEqual(r["thresholds"]["supportive_min_ms"], 2.0)
        self.assertEqual(r["status"], "supportive")

    def test_source_station_and_observed_at_passed_through(self):
        feats = _summit_feats(8.0, 225.0)
        r = md.summit_wind_diagnosis(feats, station_id="cov", observed_at="2026-07-16T10:00:00+00:00",
                                       age_minutes=12.5)
        self.assertEqual(r["source_station"], "cov")
        self.assertEqual(r["observed_at"], "2026-07-16T10:00:00+00:00")
        self.assertEqual(r["age_minutes"], 12.5)

    def test_samedan_temperature_diff_and_shear_computed_when_provided(self):
        feats = _summit_feats(8.0, 225.0, temperature_latest=-2.0)
        samedan = _summit_feats(4.0, 200.0, temperature_latest=15.0)
        r = md.summit_wind_diagnosis(feats, samedan_feats=samedan)
        self.assertAlmostEqual(r["raw_values"]["samedan_temperature_diff_c"], -17.0, delta=0.01)
        self.assertIsNotNone(r["raw_values"]["samedan_wind_vector_shear_ms"])

    def test_all_14_raw_value_keys_present(self):
        feats = _summit_feats(8.0, 225.0)
        r = md.summit_wind_diagnosis(feats)
        expected_keys = {
            "latest_wind_speed_ms", "morning_mean_wind_ms", "morning_max_gust_ms",
            "wind_direction_deg", "wind_direction_sin", "wind_direction_cos",
            "sw_alignment_score", "northerly_opposition_score", "easterly_opposition_score",
            "wind_speed_trend_1h_ms", "wind_speed_trend_3h_ms", "temperature_c",
            "samedan_temperature_diff_c", "samedan_wind_vector_shear_ms",
        }
        self.assertTrue(expected_keys.issubset(r["raw_values"].keys()))


if __name__ == "__main__":
    unittest.main()
