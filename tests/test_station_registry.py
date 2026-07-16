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


class CovRegistrationTests(unittest.TestCase):
    """Corvatsch (COV) registration: identity confirmed, but not yet
    enabled/verification=confirmed since no real fetch has succeeded in
    this repo's own environment yet - see docs/STATION_RESEARCH.md."""

    def setUp(self):
        self.stations = sr.load_registry()

    def test_cov_is_registered(self):
        self.assertIn("cov", self.stations)

    def test_cov_has_summit_and_competing_flow_roles(self):
        cov = self.stations["cov"]
        self.assertIn("summit", cov.roles)
        self.assertIn("competing_flow", cov.roles)

    def test_cov_is_not_yet_enabled_or_confirmed(self):
        cov = self.stations["cov"]
        self.assertFalse(cov.enabled)
        self.assertEqual(cov.verification, "unverified")

    def test_cov_coordinates_and_elevation_are_null_pending_real_fetch(self):
        cov = self.stations["cov"]
        self.assertIsNone(cov.latitude)
        self.assertIsNone(cov.longitude)
        self.assertIsNone(cov.elevation_m)

    def test_old_invented_cor_id_no_longer_present(self):
        # An earlier iteration of this registry used the wrong, invented
        # abbreviation "cor" for the same physical station (Piz Corvatsch) -
        # the real official abbreviation is "cov".
        self.assertNotIn("cor", self.stations)

    def test_piz_nair_still_not_enabled(self):
        piz_nair = self.stations["piz_nair"]
        self.assertFalse(piz_nair.enabled)
        self.assertEqual(piz_nair.verification, "unverified")


class MergeOfficialMetadataTests(unittest.TestCase):
    """The 'official metadata overrides provisional values' mechanism -
    exercised directly rather than via a real fetch (which is blocked in
    this sandbox - see docs/STATION_RESEARCH.md)."""

    def setUp(self):
        self.stations = sr.load_registry()

    def test_official_metadata_overrides_null_placeholders(self):
        cov = self.stations["cov"]
        official = {"latitude": 46.4144, "longitude": 9.8214, "elevation_m": 3295.0, "name": "Piz Corvatsch"}
        merged = sr.merge_official_metadata(cov, official)
        self.assertEqual(merged.latitude, 46.4144)
        self.assertEqual(merged.longitude, 9.8214)
        self.assertEqual(merged.elevation_m, 3295.0)

    def test_missing_official_fields_keep_the_provisional_value(self):
        cov = self.stations["cov"]
        merged = sr.merge_official_metadata(cov, {"latitude": 46.4144})
        self.assertEqual(merged.latitude, 46.4144)
        self.assertIsNone(merged.longitude)  # not in official_metadata - stays as it was

    def test_merge_does_not_mutate_the_original_entry(self):
        cov = self.stations["cov"]
        sr.merge_official_metadata(cov, {"latitude": 46.4144})
        self.assertIsNone(cov.latitude)  # original untouched (NamedTuple is immutable anyway, but confirm no aliasing bugs)

    def test_merge_never_flips_enabled_or_verification(self):
        cov = self.stations["cov"]
        merged = sr.merge_official_metadata(cov, {"latitude": 46.4144, "longitude": 9.8214, "elevation_m": 3295.0})
        self.assertEqual(merged.enabled, cov.enabled)
        self.assertEqual(merged.verification, cov.verification)


class PizNairCannotBeEnabledTests(unittest.TestCase):
    """Part 7's explicit constraint: piz_nair may only be enabled once a
    verified provider is confirmed - validate_registry() must reject any
    attempt to flip it (or any unverified station) to enabled without
    also being confirmed."""

    def test_enabling_piz_nair_without_confirming_it_fails_validation(self):
        stations = sr.load_registry()
        tampered = dict(stations)
        tampered["piz_nair"] = stations["piz_nair"]._replace(enabled=True)
        problems = sr.validate_registry(tampered)
        self.assertTrue(any("piz_nair" in p for p in problems))

    def test_enabling_piz_nair_with_confirmed_verification_passes(self):
        # Sanity check that the invariant is specifically about the
        # confirmed/enabled pairing, not piz_nair by name - also needs
        # historical_available/live_available set, per validate_registry()'s
        # separate "enabled but no data availability declared" check.
        stations = sr.load_registry()
        tampered = dict(stations)
        tampered["piz_nair"] = stations["piz_nair"]._replace(
            enabled=True, verification="confirmed", live_available=True)
        problems = sr.validate_registry(tampered)
        self.assertFalse(any("piz_nair" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
