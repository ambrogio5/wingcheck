"""Offline tests for session_forecast.py: onset/peak/best-window detection
from a day's hourly forecasts, the confidence-reduction rules (model
spread, flat curve, missing station data, conflicting diagnostics, stale
data), and that no sub-hour precision is ever implied."""

import unittest

import session_forecast as sfc


def _hour(t, prob, tier, wind=10.0, gust=15.0):
    return {"target_time": t, "probability": prob, "tier": tier, "model_wind_kt": wind, "model_gust_kt": gust}


DAY = [
    _hour("2026-07-16T12:00", 0.1, "UNLIKELY", 5, 8),
    _hour("2026-07-16T13:00", 0.3, "UNLIKELY", 6, 9),
    _hour("2026-07-16T14:00", 0.55, "MARGINAL", 11, 15),
    _hour("2026-07-16T15:00", 0.8, "GOOD", 14, 18),
    _hour("2026-07-16T16:00", 0.75, "GOOD", 13, 17),
    _hour("2026-07-16T17:00", 0.4, "MARGINAL", 10, 14),
    _hour("2026-07-16T18:00", 0.15, "UNLIKELY", 6, 9),
]


class EmptyInputTests(unittest.TestCase):
    def test_empty_hours_returns_safe_defaults(self):
        result = sfc.build_session_forecast([])
        self.assertIsNone(result["peak_hour"])
        self.assertEqual(result["expected_rideable_hours"], 0)
        self.assertEqual(result["event_probability"], 0.0)
        self.assertEqual(result["timing_confidence"], "low")


class OnsetPeakWindowTests(unittest.TestCase):
    def test_peak_hour_is_max_probability_hour(self):
        result = sfc.build_session_forecast(DAY)
        self.assertEqual(result["peak_hour"], "2026-07-16T15:00")

    def test_event_probability_is_the_max(self):
        result = sfc.build_session_forecast(DAY)
        self.assertEqual(result["event_probability"], 0.8)

    def test_onset_is_first_rideable_hour(self):
        result = sfc.build_session_forecast(DAY)
        self.assertEqual(result["likely_onset_start"], "2026-07-16T14:00")

    def test_decline_time_is_last_rideable_hour(self):
        result = sfc.build_session_forecast(DAY)
        self.assertEqual(result["likely_decline_time"], "2026-07-16T17:00")

    def test_best_window_spans_the_contiguous_rideable_block(self):
        result = sfc.build_session_forecast(DAY)
        self.assertEqual(result["best_window_start"], "2026-07-16T14:00")
        self.assertEqual(result["best_window_end"], "2026-07-16T17:00")

    def test_expected_rideable_hours_count(self):
        result = sfc.build_session_forecast(DAY)
        self.assertEqual(result["expected_rideable_hours"], 4)

    def test_wind_and_gust_ranges_from_rideable_hours_only(self):
        result = sfc.build_session_forecast(DAY)
        self.assertEqual(result["expected_wind_min_kt"], 10)
        self.assertEqual(result["expected_wind_max_kt"], 14)
        self.assertEqual(result["expected_gust_min_kt"], 14)
        self.assertEqual(result["expected_gust_max_kt"], 18)

    def test_no_rideable_hours_falls_back_to_full_day_wind_range(self):
        all_unlikely = [_hour("2026-07-16T12:00", 0.1, "UNLIKELY", 5, 8),
                         _hour("2026-07-16T13:00", 0.2, "UNLIKELY", 7, 10)]
        result = sfc.build_session_forecast(all_unlikely)
        self.assertIsNone(result["best_window_start"])
        self.assertEqual(result["expected_wind_min_kt"], 5)
        self.assertEqual(result["expected_wind_max_kt"], 7)

    def test_no_timestamp_implies_sub_hour_precision(self):
        result = sfc.build_session_forecast(DAY)
        for key in ("likely_onset_start", "best_window_start", "peak_hour", "likely_decline_time"):
            value = result[key]
            self.assertIn(value, [h["target_time"] for h in DAY])


class ConfidenceTests(unittest.TestCase):
    def test_high_confidence_with_clean_agreeing_data(self):
        result = sfc.build_session_forecast(DAY, model_agreement=0.95)
        self.assertEqual(result["timing_confidence"], "high")
        self.assertEqual(result["strength_confidence"], "high")

    def test_high_model_spread_reduces_confidence(self):
        clean = sfc.build_session_forecast(DAY, model_agreement=0.95)
        spread = sfc.build_session_forecast(DAY, model_agreement=0.1)
        confidence_order = {"low": 0, "medium": 1, "high": 2}
        self.assertLess(confidence_order[spread["timing_confidence"]], confidence_order[clean["timing_confidence"]])

    def test_flat_curve_reduces_timing_confidence_only(self):
        flat = [_hour(f"2026-07-16T{h:02d}:00", 0.5, "MARGINAL") for h in range(12, 19)]
        result = sfc.build_session_forecast(flat, model_agreement=0.95)
        self.assertEqual(result["timing_confidence"], "medium")
        self.assertEqual(result["strength_confidence"], "high")

    def test_missing_station_data_reduces_both_confidences(self):
        clean = sfc.build_session_forecast(DAY, model_agreement=0.95)
        missing = sfc.build_session_forecast(DAY, model_agreement=0.95, station_data_missing=True)
        confidence_order = {"low": 0, "medium": 1, "high": 2}
        self.assertLessEqual(confidence_order[missing["timing_confidence"]], confidence_order[clean["timing_confidence"]])
        self.assertLessEqual(confidence_order[missing["strength_confidence"]], confidence_order[clean["strength_confidence"]])

    def test_conflicting_diagnostics_reduce_confidence(self):
        diagnostics = {
            "pressure_support": {"status": "favourable", "missing": False},
            "competing_flow": {"status": "easterly", "missing": False},
        }
        result = sfc.build_session_forecast(DAY, model_agreement=0.95, diagnostics=diagnostics)
        clean = sfc.build_session_forecast(DAY, model_agreement=0.95)
        confidence_order = {"low": 0, "medium": 1, "high": 2}
        self.assertLess(confidence_order[result["timing_confidence"]], confidence_order[clean["timing_confidence"]])

    def test_agreeing_diagnostics_do_not_reduce_confidence(self):
        diagnostics = {
            "pressure_support": {"status": "favourable", "missing": False},
            "summit_support": {"status": "missing", "missing": True},
        }
        result = sfc.build_session_forecast(DAY, model_agreement=0.95, diagnostics=diagnostics)
        self.assertEqual(result["timing_confidence"], "high")

    def test_stale_data_reduces_strength_confidence_only(self):
        result = sfc.build_session_forecast(DAY, model_agreement=0.95, data_age_minutes=200)
        self.assertEqual(result["timing_confidence"], "high")
        self.assertNotEqual(result["strength_confidence"], "high")


if __name__ == "__main__":
    unittest.main()
