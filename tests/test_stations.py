"""Offline tests for stations.py's registry: every entry is well-formed,
verification status is honest (confirmed stations really do have a
justification, unconfirmed ones are clearly labeled), and helper lookups
work correctly. No network calls."""

import unittest

import stations as station_module
from stations import STATIONS, REJECTED, Station, confirmed_station_ids, stations_by_role, stations_by_provider

VALID_VERIFICATIONS = {"confirmed", "candidate_unconfirmed", "needs_discovery"}
VALID_CONFIDENCE = {"high", "moderate", "low", "n/a"}


class RegistryWellFormedTests(unittest.TestCase):
    def test_every_entry_is_a_station_namedtuple(self):
        for sid, s in STATIONS.items():
            self.assertIsInstance(s, Station, f"{sid} is not a Station")

    def test_verification_values_are_valid(self):
        for sid, s in STATIONS.items():
            self.assertIn(s.verification, VALID_VERIFICATIONS, f"{sid} has invalid verification")

    def test_confidence_values_are_valid(self):
        for sid, s in STATIONS.items():
            self.assertIn(s.confidence, VALID_CONFIDENCE, f"{sid} has invalid confidence")

    def test_every_entry_has_a_nonempty_verification_note(self):
        for sid, s in STATIONS.items():
            self.assertTrue(s.verification_note.strip(), f"{sid} has an empty verification_note")

    def test_no_duplicate_station_ids_between_stations_and_rejected(self):
        self.assertEqual(set(STATIONS) & set(REJECTED), set())


class HonestyInvariantTests(unittest.TestCase):
    """The single most important property of this registry: confirmed
    status must be earned, not assumed."""

    def test_confirmed_stations_are_exactly_the_ones_with_real_cached_data(self):
        # This is intentionally a hardcoded expectation, not derived from
        # the registry itself - it must match logs/raw_cache/*.json, which
        # are the only three station data sources actually fetched and
        # parsed in this project as of this session.
        self.assertEqual(set(confirmed_station_ids()), {"sam", "lug", "sma"})

    def test_confirmed_stations_have_high_confidence(self):
        for sid in confirmed_station_ids():
            self.assertEqual(STATIONS[sid].confidence, "high")

    def test_unconfirmed_stations_are_not_marked_suitable_for_backtesting(self):
        for sid, s in STATIONS.items():
            if s.verification != "confirmed":
                self.assertNotEqual(s.suitable_for_backtesting, True,
                                     f"{sid} is unconfirmed but marked suitable_for_backtesting=True")

    def test_needs_discovery_stations_have_na_confidence(self):
        for sid, s in STATIONS.items():
            if s.verification == "needs_discovery":
                self.assertEqual(s.confidence, "n/a", f"{sid} needs_discovery but has a confidence claim")


class LookupHelperTests(unittest.TestCase):
    def test_stations_by_role_returns_only_matching(self):
        result = stations_by_role("morning_nowcast")
        self.assertIn("sam", result)
        for sid, s in result.items():
            self.assertIn("morning_nowcast", s.roles)

    def test_stations_by_provider_returns_only_matching(self):
        result = stations_by_provider("meteoswiss")
        for sid, s in result.items():
            self.assertEqual(s.provider, "meteoswiss")
        self.assertIn("sam", result)

    def test_stations_by_role_empty_for_unknown_role(self):
        self.assertEqual(stations_by_role("no_such_role"), {})


class RejectedStationsTests(unittest.TestCase):
    def test_rejected_stations_have_a_reason(self):
        for sid, s in REJECTED.items():
            self.assertTrue(s.rejected)
            self.assertTrue(s.rejection_reason.strip(), f"{sid} rejected with no reason")


if __name__ == "__main__":
    unittest.main()
