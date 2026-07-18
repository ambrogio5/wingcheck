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

    def test_confirmed_set_includes_official_sia(self):
        # cov joined this set 2026-07-17 after a real network-enabled fetch
        # confirmed both its official metadata and 398,816 real historical
        # records - see config/stations.json's cov entry and
        # docs/STATION_RESEARCH.md for the full evidence. sils joined the
        # same day via a real user-provided historical CSV (22 records,
        # 2014-04-02) - a different kind of "confirmed" (direct data
        # inspection, not a live provider fetch), see docs/STATION_RESEARCH.md's
        # "Sils / Segl (Silser See) manual import" section.
        confirmed = {sid for sid, s in self.stations.items() if s.verification == "confirmed"}
        self.assertEqual(confirmed, {"sam", "sia", "lug", "sma", "cov", "sils"})

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
        self.assertEqual(set(ids), {"sam", "sia", "lug", "sma", "cov", "sils"})

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
    """Corvatsch (COV) registration: identity, metadata, AND a real
    historical data fetch were all confirmed via real network-enabled
    GitHub Actions runs (2026-07-16/17) - see docs/STATION_RESEARCH.md and
    config/stations.json's cov entry for the full evidence. cov is now
    genuinely enabled/confirmed, not just identity-confirmed."""

    def setUp(self):
        self.stations = sr.load_registry()

    def test_cov_is_registered(self):
        self.assertIn("cov", self.stations)

    def test_cov_has_summit_and_competing_flow_roles(self):
        cov = self.stations["cov"]
        self.assertIn("summit", cov.roles)
        self.assertIn("competing_flow", cov.roles)

    def test_cov_is_enabled_and_confirmed(self):
        cov = self.stations["cov"]
        self.assertTrue(cov.enabled)
        self.assertEqual(cov.verification, "confirmed")

    def test_cov_coordinates_and_elevation_are_the_real_confirmed_values(self):
        cov = self.stations["cov"]
        self.assertAlmostEqual(cov.latitude, 46.418039)
        self.assertAlmostEqual(cov.longitude, 9.821308)
        self.assertAlmostEqual(cov.elevation_m, 3294.0)

    def test_cov_available_variables_reflect_real_confirmed_evidence_only(self):
        cov = self.stations["cov"]
        for expected in ("wind_speed_ms", "wind_gust_ms", "global_radiation_wm2",
                         "relative_humidity_pct", "sunshine_duration_min"):
            self.assertIn(expected, cov.available_variables)
        # Fields never independently confirmed present must not be assumed.
        for unconfirmed in ("temperature_c", "dew_point_c", "precipitation_mm",
                             "pressure_sea_level_hpa", "wind_direction_deg"):
            self.assertNotIn(unconfirmed, cov.available_variables)

    def test_old_invented_cor_id_no_longer_present(self):
        # An earlier iteration of this registry used the wrong, invented
        # abbreviation "cor" for the same physical station (Piz Corvatsch) -
        # the real official abbreviation is "cov".
        self.assertNotIn("cor", self.stations)

    def test_piz_nair_still_not_enabled(self):
        piz_nair = self.stations["piz_nair"]
        self.assertFalse(piz_nair.enabled)
        self.assertEqual(piz_nair.verification, "unverified")


class BerninaPassRegistrationTests(unittest.TestCase):
    """Passo del Bernina (BEH): identity confirmed via a real
    meteoswiss.search_stations_by_name('bernina') run (2026-07-17,
    GitHub Actions) - a genuine official MeteoSwiss station, never a
    guessed abbreviation. Data sync deliberately deferred - not attempted
    this session, so it stays unverified/disabled exactly like COV did
    before its own data fetch succeeded."""

    def setUp(self):
        self.stations = sr.load_registry()

    def test_beh_is_registered(self):
        self.assertIn("beh", self.stations)

    def test_beh_has_pass_and_competing_flow_roles(self):
        beh = self.stations["beh"]
        self.assertIn("pass", beh.roles)
        self.assertIn("competing_flow", beh.roles)

    def test_beh_coordinates_and_elevation_are_the_real_confirmed_values(self):
        beh = self.stations["beh"]
        self.assertAlmostEqual(beh.latitude, 46.409158)
        self.assertAlmostEqual(beh.longitude, 10.019567)
        self.assertAlmostEqual(beh.elevation_m, 2260.0)

    def test_beh_is_not_yet_enabled_or_confirmed(self):
        # Identity/metadata confirmed, but no historical data sync has
        # been attempted yet - enabling requires a real sync, per this
        # registry's own bar for every station.
        beh = self.stations["beh"]
        self.assertFalse(beh.enabled)
        self.assertEqual(beh.verification, "unverified")

    def test_beh_available_variables_empty_pending_data_sync(self):
        beh = self.stations["beh"]
        self.assertEqual(beh.available_variables, ())


class SilsRegistrationTests(unittest.TestCase):
    """Sils / Segl (Silser See): the only confirmed station in this
    registry with NO live/API source at all - confirmed via a real
    user-provided historical CSV (22 hourly records, 2014-04-02), not a
    MeteoSwiss fetch. See docs/STATION_RESEARCH.md's "Sils / Segl (Silser
    See) manual import" section and historical_data.NO_LIVE_SOURCE_STATIONS."""

    def setUp(self):
        self.stations = sr.load_registry()

    def test_sils_is_registered(self):
        self.assertIn("sils", self.stations)

    def test_sils_has_target_region_role(self):
        self.assertIn("target_region", self.stations["sils"].roles)

    def test_sils_is_enabled_and_confirmed(self):
        sils = self.stations["sils"]
        self.assertTrue(sils.enabled)
        self.assertEqual(sils.verification, "confirmed")

    def test_sils_historical_available_but_not_live(self):
        sils = self.stations["sils"]
        self.assertTrue(sils.historical_available)
        self.assertFalse(sils.live_available)

    def test_sils_provider_is_not_meteoswiss(self):
        # This is real data, but not from MeteoSwiss - the provider field
        # must say so honestly, never claim MeteoSwiss provenance for data
        # that didn't come from there.
        self.assertNotEqual(self.stations["sils"].provider, "meteoswiss")

    def test_sils_is_in_no_live_source_stations(self):
        import historical_data as hd
        self.assertIn("sils", hd.NO_LIVE_SOURCE_STATIONS)


class MergeOfficialMetadataTests(unittest.TestCase):
    """The 'official metadata overrides provisional values' mechanism -
    exercised against a synthetic provisional station (constructed
    directly, not pulled from the real registry - cov itself is now
    confirmed/enabled with real metadata already filled in, following
    exactly this merge process for real, so its own placeholder state no
    longer exists to test against; beh is the current still-provisional
    example, but a synthetic fixture keeps this test independent of
    whichever real station happens to be mid-bootstrap at any given time)."""

    def _provisional_station(self, station_id="candidate"):
        return sr.Station(
            station_id=station_id, name="Provisional Candidate", provider="meteoswiss",
            latitude=None, longitude=None, elevation_m=None, roles=("summit",),
            available_variables=(), historical_available=None, live_available=None,
            licence="unknown", reporting_delay_minutes=15, enabled=False,
            verification="unverified", notes="",
        )

    def test_official_metadata_overrides_null_placeholders(self):
        candidate = self._provisional_station()
        official = {"latitude": 46.4144, "longitude": 9.8214, "elevation_m": 3295.0, "name": "Real Name"}
        merged = sr.merge_official_metadata(candidate, official)
        self.assertEqual(merged.latitude, 46.4144)
        self.assertEqual(merged.longitude, 9.8214)
        self.assertEqual(merged.elevation_m, 3295.0)

    def test_missing_official_fields_keep_the_provisional_value(self):
        candidate = self._provisional_station()
        merged = sr.merge_official_metadata(candidate, {"latitude": 46.4144})
        self.assertEqual(merged.latitude, 46.4144)
        self.assertIsNone(merged.longitude)  # not in official_metadata - stays as it was

    def test_merge_does_not_mutate_the_original_entry(self):
        candidate = self._provisional_station()
        sr.merge_official_metadata(candidate, {"latitude": 46.4144})
        self.assertIsNone(candidate.latitude)  # original untouched (NamedTuple is immutable anyway, but confirm no aliasing bugs)

    def test_merge_never_flips_enabled_or_verification(self):
        candidate = self._provisional_station()
        merged = sr.merge_official_metadata(candidate, {"latitude": 46.4144, "longitude": 9.8214, "elevation_m": 3295.0})
        self.assertEqual(merged.enabled, candidate.enabled)
        self.assertEqual(merged.verification, candidate.verification)


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
