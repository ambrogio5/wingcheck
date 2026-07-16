"""Offline tests for station_features.py: cutoff/reporting-delay discipline
(no observation past the cutoff may ever be included, at both 07:00 and
10:00), wind-vector conversion, trend computation, missing-data handling,
and the pairwise station-comparison helpers."""

import unittest

import station_features as sf


def _rec(local_ts, **overrides):
    base = {
        "timestamp_local": local_ts, "wind_speed_ms": 5.0, "wind_gust_ms": 8.0,
        "wind_direction_deg": 200.0, "temperature_c": 15.0, "dew_point_c": 10.0,
        "relative_humidity_pct": 60.0, "pressure_sea_level_hpa": 1013.0,
        "precipitation_mm": 0.0, "global_radiation_wm2": 100.0,
    }
    base.update(overrides)
    return base


class CutoffTests(unittest.TestCase):
    def test_07_00_cutoff_excludes_08_00_observation(self):
        records = [_rec("2026-07-15T06:00:00+02:00", wind_speed_ms=1.0),
                   _rec("2026-07-15T08:00:00+02:00", wind_speed_ms=99.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertEqual(feats["latest_wind_speed"], 1.0)

    def test_10_00_cutoff_includes_up_to_10_00(self):
        records = [_rec("2026-07-15T06:00:00+02:00", wind_speed_ms=1.0),
                   _rec("2026-07-15T10:00:00+02:00", wind_speed_ms=3.0),
                   _rec("2026-07-15T11:00:00+02:00", wind_speed_ms=99.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "10:00", 0)
        self.assertEqual(feats["latest_wind_speed"], 3.0)

    def test_no_afternoon_leakage_across_both_cutoffs(self):
        records = [_rec("2026-07-15T14:00:00+02:00", wind_speed_ms=999.0)]
        for cutoff in ("07:00", "10:00"):
            feats = sf.generate_station_features(records, "2026-07-15", cutoff, 0)
            self.assertIsNone(feats["latest_wind_speed"])
            self.assertEqual(feats["missing_indicator"], 1.0)

    def test_invalid_cutoff_raises(self):
        with self.assertRaises(ValueError):
            sf._cutoff_datetime("2026-07-15", "13:00")

    def test_reporting_delay_excludes_observation_not_yet_available(self):
        # A 06:50 observation with a 20-minute reporting delay isn't
        # actually available until 07:10 - after the 07:00 cutoff.
        records = [_rec("2026-07-15T06:50:00+02:00", wind_speed_ms=42.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", reporting_delay_minutes=20)
        self.assertIsNone(feats["latest_wind_speed"])

    def test_reporting_delay_includes_observation_that_is_available(self):
        records = [_rec("2026-07-15T06:30:00+02:00", wind_speed_ms=42.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", reporting_delay_minutes=20)
        self.assertEqual(feats["latest_wind_speed"], 42.0)

    def test_previous_day_records_excluded_from_morning_window(self):
        records = [_rec("2026-07-14T23:00:00+02:00", wind_speed_ms=999.0),
                   _rec("2026-07-15T06:00:00+02:00", wind_speed_ms=1.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertEqual(feats["latest_wind_speed"], 1.0)


class MissingDataTests(unittest.TestCase):
    def test_no_records_reports_missing(self):
        feats = sf.generate_station_features([], "2026-07-15", "07:00", 0)
        self.assertEqual(feats["missing_indicator"], 1.0)
        self.assertEqual(feats["coverage"], 0.0)
        self.assertIsNone(feats["latest_wind_speed"])

    def test_partial_data_reports_partial_coverage(self):
        records = [_rec(f"2026-07-15T{h:02d}:00:00+02:00") for h in (0, 6)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertEqual(feats["missing_indicator"], 0.0)
        self.assertGreater(feats["coverage"], 0)
        self.assertLess(feats["coverage"], 1.0)


class WindVectorTests(unittest.TestCase):
    def test_southerly_wind_from_south_has_negative_v(self):
        # Wind FROM the south (180 deg) blows northward (positive v... but
        # meteorological FROM-south wind moves toward the north, so v should
        # be negative per the -speed*cos(dir) convention at dir=180).
        u, v = sf._wind_vector(10.0, 180.0)
        self.assertAlmostEqual(u, 0.0, places=5)
        self.assertAlmostEqual(v, 10.0, places=5)

    def test_wind_from_west_has_negative_u(self):
        u, v = sf._wind_vector(10.0, 270.0)
        self.assertAlmostEqual(u, 10.0, places=5)
        self.assertAlmostEqual(v, 0.0, places=5)

    def test_none_inputs_return_none(self):
        self.assertEqual(sf._wind_vector(None, 180.0), (None, None))
        self.assertEqual(sf._wind_vector(10.0, None), (None, None))

    def test_wind_u_v_present_in_generated_features(self):
        records = [_rec("2026-07-15T06:00:00+02:00", wind_speed_ms=10.0, wind_direction_deg=200.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertIsNotNone(feats["wind_u"])
        self.assertIsNotNone(feats["wind_v"])


class TrendAndDerivedFeatureTests(unittest.TestCase):
    def test_wind_speed_trend_1h(self):
        records = [_rec("2026-07-15T05:00:00+02:00", wind_speed_ms=5.0),
                   _rec("2026-07-15T06:00:00+02:00", wind_speed_ms=8.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertAlmostEqual(feats["wind_speed_trend_1h"], 3.0, places=2)

    def test_dew_point_depression(self):
        records = [_rec("2026-07-15T06:00:00+02:00", temperature_c=20.0, dew_point_c=12.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertAlmostEqual(feats["dew_point_depression"], 8.0, places=2)

    def test_precipitation_since_midnight_sums_the_window(self):
        records = [_rec("2026-07-15T02:00:00+02:00", precipitation_mm=1.0),
                   _rec("2026-07-15T05:00:00+02:00", precipitation_mm=2.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertAlmostEqual(feats["precipitation_since_midnight"], 3.0, places=2)

    def test_max_morning_gust(self):
        records = [_rec("2026-07-15T04:00:00+02:00", wind_gust_ms=5.0),
                   _rec("2026-07-15T06:00:00+02:00", wind_gust_ms=12.0)]
        feats = sf.generate_station_features(records, "2026-07-15", "07:00", 0)
        self.assertEqual(feats["max_morning_gust"], 12.0)


class PairwiseHelperTests(unittest.TestCase):
    def test_temperature_difference(self):
        a = {"temperature_latest": 20.0}
        b = {"temperature_latest": 15.0}
        self.assertAlmostEqual(sf.temperature_difference(a, b), 5.0)

    def test_temperature_difference_missing_returns_none(self):
        self.assertIsNone(sf.temperature_difference({"temperature_latest": None}, {"temperature_latest": 15.0}))

    def test_pressure_difference(self):
        a = {"pressure_latest": 1018.0}
        b = {"pressure_latest": 1014.0}
        self.assertAlmostEqual(sf.pressure_difference(a, b), 4.0)

    def test_pressure_tendency_difference(self):
        a = {"pressure_trend_3h": 2.0}
        b = {"pressure_trend_3h": -1.0}
        self.assertAlmostEqual(sf.pressure_tendency_difference(a, b), 3.0)

    def test_wind_vector_difference_and_shear(self):
        a = {"wind_u": 3.0, "wind_v": 4.0}
        b = {"wind_u": 0.0, "wind_v": 0.0}
        du, dv = sf.wind_vector_difference(a, b)
        self.assertAlmostEqual(du, 3.0)
        self.assertAlmostEqual(dv, 4.0)
        self.assertAlmostEqual(sf.wind_vector_shear(a, b), 5.0)

    def test_wind_vector_shear_missing_returns_none(self):
        self.assertIsNone(sf.wind_vector_shear({"wind_u": None, "wind_v": None}, {"wind_u": 1.0, "wind_v": 1.0}))

    def test_warming_rate_difference(self):
        a = {"temperature_change_since_sunrise": 4.0}
        b = {"temperature_change_since_sunrise": 1.0}
        self.assertAlmostEqual(sf.warming_rate_difference(a, b), 3.0)


class GenerateAllStationFeaturesTests(unittest.TestCase):
    def test_generates_per_station_dict(self):
        import station_registry as sr
        records_by_station = {"sam": [_rec("2026-07-15T06:00:00+02:00")]}
        registry = {"sam": sr.Station(
            station_id="sam", name="Samedan", provider="meteoswiss", latitude=46.5, longitude=9.8,
            elevation_m=1700, roles=("target_region",), available_variables=("wind_speed_ms",),
            historical_available=True, live_available=True, licence="test", reporting_delay_minutes=10,
            enabled=True, verification="confirmed", notes="",
        )}
        result = sf.generate_all_station_features(records_by_station, "2026-07-15", "07:00", registry)
        self.assertIn("sam", result)
        self.assertIsNotNone(result["sam"]["latest_wind_speed"])


if __name__ == "__main__":
    unittest.main()
