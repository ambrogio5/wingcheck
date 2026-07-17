"""Offline tests for meteoswiss.py's generic multi-variable MeteoSwiss
station support (Part 2): metadata CSV parsing, generic hourly CSV
parsing (fail-fast on a requested-but-missing core variable), and COV
recent/historical fetch paths using fixture data with requests mocked -
never a real network call."""

import io
import unittest
from unittest import mock

import meteoswiss as ms


METADATA_CSV = (
    "station_abbr;station_name;station_canton;station_wigos_id;station_data_since;"
    "station_height_masl;station_coordinates_wgs84_lat;station_coordinates_wgs84_lon\n"
    "COV;Piz Corvatsch;GR;0-20000-0-06755;1959;3295;46.4144;9.8214\n"
    "SAM;Samedan;GR;0-20000-0-06755;1978;1705;46.5335;9.8794\n"
)

STATION_CSV = (
    "station_abbr;reference_timestamp;tre200h0;ure200h0;fu3010h0;fu3010h1;dkl010h0;pp0qffh0\n"
    "COV;01.07.2026 06:00;5.2;60;25.0;40.0;220;850.5\n"
    "COV;01.07.2026 07:00;5.5;58;30.0;45.0;225;850.2\n"
)


def _fake_response(text):
    resp = mock.Mock()
    resp.content = text.encode("utf-8")
    resp.raise_for_status = mock.Mock()
    return resp


class MetadataParsingTests(unittest.TestCase):
    def test_parses_known_station(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            meta = ms.fetch_station_metadata("cov")
        self.assertEqual(meta["station_abbr"], "COV")
        self.assertAlmostEqual(meta["latitude"], 46.4144)
        self.assertAlmostEqual(meta["longitude"], 9.8214)
        self.assertAlmostEqual(meta["elevation_m"], 3295)

    def test_case_insensitive_lookup(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            meta = ms.fetch_station_metadata("COV")
        self.assertEqual(meta["station_abbr"], "COV")

    def test_unknown_station_raises(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            with self.assertRaises(ValueError):
                ms.fetch_station_metadata("zzz")

    def test_no_station_id_returns_full_dict(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            all_stations = ms.fetch_station_metadata()
        self.assertIn("cov", all_stations)
        self.assertIn("sam", all_stations)


class SearchStationsByNameTests(unittest.TestCase):
    """search_stations_by_name() - the only sanctioned way to discover a
    real station's official abbreviation, so a candidate is never assumed
    to exist under a guessed code (see docs/STATION_RESEARCH.md)."""

    def test_matches_by_name_substring(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            result = ms.search_stations_by_name("corvatsch")
        self.assertIn("cov", result)
        self.assertNotIn("sam", result)

    def test_matches_by_abbreviation_substring(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            result = ms.search_stations_by_name("sam")
        self.assertIn("sam", result)

    def test_case_insensitive(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            result = ms.search_stations_by_name("CORVATSCH")
        self.assertIn("cov", result)

    def test_no_match_returns_empty_dict(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(METADATA_CSV)):
            result = ms.search_stations_by_name("bernina")
        self.assertEqual(result, {})


class GenericCsvParsingTests(unittest.TestCase):
    def test_parses_all_available_fields_by_default(self):
        result = ms.parse_generic_station_csv(STATION_CSV)
        self.assertIn("temperature_c", result["raw_column_map"])
        self.assertIn("wind_speed_ms", result["raw_column_map"])
        self.assertEqual(len(result["observations"]), 2)

    def test_wind_converted_from_kmh_to_ms(self):
        result = ms.parse_generic_station_csv(STATION_CSV)
        first = sorted(result["observations"].items())[0][1]
        self.assertAlmostEqual(first["wind_speed_ms"], 25.0 * 1000 / 3600, places=4)

    def test_confirmed_vs_unconfirmed_columns(self):
        result = ms.parse_generic_station_csv(STATION_CSV)
        self.assertIn("fu3010h0", result["confirmed_columns"])
        self.assertIn("fu3010h1", result["confirmed_columns"])
        self.assertIn("tre200h0", result["unconfirmed_columns"])

    def test_requested_variable_present_does_not_raise(self):
        result = ms.parse_generic_station_csv(STATION_CSV, requested_variables=["wind_speed_ms"])
        self.assertIn("wind_speed_ms", result["raw_column_map"])

    def test_requested_core_variable_missing_raises(self):
        csv_without_temp = "station_abbr;reference_timestamp;fu3010h0\nCOV;01.07.2026 06:00;25.0\n"
        with self.assertRaises(ValueError):
            ms.parse_generic_station_csv(csv_without_temp, requested_variables=["wind_speed_ms", "temperature_c"])

    def test_unrecognized_field_name_raises_when_requested(self):
        with self.assertRaises(ValueError):
            ms.parse_generic_station_csv(STATION_CSV, requested_variables=["not_a_real_field"])

    def test_missing_columns_never_silently_guessed(self):
        # A CSV with NO known columns at all - best-effort (no requested_variables)
        # must return an empty raw_column_map, never invent one.
        csv_text = "station_abbr;reference_timestamp;some_unknown_column\nCOV;01.07.2026 06:00;42\n"
        result = ms.parse_generic_station_csv(csv_text)
        self.assertEqual(result["raw_column_map"], {})


class CovFetchFixtureTests(unittest.TestCase):
    """COV recent-tail and historical-discovery fetch paths, both with
    requests fully mocked - fixture data only, no network."""

    def test_recent_fetch_uses_recent_url_only(self):
        with mock.patch("meteoswiss.requests.get", return_value=_fake_response(STATION_CSV)) as mock_get:
            result = ms.fetch_station_observations("cov", include_historical=False)
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(len(result["source_assets"]), 1)
        self.assertIn("cov_h_recent.csv", result["source_assets"][0])
        mock_get.assert_called_once()

    def test_historical_discovery_merges_multiple_files(self):
        stac_response = mock.Mock()
        stac_response.json.return_value = {
            "assets": {
                "ogd-smn_cov_h_historical_2020-2029.csv": {"href": "https://example/cov_h_historical.csv"},
                "ogd-smn_cov_h_recent.csv": {"href": "https://example/cov_h_recent.csv"},
            }
        }
        stac_response.raise_for_status = mock.Mock()

        historical_csv = "station_abbr;reference_timestamp;fu3010h0\nCOV;01.06.2026 06:00;10.0\n"

        def fake_get(url, timeout=None):
            if "stac" in url:
                return stac_response
            if "historical" in url:
                return _fake_response(historical_csv)
            return _fake_response(STATION_CSV)

        with mock.patch("meteoswiss.requests.get", side_effect=fake_get):
            result = ms.fetch_station_observations("cov", include_historical=True)
        self.assertEqual(len(result["observations"]), 3)  # 1 historical + 2 recent
        self.assertEqual(len(result["source_assets"]), 2)

    def test_fetch_failure_for_one_file_does_not_abort_others(self):
        import requests as real_requests

        stac_response = mock.Mock()
        stac_response.json.return_value = {
            "assets": {
                "ogd-smn_cov_h_historical_2020-2029.csv": {"href": "https://example/cov_h_historical.csv"},
                "ogd-smn_cov_h_recent.csv": {"href": "https://example/cov_h_recent.csv"},
            }
        }
        stac_response.raise_for_status = mock.Mock()

        def fake_get(url, timeout=None):
            if "stac" in url:
                return stac_response
            if "historical" in url:
                raise real_requests.RequestException("simulated failure")
            return _fake_response(STATION_CSV)

        with mock.patch("meteoswiss.requests.get", side_effect=fake_get):
            result = ms.fetch_station_observations("cov", include_historical=True)
        self.assertEqual(len(result["observations"]), 2)  # only the recent file succeeded


if __name__ == "__main__":
    unittest.main()
