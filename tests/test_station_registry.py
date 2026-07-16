"""Offline tests for station_registry.py: registry well-formedness and the
honesty invariants that keep unverified candidates from being silently
treated as real (no network calls - reads config/stations.json only)."""

import unittest

import station_registry as sr


class RegistryLoadTests(unittest.TestCase):
    def setUp(self):
        self.stations = sr.load_registry()

    def test_loads_at_least_the_three_confirmed_stations(self):
        for sid in ("sam", "lug", "sma"):
            self.assertIn(sid, self.stations)

    def test_every_station_has_required_fields(self):
        for sid, s in self.stations.items():
            self.assertTrue(s.station_id)
            self.assertTrue(s.name)
            self.assertTrue(s.roles, f"{sid} has no roles")

    def test_no_validation_problems(self):
        problems = sr.validate_registry(self.stations)
        self.assertEqual(problems, [])


class HonestyInvariantTests(unittest.TestCase):
    def setUp(self):
        self.stations = sr.load_registry()

    def test_confirmed_set_is_exactly_sam_lug_sma(self):
        confirmed = {sid for sid, s in self.stations.items() if s.verification == "confirmed"}
        self.assertEqual(confirmed, {"sam", "lug", "sma"})

    def test_enabled_stations_are_all_confirmed(self):
        for sid, s in self.stations.items():
            if s.enabled:
                self.assertEqual(s.verification, "confirmed",
                                  f"{sid} is enabled but not confirmed - real fetch required first")

    def test_unverified_stations_are_never_enabled(self):
        for sid, s in self.stations.items():
            if s.verification == "unverified":
                self.assertFalse(s.enabled, f"{sid} is unverified but enabled")

    def test_validate_registry_flags_enabled_unconfirmed_station(self):
        bad = dict(self.stations)
        bad["fake"] = sr.Station(
            station_id="fake", name="Fake", provider="unknown", latitude=None, longitude=None,
            elevation_m=None, roles=("summit",), available_variables=(), historical_available=None,
            live_available=None, licence="unknown", reporting_delay_minutes=None,
            enabled=True, verification="unverified", notes="",
        )
        problems = sr.validate_registry(bad)
        self.assertTrue(any("fake" in p for p in problems))

    def test_validate_registry_flags_unknown_role(self):
        bad = dict(self.stations)
        bad["fake2"] = sr.Station(
            station_id="fake2", name="Fake2", provider="unknown", latitude=None, longitude=None,
            elevation_m=None, roles=("not_a_real_role",), available_variables=(), historical_available=None,
            live_available=None, licence="unknown", reporting_delay_minutes=None,
            enabled=False, verification="unverified", notes="",
        )
        problems = sr.validate_registry(bad)
        self.assertTrue(any("unknown role" in p for p in problems))


class LookupHelperTests(unittest.TestCase):
    def setUp(self):
        self.stations = sr.load_registry()

    def test_enabled_station_ids(self):
        ids = sr.enabled_station_ids(self.stations)
        self.assertEqual(set(ids), {"sam", "lug", "sma"})

    def test_stations_by_role(self):
        pressure_stations = sr.stations_by_role("synoptic_pressure", self.stations)
        self.assertIn("lug", pressure_stations)
        self.assertIn("sma", pressure_stations)

    def test_stations_by_provider(self):
        meteoswiss_stations = sr.stations_by_provider("meteoswiss", self.stations)
        self.assertIn("sam", meteoswiss_stations)

    def test_get_station(self):
        s = sr.get_station("sam", self.stations)
        self.assertEqual(s.name, "Samedan")


if __name__ == "__main__":
    unittest.main()
