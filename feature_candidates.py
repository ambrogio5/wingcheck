"""
feature_candidates.py - Phase 11: the explicit candidate-feature
promotion registry.

NOTHING in this file changes production behavior. features.FEATURE_NAMES
is still the single source of truth for the deployed schema, and
model.new_weights() still builds weights.json's schema from it - this
registry NEVER auto-populates FEATURE_NAMES. A feature only ever enters
production when a human manually edits features.py, bumps
model.SCHEMA_VERSION, and re-runs backtest.py - see PROMOTION_PROCESS
below for the full checklist that should happen first.
"""

from typing import NamedTuple


class CandidateFeature(NamedTuple):
    name: str
    description: str
    physical_rationale: str
    source_station_or_provider: str
    availability_cutoff: str
    coverage: float               # None if untested/unknown
    missing_data_behaviour: str
    research_status: str          # see RESEARCH_STATUSES below
    fold_results_summary: str
    reference_2026_result_summary: str
    approved_for_production: bool
    schema_version_introduced: int  # None until actually added to FEATURE_NAMES


RESEARCH_STATUSES = (
    "proposed",              # named and described, no real data yet
    "under_research",        # real data exists, being evaluated
    "validated_unstable",    # tested, does NOT show robust incremental value
    "validated_stable",      # tested, DOES show robust incremental value - ready for approval
    "rejected",              # will not be pursued (see rejection reason in the description)
)

PROMOTION_PROCESS = (
    "1. research candidate - name/description/physical rationale/source station "
    "recorded here with research_status='proposed'",
    "2. coverage validation - historical_data.py coverage must show real, sufficient "
    "(not synthetic, not guessed) historical data for the underlying station",
    "3. rolling-origin validation - station_analysis.py's incremental-value comparison "
    "must show a stable direction of effect across multiple rolling folds, not just "
    "the 2026 reference",
    "4. calibration check - calibration_analysis.py must not show the candidate "
    "degrading calibration (ECE/MCE) when added",
    "5. operational reliability check - the live nowcast fetch path (if this is a "
    "real-time feature) must be confirmed reliable: acceptable missing-data rate, "
    "acceptable latency relative to the 07:00/10:00 forecast issue times",
    "6. manual approval - a human reviews steps 2-5 and sets approved_for_production=True",
    "7. schema version bump - features.FEATURE_NAMES gains the new name, "
    "model.SCHEMA_VERSION is incremented, weights.json is reset via model.new_weights()",
    "8. deployment retrain - backtest.py is re-run so the deployed model actually "
    "learns a weight for the new feature",
    "9. post-deployment monitoring - refresh_dashboard.py's live_metrics is watched "
    "for any regression after the retrain",
)


# ---------------------------------------------------------------------------
# Already-in-production features flagged for RE-EVALUATION (not addition) -
# see station_analysis.py's real rolling-origin finding that neither shows
# robust incremental value.
# ---------------------------------------------------------------------------

EXISTING_FEATURE_REEVALUATIONS = {
    "samedan_morning_score": CandidateFeature(
        name="samedan_morning_score",
        description="Already in production (schema v3). Flagged here for "
                     "RE-EVALUATION given this session's rolling-origin findings.",
        physical_rationale="Real, measured upstream wind at Samedan ~07:00 local, "
                            "as a genuine same-day precursor signal.",
        source_station_or_provider="sam (verification=confirmed, meteoswiss)",
        availability_cutoff="07:00 Europe/Zurich",
        coverage=1.0,
        missing_data_behaviour="falls back to neutral 0.0 (features.py engineer_features)",
        research_status="validated_unstable",
        fold_results_summary=(
            "station_analysis.py rolling-origin: full_minus_samedan_morning shows NO "
            "consistent degradation vs full_current_model across any of the 5 rolling "
            "folds or the 2026 reference (sometimes a slight IMPROVEMENT) - see "
            "logs/historical/reports/station_analysis_*.json's "
            "rolling_origin_family_comparison.full_minus_samedan_morning."
        ),
        reference_2026_result_summary="Full-window ROC AUC 0.746 (minus) vs 0.747 (full) - a <0.001 difference.",
        approved_for_production=True,  # still deployed; NOT a recommendation to add, a flag for possible future removal
        schema_version_introduced=3,
    ),
    "pressure_nowcast_score": CandidateFeature(
        name="pressure_nowcast_score",
        description="Already in production (schema v3). Flagged here for "
                     "RE-EVALUATION given this session's rolling-origin findings.",
        physical_rationale="Real Lugano-Zurich pressure gradient measured this "
                            "morning, distinct from the forecast-based pressure_signal.",
        source_station_or_provider="lug + sma (verification=confirmed, meteoswiss)",
        availability_cutoff="07:00 Europe/Zurich",
        coverage=1.0,
        missing_data_behaviour="falls back to neutral 0.0 (features.py engineer_features)",
        research_status="validated_unstable",
        fold_results_summary=(
            "station_analysis.py rolling-origin: full_minus_pressure_nowcast shows NO "
            "consistent degradation vs full_current_model across any fold (sometimes a "
            "slight improvement) - see logs/historical/reports/station_analysis_*.json."
        ),
        reference_2026_result_summary="Full-window ROC AUC 0.7474 (minus) vs 0.747 (full) - negligible difference.",
        approved_for_production=True,
        schema_version_introduced=3,
    ),
}


# ---------------------------------------------------------------------------
# New candidates proposed this session - every one is "proposed" (not
# "under_research") because none has real historical data yet (see
# stations.py's verification field and historical_data.py's coverage report).
# ---------------------------------------------------------------------------

NEW_CANDIDATES = {
    "corvatsch_summit_wind_moderate": CandidateFeature(
        name="corvatsch_summit_wind_moderate",
        description="Morning summit wind speed at Corvatsch, transformed so moderate "
                     "wind scores positively and excessive wind scores negatively "
                     "(Phase 5.6's nonlinear-effects requirement).",
        physical_rationale="A moderate SW summit wind may indicate a supportive "
                            "synoptic flow reinforcing the thermal circulation; "
                            "excessive summit wind more likely indicates synoptic "
                            "override of the thermal rather than reinforcement.",
        source_station_or_provider="cor (verification=candidate_unconfirmed, meteoswiss) - see stations.py",
        availability_cutoff="07:00 Europe/Zurich (proposed - not yet implemented in features.py)",
        coverage=None,
        missing_data_behaviour="not yet implemented",
        research_status="proposed",
        fold_results_summary="Not testable - zero historical records for station 'cor' "
                              "(historical_data.py coverage). Network access to verify "
                              "this station and pull its history was unavailable this "
                              "session (see docs/STATION_RESEARCH.md).",
        reference_2026_result_summary="not available",
        approved_for_production=False,
        schema_version_introduced=None,
    ),
    "corvatsch_samedan_temp_diff": CandidateFeature(
        name="corvatsch_samedan_temp_diff",
        description="Corvatsch minus Samedan morning temperature - a vertical "
                     "lapse-rate / airmass-stability proxy.",
        physical_rationale="A steeper-than-normal vertical temperature gradient can "
                            "indicate a more unstable, storm-prone airmass (echoing "
                            "the existing freezing_level_score/cape_penalty features "
                            "but from a real local vertical measurement pair rather "
                            "than a forecast-model column).",
        source_station_or_provider="cor + sam (cor unconfirmed, sam confirmed)",
        availability_cutoff="07:00 Europe/Zurich (proposed)",
        coverage=None,
        missing_data_behaviour="not yet implemented",
        research_status="proposed",
        fold_results_summary="Not testable - 'cor' has zero historical records.",
        reference_2026_result_summary="not available",
        approved_for_production=False,
        schema_version_introduced=None,
    ),
    "corvatsch_samedan_wind_shear": CandidateFeature(
        name="corvatsch_samedan_wind_shear",
        description="Corvatsch minus Samedan wind-vector components (u/v) - a "
                     "vertical wind-shear proxy.",
        physical_rationale="Large shear between the summit and the valley floor may "
                            "indicate the summit flow is decoupled from the valley "
                            "thermal circulation, weakening the expected reinforcement.",
        source_station_or_provider="cor + sam (cor unconfirmed, sam confirmed)",
        availability_cutoff="07:00 Europe/Zurich (proposed)",
        coverage=None,
        missing_data_behaviour="not yet implemented",
        research_status="proposed",
        fold_results_summary="Not testable - 'cor' has zero historical records.",
        reference_2026_result_summary="not available",
        approved_for_production=False,
        schema_version_introduced=None,
    ),
    "piz_nair_wind_shear": CandidateFeature(
        name="piz_nair_wind_shear",
        description="Piz Nair vs Samedan wind-vector shear - a second, independent "
                     "summit-shear estimate near St. Moritz.",
        physical_rationale="Same rationale as corvatsch_samedan_wind_shear; a second "
                            "nearby summit would let a future analysis check whether "
                            "any shear signal is robust across summits or an artifact "
                            "of one station's specific exposure.",
        source_station_or_provider="piz_nair (verification=needs_discovery - no station "
                                    "code could even be proposed this session)",
        availability_cutoff="unknown",
        coverage=None,
        missing_data_behaviour="not yet implemented",
        research_status="proposed",
        fold_results_summary="Not testable - station identity itself is unresolved.",
        reference_2026_result_summary="not available",
        approved_for_production=False,
        schema_version_introduced=None,
    ),
    "bregaglia_real_station_heating": CandidateFeature(
        name="bregaglia_real_station_heating",
        description="A REAL Bregaglia ground-station morning temperature/warming "
                     "rate, if one is ever confirmed to exist (Vicosoprano/Bondo/"
                     "Soglio/Castasegna/Maloja).",
        physical_rationale="thermal_excess (the existing production feature) is "
                            "entirely forecast-model-based (Open-Meteo's Bregaglia "
                            "grid point). A real station reading would give a genuine "
                            "same-morning nowcast of the actual thermal driver, "
                            "analogous to what samedan_morning_score already does for "
                            "the wind side.",
        source_station_or_provider="maloja (candidate_unconfirmed, moderate confidence) "
                                    "or vicosoprano/bondo/soglio/castasegna (all "
                                    "needs_discovery) - see stations.py",
        availability_cutoff="07:00 Europe/Zurich (proposed)",
        coverage=None,
        missing_data_behaviour="not yet implemented",
        research_status="proposed",
        fold_results_summary="Not testable - no confirmed real Bregaglia ground "
                              "station in this project as of this session. This is "
                              "the single highest-priority station gap identified - "
                              "see docs/STATION_RESEARCH.md's recommended next experiment.",
        reference_2026_result_summary="not available",
        approved_for_production=False,
        schema_version_introduced=None,
    ),
    "davos_chur_pressure_context": CandidateFeature(
        name="davos_chur_pressure_context",
        description="Davos/Chur real pressure observations, extending the existing "
                     "Lugano-Zurich pressure-gradient nowcast with a northeast axis.",
        physical_rationale="A north-south (Lugano-Zurich) gradient is already used; "
                            "adding a real northeast-context station could help "
                            "disambiguate synoptic patterns that look similar on the "
                            "Lugano-Zurich axis alone (e.g. distinguishing a clean "
                            "south-north gradient from a more complex rotational pattern).",
        source_station_or_provider="davos + chur (both candidate_unconfirmed, moderate confidence)",
        availability_cutoff="07:00 Europe/Zurich (proposed)",
        coverage=None,
        missing_data_behaviour="not yet implemented",
        research_status="proposed",
        fold_results_summary="Not testable - zero historical records for either station.",
        reference_2026_result_summary="not available",
        approved_for_production=False,
        schema_version_introduced=None,
    ),
}


CANDIDATES = {**EXISTING_FEATURE_REEVALUATIONS, **NEW_CANDIDATES}


def promotable_candidates() -> dict:
    """Candidates that have cleared research_status='validated_stable' AND
    are not yet approved - i.e. ready for a human to review step 6 of
    PROMOTION_PROCESS. Empty as of this session (every new candidate is
    still 'proposed', and the two re-evaluated existing features are
    'validated_unstable', the opposite of a promotion signal)."""
    return {name: c for name, c in CANDIDATES.items()
            if c.research_status == "validated_stable" and not c.approved_for_production}
