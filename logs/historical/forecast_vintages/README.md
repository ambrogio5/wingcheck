# `forecast_vintages/`

See `forecast_vintages.py` and `docs/DATA_ARCHITECTURE.md`. Stores every
forecast payload exactly as it was available at issue time, so a future
retrain can eventually train on genuine multi-day-lead forecasts instead
of Open-Meteo's 0-hour historical archive (see CLAUDE.md's long-standing
"Known limitation" note on this gap).

Structure:

    YYYY/MM/DD/<issue-time>_<provider>_<model>.json.gz   - one compressed
                                                            payload per
                                                            forecast run
    index.jsonl                                          - append-only,
                                                            normalized index

Unlike `../station_hourly/`, these payloads are **not** re-derivable once
the forecast window has passed - Open-Meteo's live API only serves ~3
months of history and even its historical-archive endpoint returns
0-hour data, never a genuine multi-day-lead forecast. So, unlike the
station archive, vintages ARE committed to git going forward, subject to
the retention policy in `docs/DATA_ARCHITECTURE.md`.
