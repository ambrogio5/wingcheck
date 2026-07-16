# Maloja wind diagnostics

This document describes `station_features.py` (pre-forecast station
feature generation), `maloja_diagnostics.py` (the fixed diagnostic
families built on top of it), and `session_forecast.py` (the session-level
summary shown on the main dashboard's "Session outlook" section). None of
this changes how the production model scores an hour - it's a separate,
transparent explanation layer.

## Cutoff and reporting-delay discipline

`forecast_and_log.py` runs twice daily, 07:00 and 10:00 CEST. Station
diagnostics for a given run may only use observations that would genuinely
have been knowable by that run's issuance time - never a later
observation, and never an observation that exists on the clock but hasn't
actually been published by the station's provider yet.

`station_features.generate_station_features()` enforces this in two
layers:
1. **Cutoff**: only records with `timestamp_local` within
   `[day_start, cutoff]` are considered at all.
2. **Reporting delay**: each station's `config/stations.json` entry
   carries a `reporting_delay_minutes` - a record timestamped `T` is only
   "available" at `T + reporting_delay_minutes`. A 06:50 observation with
   a 20-minute delay isn't actually available until 07:10, and is
   therefore excluded from a 07:00-cutoff feature set.

Both layers are directly tested (`tests/test_station_features.py`'s
`CutoffTests`), including an explicit no-afternoon-leakage check.

`"since sunrise"` features use a fixed `SUNRISE_REFERENCE_HOUR` (06:00
local) rather than a real per-day astronomical calculation - a documented
simplification appropriate for this project's May-Oct season and
"lightweight" philosophy.

## Generic per-station features

`generate_station_features()` returns, per station, per cutoff:

```
latest_wind_speed, mean_morning_wind, max_morning_gust, wind_u, wind_v,
wind_speed_trend_1h, wind_speed_trend_3h,
temperature_latest, temperature_change_since_sunrise,
temperature_trend_1h, temperature_trend_3h,
dew_point_depression, relative_humidity,
pressure_latest, pressure_trend_3h,
precipitation_since_midnight, radiation_since_sunrise,
coverage, missing_indicator
```

Wind direction is never used as raw degrees in any downstream computation
- `wind_u`/`wind_v` are the meteorological-convention vector components
(`u = -speed*sin(dir)`, `v = -speed*cos(dir)`), and every pairwise
direction comparison works on these vectors, never on a raw degree
difference (which would be wrong across the 0/360 wrap).

Pairwise helpers (station A vs. station B): `temperature_difference()`,
`warming_rate_difference()`, `pressure_difference()`,
`pressure_tendency_difference()`, `wind_vector_difference()`,
`wind_vector_shear()`.

**None of these features are added to `features.FEATURE_NAMES`** - they
exist only for `maloja_diagnostics.py` and `station_analysis.py` to
consume. See `docs/STATION_RESEARCH.md`'s feature-promotion-prohibition
section.

## The seven diagnostic families (`maloja_diagnostics.py`)

Every diagnostic function returns exactly:

```json
{"score": 0.0, "status": "...", "raw_values": {}, "sources": [],
 "explanation_key": "...", "missing": false}
```

`explanation_key` is always drawn from a small, fixed vocabulary per
family (never free-form generated text) - `docs/index.html`'s
`EXPLANATION_TEXT` map turns each key into a short human sentence for the
"Why it may work" / "What could prevent it" lists.

| Family | Based on | Status vocabulary |
|---|---|---|
| **Source heating** | Source-region vs. target-region temperature and warming rate | favourable / neutral / unfavourable / missing |
| **Pass activation** | Pass-aligned wind speed and direction | favourable / neutral / unfavourable / missing |
| **Summit support** | Summit wind speed and direction, nonlinear (moderate reinforces, excessive may override) | weak / supportive / excessive / opposing / missing |
| **Radiation support** | Morning radiation and recent precipitation | favourable / neutral / unfavourable / missing |
| **Pressure support** | Real Lugano-Zürich pressure gradient, kept separate from the forecast-based `pressure_signal` | favourable / neutral / unfavourable / missing |
| **Competing flow** | Easterly/northerly flow, or a poorly-aligned summit/surface wind pair | clear / easterly / northerly / misaligned_shear / missing |
| **Data health** | Station coverage, missing-input count | healthy / degraded / critical |

**Honesty on missing data**: source heating, pass activation, summit
support, and radiation support all depend on station roles
(`source_region`, `pass`, `summit`) with **zero confirmed stations** today
(see `docs/STATION_RESEARCH.md`) - in production, they currently always
report `missing: true` with an honest `*_missing_station_data`
explanation key. Every function's actual scoring logic is fully
implemented and tested via fixtures (`tests/test_maloja_diagnostics.py`)
so the moment a real station is confirmed, these start producing real
output with no code change. **Pressure support is the one family with
real data today** (`lug`/`sma` are both confirmed, enabled stations) and
**competing flow** is missing in the current wiring because no
confirmed station reports wind direction yet either.

`pressure_support()` deliberately takes the observed gradient and the
forecast `pressure_signal` as two separate arguments - the forecast value
is reported in `raw_values` for side-by-side comparison but never affects
the score, per this family's explicit "keep forecast and observed signals
separate" requirement (tested in `test_forecast_signal_reported_but_never_affects_score`).

## Session-level summary (`session_forecast.py`)

Given one calendar day's already-scored hourly forecasts, derives:

```
likely_onset_start, likely_onset_end, best_window_start, best_window_end,
peak_hour, expected_wind_min_kt, expected_wind_max_kt,
expected_gust_min_kt, expected_gust_max_kt, expected_rideable_hours,
likely_decline_time, event_probability, timing_confidence,
strength_confidence, model_agreement
```

"Rideable" means `tier != "UNLIKELY"` - the existing GOOD/MARGINAL alert
tiers already calibrated by `backtest.py`, not a new threshold.
`event_probability` is the MAX hourly probability across the day, matching
the already-validated production convention that the day's session
probability is best represented by its peak hour, not an average.

**Every timestamp in the output is one of the input hourly `target_time`
values verbatim** - this module never interpolates or implies sub-hour
precision the underlying hourly forecast doesn't have.

### Confidence rules

Both `timing_confidence` and `strength_confidence` start at 1.0 and
subtract a fixed penalty (0.30 each) for triggered factors, then map the
result to `"high"` (≥0.75) / `"medium"` (≥0.45) / `"low"` via fixed
cutoffs - a deterministic, inspectable scoring formula, never a hidden
model:

- **High model spread** (`model_agreement < 0.5`, from
  `ensemble_agreement_score`) - reduces both.
- **Flat/inconclusive hourly probability curve** (population standard
  deviation of the day's hourly probabilities below 0.05) - reduces
  timing confidence only (can't pinpoint a peak hour if the whole day
  looks the same).
- **Missing station input** (any station's `missing_indicator == 1.0`) -
  reduces both.
- **Conflicting diagnostic families** (at least one family status in the
  favourable set and at least one in the unfavourable set, among non-
  missing diagnostics) - reduces both.
- **Stale data** (oldest station input older than 90 minutes) - reduces
  strength confidence only (the reported kt range itself might be out of
  date).

## Dashboard integration

`refresh_dashboard.py`'s `optional_issuance_fields()` builds
`daily_diagnostics`, `session_forecast`, `station_health`,
`model_agreement`, and `data_provenance` from the latest
`logs/forecast_issuances.jsonl` record - and degrades to `{}` for every
field when no issuance log exists yet (a fresh checkout, or a repo
predating this feature). `docs/index.html`'s "Session outlook" section
(hidden entirely when there's no data for today) renders these fields;
`docs/research.html` never shows per-day diagnostics, only the
aggregate research comparisons - see `docs/DATA_ARCHITECTURE.md`.
