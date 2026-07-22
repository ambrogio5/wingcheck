import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import water_temperature as wt


class WaterTemperatureTests(unittest.TestCase):
    def test_parses_explicit_water_temperature_heading(self):
        page = '<h2>Aktuelle Wassertemperatur im Silvaplanersee: 17 °C</h2>'
        self.assertEqual(wt.parse_temperature(page), 17.0)

    def test_does_not_accept_unlabelled_air_temperature(self):
        with self.assertRaises(ValueError):
            wt.parse_temperature('<div class="weather">17.4 °C</div>')

    def test_fetch_marks_value_as_estimated_and_keeps_source(self):
        response = mock.Mock(text='<h2>Aktuelle Wassertemperatur im Silvaplanersee: 16,5 °C</h2>')
        response.raise_for_status.return_value = None
        now = datetime(2026, 7, 20, 19, 0, tzinfo=timezone.utc)
        with mock.patch.object(wt.requests, "get", return_value=response):
            reading = wt.fetch_current_reading(now=now)
        self.assertEqual(reading["temp_c"], 16.5)
        self.assertTrue(reading["estimated"])
        self.assertEqual(reading["source_url"], wt.URL)
        self.assertEqual(reading["retrieved_at"], now.isoformat())

    def test_write_latest_is_readable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latest.json"
            wt.write_latest({"temp_c": 17.0}, path)
            self.assertIn('"temp_c": 17.0', path.read_text())
