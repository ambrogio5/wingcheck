"""
session_forecast.py - deterministic session-level summary derived from an
existing day's already-scored hourly forecasts (forecast_and_log.py's
per-hour records). Research/dashboard support only - does not change how
individual hours are scored.

RESOLUTION: every timestamp in the output is one of the input hourly
target_time values verbatim - this module never interpolates or implies
sub-hour precision it doesn't have. "likely_onset_start"/"best_window_end"
etc. are always exactly one of the hourly boundaries already present in
the input.

"Rideable" (used throughout) means tier != "UNLIKELY", matching the
existing GOOD/MARGINAL alert tiers already calibrated by backtest.py -
this module introduces no new threshold of its own.

event_probability is the MAX hourly probability across the day - the same
convention already validated as matching-or-beating a dedicated daily
model in prior research (see CLAUDE.md's session-target findings); this
module doesn't reinvent that choice, just names it at the session level.

CONFIDENCE RULES (deterministic, see _confidence_label): start at 1.0 and
subtract a fixed penalty for each of: high model spread (low
model_agreement input), a flat/inconclusive hourly probability curve, any
missing station input, a genuine disagreement between diagnostic-family
statuses, and stale input data. The result is clipped to [0, 1] and
labeled "high"/"medium"/"low" via fixed cutoffs - never a hidden ML model,
so the reasoning is always inspectable.
"""

import statistics

RIDEABLE_TIERS = ("GOOD", "MARGINAL")

FLAT_CURVE_STDDEV_THRESHOLD = 0.05
STALE_DATA_MINUTES = 90.0
LOW_MODEL_AGREEMENT_THRESHOLD = 0.5

PENALTY_HIGH_MODEL_SPREAD = 0.30
PENALTY_FLAT_CURVE = 0.30
PENALTY_MISSING_STATION_INPUT = 0.30
PENALTY_CONFLICTING_DIAGNOSTICS = 0.30
PENALTY_STALE_DATA = 0.30

CONFIDENCE_HIGH_CUTOFF = 0.75
CONFIDENCE_MEDIUM_CUTOFF = 0.45

_UNFAVOURABLE_STATUSES = {"unfavourable", "opposing", "easterly", "northerly", "misaligned_shear", "excessive"}
_FAVOURABLE_STATUSES = {"favourable", "supportive", "clear"}


def _is_rideable(hour: dict) -> bool:
    return hour.get("tier") in RIDEABLE_TIERS


def _confidence_label(score: float) -> str:
    if score >= CONFIDENCE_HIGH_CUTOFF:
        return "high"
    if score >= CONFIDENCE_MEDIUM_CUTOFF:
        return "medium"
    return "low"


def _diagnostics_conflict(diagnostics: dict) -> bool:
    if not diagnostics:
        return False
    statuses = {d.get("status") for d in diagnostics.values() if not d.get("missing")}
    has_favourable = bool(statuses & _FAVOURABLE_STATUSES)
    has_unfavourable = bool(statuses & _UNFAVOURABLE_STATUSES)
    return has_favourable and has_unfavourable


def build_session_forecast(hourly_predictions: list, diagnostics: dict = None,
                             model_agreement: float = None, station_data_missing: bool = False,
                             data_age_minutes: float = 0.0) -> dict:
    """hourly_predictions: one calendar day's chronologically-sorted hourly
    forecast records (each with at least target_time/probability/tier/
    model_wind_kt/model_gust_kt). Returns the session-level summary dict."""
    if not hourly_predictions:
        return {
            "likely_onset_start": None, "likely_onset_end": None,
            "best_window_start": None, "best_window_end": None,
            "peak_hour": None,
            "expected_wind_min_kt": None, "expected_wind_max_kt": None,
            "expected_gust_min_kt": None, "expected_gust_max_kt": None,
            "expected_rideable_hours": 0,
            "likely_decline_time": None,
            "event_probability": 0.0,
            "timing_confidence": "low", "strength_confidence": "low",
            "model_agreement": model_agreement,
        }

    hours = sorted(hourly_predictions, key=lambda h: h["target_time"])
    rideable = [h for h in hours if _is_rideable(h)]
    probabilities = [h["probability"] for h in hours]

    peak = max(hours, key=lambda h: h["probability"])
    event_probability = peak["probability"]

    likely_onset_start = rideable[0]["target_time"] if rideable else None
    likely_onset_end = hours[hours.index(rideable[0]) + 1]["target_time"] \
        if rideable and hours.index(rideable[0]) + 1 < len(hours) else likely_onset_start
    likely_decline_time = rideable[-1]["target_time"] if rideable else None

    best_window_start = best_window_end = None
    if rideable:
        # contiguous rideable block containing the peak hour, if the peak
        # itself is rideable; otherwise the single largest contiguous
        # rideable block.
        blocks = []
        current = [rideable[0]]
        for h in rideable[1:]:
            prev_idx = hours.index(current[-1])
            this_idx = hours.index(h)
            if this_idx == prev_idx + 1:
                current.append(h)
            else:
                blocks.append(current)
                current = [h]
        blocks.append(current)
        target_block = next((b for b in blocks if peak in b), max(blocks, key=len))
        best_window_start = target_block[0]["target_time"]
        best_window_end = target_block[-1]["target_time"]

    wind_values = [h["model_wind_kt"] for h in (rideable or hours)]
    gust_values = [h["model_gust_kt"] for h in (rideable or hours)]

    high_model_spread = model_agreement is not None and model_agreement < LOW_MODEL_AGREEMENT_THRESHOLD
    flat_curve = len(probabilities) > 1 and statistics.pstdev(probabilities) < FLAT_CURVE_STDDEV_THRESHOLD
    conflicting = _diagnostics_conflict(diagnostics)
    stale = bool(data_age_minutes and data_age_minutes > STALE_DATA_MINUTES)

    # Shared penalties apply to both; timing confidence is additionally hurt
    # by a flat curve (can't pinpoint a peak hour), strength confidence by
    # staleness (the reported kt range itself might be out of date).
    shared_penalty = (
        (PENALTY_HIGH_MODEL_SPREAD if high_model_spread else 0.0)
        + (PENALTY_MISSING_STATION_INPUT if station_data_missing else 0.0)
        + (PENALTY_CONFLICTING_DIAGNOSTICS if conflicting else 0.0)
    )
    timing_score = 1.0 - shared_penalty - (PENALTY_FLAT_CURVE if flat_curve else 0.0)
    strength_score = 1.0 - shared_penalty - (PENALTY_STALE_DATA if stale else 0.0)
    timing_score = max(0.0, timing_score)
    strength_score = max(0.0, strength_score)

    return {
        "likely_onset_start": likely_onset_start,
        "likely_onset_end": likely_onset_end,
        "best_window_start": best_window_start,
        "best_window_end": best_window_end,
        "peak_hour": peak["target_time"],
        "expected_wind_min_kt": round(min(wind_values), 1) if wind_values else None,
        "expected_wind_max_kt": round(max(wind_values), 1) if wind_values else None,
        "expected_gust_min_kt": round(min(gust_values), 1) if gust_values else None,
        "expected_gust_max_kt": round(max(gust_values), 1) if gust_values else None,
        "expected_rideable_hours": len(rideable),
        "likely_decline_time": likely_decline_time,
        "event_probability": round(event_probability, 3),
        "timing_confidence": _confidence_label(timing_score),
        "strength_confidence": _confidence_label(strength_score),
        "model_agreement": model_agreement,
    }
