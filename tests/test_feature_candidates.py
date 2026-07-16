"""Offline tests for feature_candidates.py: the registry is well-formed,
no feature here silently affects production, and promotion status logic
is honest (nothing auto-approved without real evidence)."""

import unittest

from feature_candidates import (
    CANDIDATES, RESEARCH_STATUSES, PROMOTION_PROCESS, promotable_candidates,
)
from features import FEATURE_NAMES


class RegistryWellFormedTests(unittest.TestCase):
    def test_every_candidate_has_a_valid_research_status(self):
        for name, c in CANDIDATES.items():
            self.assertIn(c.research_status, RESEARCH_STATUSES, name)

    def test_every_candidate_has_nonempty_rationale_and_description(self):
        for name, c in CANDIDATES.items():
            self.assertTrue(c.description.strip(), name)
            self.assertTrue(c.physical_rationale.strip(), name)

    def test_promotion_process_has_nine_documented_steps(self):
        self.assertEqual(len(PROMOTION_PROCESS), 9)


class HonestyInvariantTests(unittest.TestCase):
    def test_no_new_candidate_is_approved_for_production(self):
        """Every genuinely NEW candidate (not already in FEATURE_NAMES)
        must be approved_for_production=False - nothing here silently
        promotes itself."""
        for name, c in CANDIDATES.items():
            if name not in FEATURE_NAMES:
                self.assertFalse(c.approved_for_production, f"{name} must not be pre-approved")
                self.assertIsNone(c.schema_version_introduced, f"{name} must not claim a schema version")

    def test_untested_candidates_have_no_coverage_claim(self):
        for name, c in CANDIDATES.items():
            if c.research_status == "proposed":
                self.assertIsNone(c.coverage, f"{name} is 'proposed' but claims a coverage value")

    def test_reevaluated_existing_features_are_actually_in_production(self):
        for name, c in CANDIDATES.items():
            if name in FEATURE_NAMES:
                self.assertTrue(c.approved_for_production)
                self.assertIsNotNone(c.schema_version_introduced)

    def test_promotable_candidates_are_validated_stable_and_unapproved(self):
        for name, c in promotable_candidates().items():
            self.assertEqual(c.research_status, "validated_stable")
            self.assertFalse(c.approved_for_production)

    def test_no_candidate_is_currently_promotable(self):
        # As of this session, every new candidate is "proposed" (no real
        # data) and the two re-evaluated existing features are
        # "validated_unstable" - the registry must not claim otherwise.
        self.assertEqual(promotable_candidates(), {})


if __name__ == "__main__":
    unittest.main()
