"""
regimes.py - transparent, rule-based weather-regime classification
(Phase 10), used to break down model performance by the physical
situation rather than treating every hour as interchangeable.

Rules operate on the SAME engineered features already in
logs/backtest_dataset.jsonl (thermal_excess, pressure_signal,
upper_wind_alignment, upper_wind_speed_score, cape_penalty,
precip_penalty, surface_dir_alignment) - no new data source required.
Deliberately simple thresholds on already-normalized (roughly -1..+1)
features, chosen to be readable and adjustable, not fit/tuned against any
holdout - this is a diagnostic labeling scheme, not a model.

Regimes (Phase 10's suggested list):
  clean_thermal_maloja               - thermal driver present, upper wind
                                        neutral (neither reinforcing nor
                                        opposing)
  thermal_supportive_southwest       - thermal driver present AND upper
                                        wind aligned with the Maloja (SW)
  strong_synoptic_southwest          - strong upper SW flow + favorable
                                        pressure gradient, dominant over
                                        any local thermal signal
  northerly_suppression              - upper wind misaligned with Maloja
                                        AND surface wind not from the SW
                                        either (a NW/N-ish flow pattern)
  easterly_suppression               - upper wind misaligned but surface
                                        wind still shows some SW character
                                        (treated as the Bernina/easterly
                                        case - a rough proxy, since this
                                        project has no real station data
                                        east of the target lake to
                                        distinguish it more precisely; see
                                        docs/STATION_RESEARCH.md's Bernina
                                        section)
  convective_storm_disruption        - high CAPE (storm risk)
  cloudy_rain_suppressed_thermal     - meaningful precipitation
  uncertain_mixed                    - none of the above clearly applies

Checked in priority order top-to-bottom (disruptive regimes first, since
e.g. a stormy day should be labeled by its storminess even if the thermal
signal also looks superficially present).
"""

REGIME_NAMES = (
    "convective_storm_disruption",
    "cloudy_rain_suppressed_thermal",
    "northerly_suppression",
    "easterly_suppression",
    "strong_synoptic_southwest",
    "thermal_supportive_southwest",
    "clean_thermal_maloja",
    "uncertain_mixed",
)


def classify_regime(features: dict) -> str:
    thermal = features.get("thermal_excess", 0.0)
    pressure = features.get("pressure_signal", 0.0)
    upper_align = features.get("upper_wind_alignment", 0.0)
    upper_speed = features.get("upper_wind_speed_score", 0.0)
    cape_penalty = features.get("cape_penalty", 0.0)
    precip_penalty = features.get("precip_penalty", 0.0)
    surface_align = features.get("surface_dir_alignment", 0.0)

    if cape_penalty < -0.5:
        return "convective_storm_disruption"
    if precip_penalty < -0.5:
        return "cloudy_rain_suppressed_thermal"

    if upper_align < -0.3 and upper_speed > 0.3:
        # Upper flow misaligned with the SW Maloja direction and strong
        # enough to matter - split by whether the surface wind still
        # shows any SW character (a rough easterly/Bernina proxy) or not
        # (treated as the northerly/NW case).
        return "easterly_suppression" if surface_align > 0 else "northerly_suppression"

    if upper_align > 0.5 and upper_speed > 0.5 and pressure > 0.3:
        return "strong_synoptic_southwest"

    if thermal > 0.2 and upper_align > 0.2:
        return "thermal_supportive_southwest"

    if thermal > 0.2:
        return "clean_thermal_maloja"

    return "uncertain_mixed"


def classify_samples(samples: list) -> list:
    """Returns a new list of regime-label strings, one per sample, in the
    same order - does not mutate the input samples."""
    return [classify_regime(s["features"]) for s in samples]
