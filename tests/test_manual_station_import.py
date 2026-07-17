"""Offline tests for manual_station_import.py's semicolon_weather CSV
parser - a real user-provided lake-station export format (Sils/Segl), no
network involved."""

import unittest
from datetime import datetime, timezone

import manual_station_import as msi

CSV_TEXT = (
    '"date/time (local)";"wind direction [degrees]";"wind speed [kts]";'
    '"air temperature [°C]";"air pressure [hPa]";"clouds"\n'
    '"2014-04-02 00:00:00";110;2.00;0.00;847.00;""\n'
    '"2014-04-02 08:00:00";10;1.00;-5.00;847.00;"SKC"\n'
    '"2014-04-02 13:00:00";240;13.00;4.00;846.00;""\n'
)


class ParseSemicolonWeatherCsvTests(unittest.TestCase):
    def test_parses_expected_number_of_rows(self):
        result = msi.parse_semicolon_weather_csv(CSV_TEXT)
        self.assertEqual(len(result), 3)

    def test_naive_local_timestamp_converted_to_utc(self):
        result = msi.parse_semicolon_weather_csv(CSV_TEXT)
        # 2014-04-02 00:00 CEST (UTC+2, DST already in effect) -> 2014-04-01T22:00 UTC
        expected = datetime(2014, 4, 1, 22, 0, tzinfo=timezone.utc)
        self.assertIn(expected, result)

    def test_wind_speed_converted_from_knots_to_ms(self):
        result = msi.parse_semicolon_weather_csv(CSV_TEXT)
        dt = datetime(2014, 4, 1, 22, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(result[dt]["wind_speed_ms"], 2.00 * 0.514444, places=3)

    def test_pressure_mapped_to_station_level_not_sea_level(self):
        result = msi.parse_semicolon_weather_csv(CSV_TEXT)
        dt = datetime(2014, 4, 1, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(result[dt]["pressure_station_hpa"], 847.0)
        self.assertNotIn("pressure_sea_level_hpa", result[dt])

    def test_wind_direction_and_temperature_present(self):
        result = msi.parse_semicolon_weather_csv(CSV_TEXT)
        dt = datetime(2014, 4, 1, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(result[dt]["wind_direction_deg"], 110.0)
        self.assertEqual(result[dt]["temperature_c"], 0.0)

    def test_empty_clouds_field_not_included(self):
        result = msi.parse_semicolon_weather_csv(CSV_TEXT)
        dt = datetime(2014, 4, 1, 22, 0, tzinfo=timezone.utc)
        self.assertNotIn("clouds_raw", result[dt])

    def test_nonempty_clouds_field_preserved(self):
        result = msi.parse_semicolon_weather_csv(CSV_TEXT)
        dt_with_clouds = datetime(2014, 4, 2, 6, 0, tzinfo=timezone.utc)  # 08:00 local -> 06:00 UTC
        self.assertEqual(result[dt_with_clouds]["clouds_raw"], "SKC")

    def test_empty_input_returns_empty_dict(self):
        header_only = '"date/time (local)";"wind direction [degrees]";"wind speed [kts]";' \
                      '"air temperature [°C]";"air pressure [hPa]";"clouds"\n'
        self.assertEqual(msi.parse_semicolon_weather_csv(header_only), {})

    def test_row_with_unparseable_timestamp_is_skipped_not_raised(self):
        bad = CSV_TEXT + '"not-a-timestamp";110;2.00;0.00;847.00;""\n'
        result = msi.parse_semicolon_weather_csv(bad)
        self.assertEqual(len(result), 3)


class ParsersRegistryTests(unittest.TestCase):
    def test_semicolon_weather_registered(self):
        self.assertIn("semicolon_weather", msi.PARSERS)
        self.assertIs(msi.PARSERS["semicolon_weather"], msi.parse_semicolon_weather_csv)


if __name__ == "__main__":
    unittest.main()
