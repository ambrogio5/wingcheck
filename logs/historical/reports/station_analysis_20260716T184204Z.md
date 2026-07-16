# Station analysis report (2026-07-16T18:42:04.318056+00:00)

Commit: `9ed8597273bc0a24deecd0bc2173063ff190275c`

## Family comparison - 2026 reference fold, full window ROC AUC

| family | ROC AUC |
|---|---|
| majority_class_baseline | 0.5 |
| forecast_wind_only | 0.7234 |
| wind_gust_direction | 0.7282 |
| full_current_model | 0.747 |
| full_plus_source_heating | 0.747 |
| full_plus_summit_support | 0.747 |
| full_plus_pressure_family | 0.7474 |
| full_plus_radiation_family | 0.747 |
| full_plus_competing_flow | 0.747 |
| full_plus_all_spatial_families | 0.7474 |

## Warnings
- 2026 is a repeatedly-inspected reference fold, not a pristine holdout - see CLAUDE.md.
- source_heating_score, summit_support_score, radiation_family_score, and competing_flow_score have ZERO real historical coverage (no confirmed source_region/pass/summit station yet) - their family comparisons structurally cannot show incremental value and must not be read as a negative physical finding.
- pressure_family_score is the only new family with genuine historical coverage (lug/sma are both confirmed, enabled stations).
- source_heating_score: missing for all 445 dates in this dataset.
- summit_support_score: missing for all 445 dates in this dataset.
- radiation_family_score: missing for all 445 dates in this dataset.
- competing_flow_score: missing for all 445 dates in this dataset.
