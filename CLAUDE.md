# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Malojawind: a self-improving forecast for the Maloja wind at Lake Silvaplana.
A logistic-regression-style model scores 22 engineered features (from 20+ raw
data points, including a multi-model wind ensemble) to predict whether a
given afternoon hour (12:00-18:00) will be a good wingfoil session, sends
Telegram alerts, verifies its own predictions against the real kitesailing.ch
Silvaplana lake reading (real MeteoSwiss Samedan station data kept as
fallback + a secondary feature), and retrains its weights automatically via
scheduled GitHub Actions. There is no server, database, or app framework —
it's a handful of plain scripts orchestrated entirely by cron-scheduled
GitHub Actions jobs that commit their own output back to the repo.

**Accuracy ceiling**: a 2026 holdout analysis found the 2026-07 model barely
beat a trivial baseline (67.9% vs 53.6% accuracy, AUC 0.75) — 15 of the then
16 features added almost nothing over the raw forecast wind alone (AUC
0.743). Root cause: the forecast-to-groundtruth correlation was only ~0.52
(Samedan is 10km from the target lake) and ~31% of hours land within ±2kt of
the labeling threshold, an inherent noise floor no amount of feature/model
tuning removes. The larger lever — swapping Samedan for the real lake
station — is now done for the *live* loop (`kitesailing_weather.py`, scraped
since no API exists, see its docstring), but **not** for `backtest.py`'s
historical retrain, since that station has no historical archive. This is a
real, currently-open labeling-criterion mismatch between the
historically-trained weights and the live online updates — see the
"Conventions" section below and `backtest.py`'s docstring.

**2026-07-16 leakage fix, and what it changed (and didn't)**: that original
67.9%/53.6% figure came from a `backtest.py` that loaded the already-trained
`weights.json` and reset only the bias before "evaluating" on 2026 — since
the per-feature weights had already been shaped by a previous retrain that
included 2026, the holdout wasn't actually untouched. `backtest.py` now
builds two separate models from `model.new_weights()` (never from
`weights.json`): an EVALUATION model trained only on 2024+2025 and scored
once against 2026, then discarded, and a DEPLOYMENT model trained on all
three years and saved to `weights.json` (see `model.py`'s and
`backtest.py`'s docstrings, and the "Conventions" bullet below). Re-run with
the fix, the honest 2026 hourly holdout came out at 69.4% vs a 53.5%
majority-class baseline, AUC 0.747 — CONFIRMING the original headline
figure rather than invalidating it (the leak didn't move the number much in
this case, though there was no way to know that without fixing it). The
feature ablation (`ablation.py`) still shows most of the discriminative
power coming from forecast wind alone (AUC ~0.72); `pressure_nowcast_score`
and `samedan_morning_score` each move full-model AUC by <0.001 either way —
new features, not yet proven, exactly as expected this early. See
`docs/dashboard_data.json`'s `evaluation`/`deployment` sections and the
dashboard's "Holdout evaluation"/"Feature ablation" tables for the current
run's exact numbers - don't hardcode these figures elsewhere, they update
every time `backtest.py` runs.

**A tried-and-reverted change, for the record**: the window was briefly
narrowed to 15:00-18:00 on the theory that hours 12-14 were just noise. The
2026-07-16 backtest disproved that empirically — full-model AUC on the 2026
holdout dropped from 0.750 (12-18h) to 0.683 (15-18h), because hours 12-14
have a low positive rate (~25-49%) and were easy, highly-separable true
negatives that boosted overall discriminative power; restricting to 15-18h
left a smaller, more homogeneous, harder-to-classify population. Reverted
back to 12:00-18:00. Don't re-narrow the window without backtest evidence
it actually helps — "seems noisier" is not evidence, AUC before/after is.

## Commands

```bash
pip install -r requirements.txt   # only dependency: requests

python forecast_and_log.py        # fetch forecast, score, send Telegram alert, log predictions
python kitesailing_weather.py     # scrape one live Silvaplana reading, append to logs/ (needs playwright, see below)
python verify_and_learn.py        # check past predictions against real observations, update weights.json
python refresh_dashboard.py       # rebuild docs/dashboard_data.json from logs on disk (no network)
python backtest.py                # full historical retrain (2024-2026), rewrites weights.json + dashboard data
python -m unittest discover -s tests   # offline test suite (stdlib only, no network calls)

# Historical archive / station research (see "Historical data archive and
# station research" below and docs/DATA_ARCHITECTURE.md / docs/STATION_RESEARCH.md)
python historical_data.py sync              # incrementally refresh the station archive (idempotent, no-op if nothing new)
python historical_data.py list-stations     # list every registered station + verification status
python historical_data.py coverage          # per-station record counts / date ranges
python historical_data.py validate          # data-quality + gap/staleness checks (non-blocking diagnostic)
python historical_data.py export-training   # export a combined normalized dataset for research scripts
python station_analysis.py                  # correlation screen + rolling-origin station-family comparison
python calibration_analysis.py              # Platt/isotonic calibration comparison per rolling fold
python regime_analysis.py                   # weather-regime false-positive breakdown
python continuous_target_analysis.py        # continuous wind-target + daily-session-target research
python refresh_research_dashboard.py        # rebuild docs/research/research_data.json from the latest reports (no network)
```

There is no linter or build step configured in this repo; there is an
offline `unittest` suite under `tests/` (stdlib only, no network calls) —
run it before trusting a change to `model.py`/`metrics.py`/`ablation.py`/
`backtest.py`. All of the historical-archive/research commands above are
ALSO research-only and network-optional in the sense that matters most:
none of them ever writes `weights.json` or `docs/dashboard_data.json` (the
main operational dashboard) — enforced by `tests/test_station_analysis.py`,
`tests/test_regime_analysis.py`, etc. asserting `weights.json`'s mtime is
unchanged after calling into them. `historical_data.py sync` does attempt
real network calls (best-effort, catches all exceptions) but falls back to
ingesting whatever's already in `logs/raw_cache/` for the three confirmed
stations, so it's always safe to run even fully offline. `forecast_and_log.py` needs
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` env vars to actually send (falls
back to printing the message if unset). `backtest.py` and
`verify_and_learn.py` make live network calls to Open-Meteo and MeteoSwiss,
so running them locally mutates `weights.json` and the `logs/` files for
real — treat that as a real training step, not a dry run.
`kitesailing_weather.py` is the one script with a dependency beyond
`requests`: it needs a real browser (`pip install playwright && playwright
install --with-deps chromium`) since there's no API to call — kept out of
`requirements.txt` deliberately so the rest of the pipeline stays
lightweight. Its `load_observations()`/`closest_observation()` helpers
(used by `verify_and_learn.py`) are pure-stdlib and importable without
playwright installed — only `fetch_current_reading()` needs the browser,
and its `playwright` import is deliberately deferred inside that function
for exactly this reason. Don't hoist it back to module level.

## Architecture

**Data flow pipeline**, in the order jobs run:

1. **`features.py`** — pulls raw values from Open-Meteo (forecast or
   historical-archive API) across three locations (Silvaplana target spot,
   Bregaglia source valley, Maloja Pass upper air) plus Lugano/Zürich
   pressure, a multi-model wind ensemble (`icon_seamless`, `gfs_seamless`,
   `ecmwf_ifs025` via the `models=` param), and real MeteoSwiss station
   observations (via `meteoswiss.py`: Samedan wind, plus Lugano/Zürich
   pressure), and engineers them into 22 named features (`engineer_features`)
   — including an ensemble mean/agreement pair, a lag-based persistence
   feature, two interaction terms, `samedan_morning_score` (Samedan's real
   measured wind around 07:00 local the same day — a genuine upstream
   nowcast, now that Samedan is no longer the ground truth, just a
   correlated secondary signal), and `pressure_nowcast_score` (the same
   Lugano-Zürich gradient as `pressure_signal`, but from real station
   observations this morning rather than the forecast model — see
   `meteoswiss.py`'s docstring for why `pressure_signal` itself has to stay
   forecast-based: it scores a 1-3 day-ahead target hour a real observation
   can't exist for yet, unlike the ground-truth role Samedan could hand off
   to kitesailing.ch). Two fetch paths: `fetch_raw` (live, ~3 day forecast)
   and `fetch_raw_historical` (date-range archive, used only by
   `backtest.py`) — they must stay feature-compatible since the same
   `engineer_features` function scores both. `fetch_raw` fetches its own
   Samedan/Lugano/Zürich "recent" data for the nowcast features;
   `fetch_raw_historical` does NOT (`backtest.py` already fetches the full
   multi-year archives once for labeling and injects them into `raw` itself,
   rather than re-fetching per season). All three nowcast fetches (ensemble,
   Samedan, pressure) are best-effort: on failure `engineer_features` falls
   back to a neutral value (0.5 agreement, 0.0 nowcast score) so a flaky
   extra API call can't take down the whole pipeline. `raw_snapshot(raw,
   idx)` is a companion function that returns every raw physical value,
   unnormalized - logged into `logs/predictions.jsonl` alongside the
   engineered features, since Open-Meteo's live API only serves ~3 months of
   history and even `backtest.py`'s archive fetch doesn't reproduce a
   genuine multi-day-lead forecast (see its own docstring). Once a live
   prediction ages out, this is the only remaining record of what the
   forecast actually said - without it, that data would be permanently
   unrecoverable for future feature engineering.
2. **`model.py`** — a from-scratch logistic unit: `score()` (sigmoid of
   `bias + Σ weight_i * feature_i`) and `update()` (one online
   gradient-descent step given an actual 0/1 outcome). No ML framework;
   weights live in `weights.json` (bias, per-feature weights, tier
   thresholds, `trained_samples` counter used to decay the learning rate).
3. **`kitesailing_weather.py`** — the PRIMARY ground truth. Scrapes the real
   Silvaplana lake reading from a "LiveMeteo" widget embedded in
   kitesailing.ch (there's no API — confirmed by inspecting the page's
   network traffic and DOM; the widget is rendered fully server-side, and
   even the vendor name found in its CSS classes doesn't resolve as a
   domain). Needs a real browser (Playwright + Chromium) — see the
   "Commands" section above for why that import is deferred inside
   `fetch_current_reading()`. Scraped on a schedule into
   `logs/kitesailing_observations.jsonl`; `load_observations()` +
   `closest_observation()` let `verify_and_learn.py` match a scraped reading
   to a target hour within a tolerance window (readings only exist for
   whenever the scrape job happened to run, not every hour on the dot).
   Has **no historical archive** — history only exists from whenever
   scraping started.
4. **`meteoswiss.py`** — SECONDARY ground truth (fallback when
   `kitesailing_weather.py` has no reading close enough to a target hour)
   and, via `features.py`, two live nowcast features. Pulls real station
   observations from MeteoSwiss's open STAC catalog (not another model
   run): Samedan (SAM) wind, and Lugano (`LUGANO_STATION`)/Zürich-Fluntern
   (`ZURICH_STATION`) sea-level pressure. Station codes and column names
   (`fu3010h0`/`fu3010h1` for wind, `pp0qffh0` for pressure) were confirmed
   against the live API on 2026-07-16 — a prior version of this docstring
   guessed wrong values (`fu3010z0`/`z1`, `pp0qffs0`) that were never
   actually used/tested; don't reintroduce guessed column names, verify
   against a real fetch. `SAM_PROXY_KT` (default 8.0) is the calibration
   knob converting "wind at Samedan" into "rideable at the lake" for the
   fallback path only — tune it after real sessions, then re-run the
   backtest so labels regenerate consistently. Samedan is also still the
   ONLY source with a multi-year historical archive, so `backtest.py` has
   to keep using it as the sole (not just fallback) ground truth for
   historical retraining.
5. **`forecast_and_log.py`** (scheduled 07:00 & 10:00 CEST) — runs
   `features.py` + `model.py` over the live forecast, tiers each hour in the
   12:00-18:00 window into GOOD/MARGINAL/UNLIKELY via thresholds in
   `weights.json`, sends a Telegram message, appends every scored hour to
   `logs/predictions.jsonl` with `verified: false`, including the raw
   `model_wind_dir_deg` (0-360 compass degrees, unconverted) alongside
   `model_wind_kt`/`model_gust_kt` - `refresh_dashboard.py`'s
   `compass_direction()` turns it into a display label (e.g. "SW") for the
   dashboard, since nothing else needs the raw degrees. `WINDOW_START_HOUR` /
   `WINDOW_END_HOUR` here must match `backtest.py`'s — the model is trained
   on exactly the hours it's later scored on in production.
6. **`sample_kitesailing`** job (scheduled every 15 min, 05:00-21:45 CEST -
   sunrise to sundown across the full May-Oct season, well beyond the
   12:00-18:00 scored window, defined directly in the workflow file, no
   dedicated `.py` entry point beyond `kitesailing_weather.py`'s own
   `main()`) — the source of ground-truth data for step 7 below, and a
   full-day record for future analysis (e.g. morning thermal onset, evening
   drop-off) that nothing currently consumes but is cheap to keep now and
   impossible to recover later.
7. **`verify_and_learn.py`** (scheduled 20:00 CEST) — reads unverified
   predictions at least `MIN_AGE_HOURS` (20h) old. For each, looks for a
   `kitesailing_weather.py` reading within `OBSERVATION_TOLERANCE_MINUTES`
   (30) of the target hour; if found, that's the label
   (`SILVAPLANA_MARGINAL_KT`, no proxy correction — it's the real spot). If
   not, falls back to Samedan (`SAM_PROXY_KT`). Either way, Samedan's own
   reading for that hour (when available) is logged alongside as
   `samedan_wind_kt`/`samedan_gust_kt` regardless of which source produced
   the actual label — the basis for a future correlation study once enough
   overlapping data exists. `ground_truth_source` on each record says which
   path was used. Calls `model.update()` — but only once per target hour
   (the *latest* of possibly several predictions for that hour, since the
   same hour gets forecast repeatedly across the 3-day rolling window) to
   avoid overweighting duplicate samples.
8. **`refresh_dashboard.py`** (scheduled after verify_and_learn) — rebuilds
   `docs/dashboard_data.json` purely from local files (no network): merges
   `logs/backtest_dataset.jsonl` (historical) with verified entries from
   `logs/predictions.jsonl` (live), re-scoring everything with the *current*
   weights so the dashboard's probability trace always reflects today's
   model. The frozen `evaluation`, `deployment`, and `reproducibility`
   sections from the original backtest run are carried over unchanged, not
   recomputed (recomputing `evaluation` would let 2026 holdout data leak
   into "training" through the continuously-learning deployed weights) —
   only `backtest.py` may write those three top-level keys (including
   `evaluation.generated_at`, which is what lets the dashboard show a
   staleness flag on the frozen evaluation independent of the top-level
   `generated_at`, which changes on every refresh). Also builds
   `upcoming_forecast` — the latest logged prediction per *future* target
   hour (`upcoming_forecast()`, deduped the same way as training, each row
   including the raw `probability`, `tier`, wind/gust, and
   `model_wind_dir` compass label) — this is what `docs/index.html` renders
   as "Today's forecast"; it's the only part of the dashboard that answers
   the page's own headline question rather than reporting on past
   performance.
9. **`backtest.py`** (manual only, `workflow_dispatch`) — the only way to
   retrain from scratch. Builds a labeled dataset for May–Oct 2024/2025/2026
   from Open-Meteo's historical archive + real SAM obs (still the only
   ground truth available historically — see `meteoswiss.py` above). Trains
   TWO separate models, both from `model.new_weights()` (never by loading
   `weights.json` and partially resetting it — see the "2026-07-16 leakage
   fix" note above and the Conventions bullet below): an EVALUATION model
   trained only on 2024+2025, with thresholds calibrated only on 2024+2025,
   scored once against the untouched 2026 holdout and then discarded; and a
   DEPLOYMENT model trained on 2024+2025+2026, with thresholds calibrated on
   all three years, which is the only one saved to `weights.json`. Reports,
   via `metrics.py`, hourly and session-level (`metrics.build_session_samples`)
   accuracy/balanced-accuracy/precision/recall/ROC-AUC/PR-AUC/Brier for both
   the full window (12:00–18:00) and a 14:00–18:00 "prime window" diagnostic
   slice (same model, same training window — NOT a second training run;
   changed from 15:00–18:00 to 14:00–18:00 on 2026-07-16, a separate,
   later change from the WINDOW_START_HOUR revert described above),
   plus operational MARGINAL/GOOD threshold performance and a feature
   ablation (`ablation.py`, diagnostic only, see its docstring). Training is
   reproducible: `model.train_epochs()` shuffles with a locally-scoped
   `random.Random(model.DEFAULT_TRAIN_SEED)`, so identical cached raw data
   reproduces byte-identical weights; the seed/epoch count are recorded in
   `docs/dashboard_data.json`'s `reproducibility` section. Goes through
   `historical_cache.py` rather than calling `features.fetch_raw_historical`/
   `meteoswiss.fetch_sam_hourly_observations` directly — see below.
10. **`historical_cache.py`** — persists backtest.py's raw fetches under
    `logs/raw_cache/` (committed by the `backtest` job, like everything
    else) so a from-scratch retrain doesn't repeat the ~14-minute network
    pull every time. Caches the FULL day (all 24 hours) per season
    regardless of the current window, since `fetch_raw_historical` already
    returns unfiltered data and window filtering happens downstream in
    `backtest.py` - so a future window change (like the revert above) is
    served entirely from cache too, no re-fetch needed. Closed seasons
    (any date range not ending today) are cached forever; the open season
    is refetched at most once per calendar day. Each MeteoSwiss station
    archive's (Samedan, Lugano, Zürich) expensive part - STAC catalog
    discovery + downloading every historical CSV - runs once ever per
    station; later calls just merge in the cheap "recent" file
    (`get_samedan_archive()` / `get_pressure_archive(station)`).

**Orchestration**: `.github/workflows/wingcheck.yml` defines five jobs
(`validate`, `forecast`, `sample_kitesailing`, `learn`, `backtest`) gated by
cron schedule, `pull_request`, or `workflow_dispatch` input — see the
comment header in that file for exact triggers. `validate` runs on every
`pull_request`: syntax-checks every module (`python -m py_compile`) and
runs the offline test suite - nothing else. It never runs forecasts,
scraping, learning, backtests, Telegram calls, or commits, and is never
exposed to the Telegram secrets, so it's safe on any PR. `forecast` now
runs `refresh_dashboard.py` immediately after `forecast_and_log.py` and
commits `docs/dashboard_data.json` alongside `logs/predictions.jsonl` -
fixed 2026-07-16, since the dashboard previously only refreshed at the
20:00 CEST `learn` run, leaving that morning's/midday's forecast invisible
on the published page for hours after it was actually logged. Each of the
other four jobs commits its own output (`weights.json`, `logs/*.jsonl`,
`docs/dashboard_data.json`) straight back to `main` with a bot identity;
there's no PR review step for these automated commits, but `forecast`,
`learn`, and `backtest` all run the same offline test suite immediately
before doing anything real, so a regression fails the job instead of
silently committing bad output. Every job's `if:` explicitly names the
`event_name`/`schedule` combination it requires, so a `pull_request` event
can never accidentally satisfy an operational job's condition.
`sample_kitesailing` is the one job with a dependency beyond `requests`
(Playwright + Chromium) and caches the browser binary (`actions/cache`,
keyed on a pinned Playwright version) since it runs every 15 minutes.
`COPY-ME_workflow.yml` is a duplicate of the same file, meant to be copied
into `.github/workflows/` when bootstrapping a new deployment from this
template — keep the two in sync if you edit the workflow (`tests/test_workflow.py`
asserts they're byte-identical).

**`docs/`** is a static dashboard (GitHub Pages, served from `/docs` on
`main`) that fetches `dashboard_data.json` client-side via a cache-busting
`fetch('./dashboard_data.json?v=' + Date.now(), {cache: 'no-store'})` -
needed because the forecast job now regenerates that file up to twice a
day (see "Orchestration" above), so a browser- or CDN-cached copy serving
a stale forecast is a real, regular risk, not a hypothetical one. It has
no build step: `index.html` (inline CSS/JS, Chart.js from a CDN used only
for the two historical charts inside the collapsed "Technical details"
section - every other section renders from plain template strings and
degrades gracefully if the CDN is blocked) plus one small plain-JS file,
`dashboard-logic.js` (loaded via a plain `<script src>` tag before the
main inline script), which holds the Europe/Zurich date-handling helpers
and `getForecastByDay()` - split out specifically so it's directly
testable with Node (`tests/test_dashboard_logic.py`) without needing a
browser or a bundler. Sections, top to bottom: "Today & tomorrow" (two
independent day-blocks, each with one card per hour 14:00-18:00, from
`upcoming_forecast`, showing the model's raw probability as a percentage
labeled "est. likelihood" - never a tier threshold relabeled as a
percentage - plus wind/gust/`model_wind_dir`, tier badge, and the best
hour highlighted *per day*; "Today" shows only hours strictly after the
current Zurich wall-clock time, with a "window is complete" message once
none remain, while "Tomorrow" always shows its full set independently -
fixed 2026-07-16, replacing a version that picked "the earliest date with
any data" as today and could hide tomorrow entirely whenever today still
had one hour left), "Daily summary" (best hour/max likelihood/peak
wind/recommendation, shown separately for today and tomorrow), "Live
performance" (from `live_metrics`, with a provisional-sample note below
n=30), "Frozen holdout evaluation" (from `evaluation`, full window and the
14:00-18:00 diagnostic prime window side by side, `evaluation.generated_at`
shown with a staleness flag past 30 days, and a "Run fresh evaluation"
button linking to
`https://github.com/<owner>/<repo>/actions/workflows/wingcheck.yml` in a
new tab - deliberately just a link, since a static page cannot safely call
the GitHub API without embedding a credential in frontend code), "Feature
ablation", and a collapsed "Technical details" section (operational
threshold confusion matrices, reproducibility seed, the weights list, and
the monthly/timeline charts).

## Historical data archive and station research (2026-07-16 addition)

A second, parallel line of work sits alongside the operational pipeline
above: a durable historical-data archive and a set of research scripts
that evaluate whether additional local weather stations, alternative
models, or calibration methods would improve the forecast — all
deliberately isolated from the production model until a finding is
explicitly promoted. Full detail is in **`docs/DATA_ARCHITECTURE.md`**
and **`docs/STATION_RESEARCH.md`**; this section is the short version for
anyone editing these modules.

11. **`stations.py`** — the station metadata registry (id, coordinates,
    provider, variables, roles, and an honest `verification` field:
    `"confirmed"` / `"candidate_unconfirmed"` / `"needs_discovery"`).
    Exactly three stations are `confirmed` (`sam`, `lug`, `sma` — the same
    three already used in production) because a 2026-07-16 research
    session ran in a sandbox where outbound network access to
    `data.geo.admin.ch` (and, it turned out, effectively every other
    external domain via `WebFetch`) was blocked at the gateway level. Over
    20 other candidate stations around Silvaplana/Maloja/Upper Engadin/
    Bregaglia are recorded with a plausible-but-unverified station code
    (`candidate_unconfirmed`) or no code at all (`needs_discovery`) — see
    `docs/STATION_RESEARCH.md` for every one, including rejected candidates
    (e.g. ski-resort webcam/weather widgets — rejected as data sources
    since they're presentation layers with no documented API, per the
    explicit rule not to assume a station exists just because a webcam
    displays weather). **Never report a station-derived research result as
    validated unless its `verification` is `"confirmed"`.**
12. **`historical_data.py`** — the durable archive's CLI and core logic
    (`sync` / `list-stations` / `coverage` / `validate` / `export-training`).
    Normalizes every station's hourly data to one canonical schema
    (`NORMALIZED_FIELDS`, 23 fields, explicit UTC + Europe/Zurich
    timestamps, nulls not invented values) regardless of source station or
    provider, and persists it under `logs/historical/`. `sync` is
    idempotent and never overwrites a richer existing record with a
    sparser one (`merge_normalized_records()`); a from-scratch rebuild of
    the derived JSONL is always possible from the small, git-committed
    `logs/raw_cache/` + `logs/historical/manifests/` alone — see
    `docs/DATA_ARCHITECTURE.md`'s "Disaster recovery" section.
    **`logs/historical/station_hourly/*.jsonl` and `logs/historical/datasets/*`
    are gitignored, deliberately** — the normalized schema is ~10x more
    verbose per row than `raw_cache/`'s compact format (a full sync+export
    of just the 3 confirmed stations produced ~1.1GB before this was
    caught and fixed), and since it's fully regenerable in seconds via
    `historical_data.py sync`, permanently committing it would be pure
    bloat. Only the manifests (tens of KB) are committed.
13. **`forecast_vintages.py`** — archives the raw, genuine multi-model
    forecast payload (issue time, target times, lead-time-in-hours,
    checksum) *before* `forecast_and_log.py` scores it, via a best-effort
    `archive_forecast_payload_safe()` call that can never break the actual
    forecast/Telegram send on failure. Deduped by content checksum,
    gzip-compressed, stored under `logs/historical/forecast_vintages/
    YYYY/MM/DD/` and **committed permanently** (unlike the station
    archive above) — a forecast vintage can never be recreated once its
    lead time has passed, since Open-Meteo's live API only retains ~3
    months and `backtest.py`'s historical-archive fetch is 0-hour data,
    not a genuine forecast (see `features.py`'s own docstring on this
    distinction). `FORECAST_PAYLOAD_KEYS` deliberately excludes the
    station-nowcast fields (`samedan_obs`/etc.) since those are refetched
    fresh each call and would defeat checksum dedup for what is really the
    same underlying forecast-model response.
14. **`research_metrics.py`** — statistical building blocks shared by every
    research script: Pearson/Spearman/point-biserial correlation,
    day-level (not row-level) bootstrap confidence intervals
    (`bootstrap_ci_by_day`/`_multi`), Benjamini-Hochberg FDR correction for
    multiple comparisons, and **`rolling_origin_splits()`** — chronological,
    day-grouped expanding-window folds (train on 2024, validate each
    subsequent month/season in turn; 2026 is reported separately, labeled
    `kind="reference"` not `"holdout"`, since repeated station-family
    comparisons against it mean it's no longer a single-use evaluation for
    that purpose — see the "Conventions" bullet below on 2026 holdout
    reuse). Every fold is checked (and tested,
    `tests/test_research_metrics.py`) to never put the same calendar day in
    both its train and validate sets.
15. **`station_analysis.py`** — the main station/feature-family research
    driver: a correlation screen (per-feature Pearson/Spearman/point-biserial/
    ROC-AUC/PR-AUC with bootstrap CIs and FDR correction), a rolling-origin
    comparison across feature-group baselines (majority-class, forecast-wind-
    only, wind+gust+direction, the full current production model, and the
    full model with each new nowcast feature added/removed), and a station
    coverage report that categorizes every registered station honestly
    (`available_for_analysis` / `insufficient_coverage` / `unavailable_historically`
    / `candidate_partial_data` — a *confirmed* station with zero records is
    reported as `insufficient_coverage`, never fabricated data). **Never
    writes `weights.json`** — every model it trains goes through
    `model.new_weights()` and is discarded after scoring, verified by
    `tests/test_station_analysis.py` asserting `weights.json`'s mtime is
    unchanged. Real finding from this session: across all 6 rolling folds
    (5 rolling + the 2026 reference), removing either
    `samedan_morning_score` or `pressure_nowcast_score` shows no consistent
    AUC degradation — both are now flagged `validated_unstable` in
    `feature_candidates.py` (see below), a materially stronger signal than
    the single-holdout ablation check alone could show.
16. **`model_regularized.py`** — a RESEARCH-ONLY L2-regularized logistic
    regression from scratch (pure stdlib, no scikit-learn), with
    standardization fit only on training-fold data (never leaking
    validation/holdout statistics) and missing-value indicator dummies.
    Completely isolated from `model.py`; the production model stays
    unregularized. Real finding: mixed results vs. production across
    folds (sometimes notably better, e.g. 2026 reference AUC 0.785 vs
    0.747; sometimes worse) — reported as "not clearly beneficial, worth
    further tuning," not a win, since inconsistent direction across folds
    fails the promotion bar in `feature_candidates.PROMOTION_PROCESS`.
17. **`calibration.py`** / **`calibration_analysis.py`** — reliability
    tables (10 bins), Expected/Maximum Calibration Error, log loss, Platt
    scaling, and isotonic regression (pool-adjacent-violators), all from
    scratch. Calibration is fit only on each rolling fold's training split
    and evaluated on that fold's validation split — never on the final
    2026 reference. Beta calibration is explicitly NOT implemented
    (documented limitation, not silently skipped). Real finding: ECE
    improved in 4 of 6 folds (2026 reference: 0.103 → 0.071 Platt / 0.073
    isotonic), slightly worse in the 2 smallest/earliest folds — plausibly
    a sample-size artifact, reported honestly either way.
18. **`regimes.py`** / **`regime_analysis.py`** — a transparent, priority-
    ordered rule-based classifier over 8 weather regimes (clean thermal
    Maloja, thermal-supportive SW, strong synoptic SW, northerly/
    easterly suppression, convective/storm disruption, cloudy/rain-
    suppressed thermal, uncertain/mixed) built entirely from existing
    engineered features — no new station data required. Used to break
    down false-positive rate by regime, out-of-sample per rolling fold.
    Real finding: `northerly_suppression` (26.5% of 740 false-positive-
    eligible hours) and `easterly_suppression` (20.7% of 227) are the two
    regimes driving most GOOD/MARGINAL false alerts in the 14:00-18:00
    window — a concrete, physically-interpretable target for future
    feature work, not yet acted on.
19. **`continuous_target_analysis.py`** — research-only alternatives to
    the binary rideable/not-rideable target: a continuous wind-speed
    regression (MAE ~2.9-3.5kt, rank correlation 0.42-0.61 across folds)
    and a daily-session target (any rideable hour, count, best hour,
    session duration). Real finding: the existing production approach —
    take the MAX hourly probability across the window as the day's
    session probability — matches or beats a dedicated daily logistic
    model in every comparable fold, validating rather than replacing the
    current design.
20. **`feature_candidates.py`** — the explicit candidate-feature registry.
    `features.FEATURE_NAMES` remains the only source of truth for what's
    actually deployed; nothing here auto-populates it. Each candidate
    records its physical rationale, source station, research status
    (`proposed` / `under_research` / `validated_unstable` /
    `validated_stable` / `rejected`), fold results, and
    `approved_for_production`. `PROMOTION_PROCESS` is the explicit 9-step
    checklist (coverage validation → rolling-origin validation →
    calibration check → operational-reliability check → manual approval →
    schema version bump → deployment retrain → post-deployment monitoring)
    that must be satisfied, in order, before a human edits `features.py`
    and re-runs `backtest.py`. `samedan_morning_score` and
    `pressure_nowcast_score` are flagged here as `validated_unstable` even
    though they're already in production (schema v3) — a flag for
    possible future removal, not a recommendation to add anything new.
21. **`data_quality.py`** — implausible-value, negative-speed, gust-less-
    than-speed, future-timestamp, duplicate, and gap/staleness checks
    (`STALE_ARCHIVE_DAYS = 30`) over the historical archive, wired into
    `historical_data.py validate`. **Flags, never silently discards** —
    every finding is preserved as a `quality_flags` entry on the record. A
    real validation run found genuine gaps in the actual archives: 2 in
    Lugano's pressure history, 8 in Zürich's.
22. **`refresh_research_dashboard.py`** / **`docs/research.html`** — a
    second, completely separate dashboard (`docs/research/research_data.json`,
    no network calls, never touches the main `docs/dashboard_data.json`)
    showing station coverage, the rolling-origin family comparison,
    correlation screen, calibration results, regime false-positive
    breakdown, and daily-session comparison — explicitly labeled
    "EXPLORATORY RESEARCH — NOT THE OPERATIONAL DASHBOARD" so a viewer
    never mistakes a research finding for a production guarantee.

**Workflow jobs for the above** (`.github/workflows/wingcheck.yml`, kept
byte-identical to `COPY-ME_workflow.yml`): `sync_historical_data` (daily,
03:30 CEST, plus manual — runs `historical_data.py sync` and commits only
`logs/historical/manifests/`), `station_research` (manual only — runs the
full research-script suite, commits reports + research dashboard data,
**never** `weights.json`), and `promote_features` (manual only, and
deliberately a no-op beyond printing `feature_candidates.PROMOTION_PROCESS`
— actually promoting a feature is a source-code change a human makes
directly, followed by the existing `run_backtest` option). A
workflow-level `concurrency` group (`wingcheck-${{ github.ref }}`,
`cancel-in-progress: false`) prevents two commit-and-push jobs (e.g. a
manual `station_research` run overlapping a scheduled `forecast` run) from
racing. The `forecast` job's manual-dispatch condition explicitly excludes
all three new flags, so ticking e.g. `sync_historical_data` alone can't
also silently trigger a real forecast + Telegram send as a side effect.
`tests/test_workflow.py` asserts each new job's safety properties directly
(manifest-only commits, no `weights.json` writes, no commits at all from
`promote_features`).

## Conventions specific to this codebase

- Feature names in `engineer_features()`'s returned dict, `weights.json`'s
  `weights` object, and any place a feature is looked up by name
  (`model.py`) must all match exactly. `features.FEATURE_NAMES` is the
  single source of truth for the 22-feature schema; `model.new_weights()`
  builds a fresh weights dict from it (or an explicit subset, for
  `ablation.py`) and `model.validate_schema()` raises `ValueError` on any
  drift between a weights dict and the expected feature set — call it
  right after building a fresh evaluation/deployment model.
- Never construct a "blank slate" model any way other than
  `model.new_weights()` — not by loading `weights.json` and resetting some
  fields by hand. Doing that once left `backtest.py`'s "2026 holdout"
  evaluation contaminated by a previous retrain's exposure to 2026 (see
  the "2026-07-16 leakage fix" note above). Every fresh-start path — an
  evaluation model trained only on past years, a deployment model trained
  on everything, ablation's per-feature-group models, a deliberate reset —
  goes through `new_weights()`.
- All prediction/backtest logs are JSONL (one JSON object per line),
  appended-to or fully rewritten with `save_predictions`/similar helpers —
  never hand-edit these files.
- `weights.json` is treated as a live, auto-updated artifact. Only hand-edit
  it to force a deliberate reset, and back it up first — any job run
  afterward will keep learning from wherever you leave it. It went through
  several feature-schema resets on 2026-07-16 (dropped `cloud_score`; added
  ensemble/persistence/interaction features, then `samedan_morning_score`,
  then `pressure_nowcast_score`) plus a window revert (15-18h back to
  12-18h) — **do not let `forecast_and_log.py` run against this until
  `backtest.py` has been re-run**, or it'll alert on a flat, untrained
  probability for every hour.
- Two ground-truth sources, two different roles — don't conflate them.
  `kitesailing_weather.py` (the real Silvaplana lake reading) is the
  PRIMARY label for the live loop (`verify_and_learn.py`), with `meteoswiss.py`
  (Samedan) as fallback + a secondary feature. But `backtest.py`'s historical
  retrain can ONLY use Samedan, since kitesailing has no historical archive.
  This means the weights `backtest.py` produces and the weights
  `verify_and_learn.py` nudges afterward are labeled on different criteria
  (`SAM_PROXY_KT` vs `SILVAPLANA_MARGINAL_KT`) — a real, currently-open
  mismatch, not an oversight. Resolve it by re-running `backtest.py`
  periodically as `logs/kitesailing_observations.jsonl` accumulates enough
  history to backtest against directly (not yet implemented — there's
  nothing to backtest until that log has real depth).
- Timestamps: raw feature times are naive local (`Europe/Zurich`); ground
  truth timestamps from MeteoSwiss are UTC; `kitesailing_weather.py`
  observations store `observed_at` as UTC ISO strings (`datetime.now
  (timezone.utc).isoformat()`). Conversions happen explicitly at the
  boundary in `verify_and_learn.py`, `backtest.py`, and `features.py`'s
  `_lookup_morning_obs()` (shared by `samedan_morning_score` and
  `pressure_nowcast_score`) — match that pattern rather than comparing
  naive and aware datetimes directly.
- Both `verify_and_learn.py` and `backtest.py` independently derive the
  outcome label and train with the same gradient-descent math as
  `model.update()` — `backtest.py` doesn't call into `model.py`'s
  `update()` because it trains over the full dataset in epochs rather than
  one online step at a time. Keep the learning rule in sync across both if
  you change it.
- **The 2026 season is not a pristine holdout for station/feature
  research, even though `backtest.py`'s own evaluation/deployment split
  still treats it correctly.** `backtest.py` looks at 2026 exactly once per
  run (the EVALUATION model is scored against it a single time and
  discarded) — that discipline is unchanged and still valid. But
  `station_analysis.py`, `model_regularized.py`, and `calibration_analysis.py`
  each inspect 2026 repeatedly across every feature/model/calibration
  comparison they run, which is a real multiple-comparison risk. Treat any
  single 2026-reference number from these research scripts as one data
  point, not proof — that's why `research_metrics.rolling_origin_splits()`
  labels it `kind="reference"` rather than `"holdout"`, and why the
  rolling-origin folds (which don't have this problem, since each one
  trains only on data strictly before its own validation period) are the
  primary evidence for any research finding. **Do not add a new
  station/feature to `feature_candidates.py` as `validated_stable` — and
  do not promote one to production — solely because one 2026-reference
  metric improved.** Consider 2027 as the next genuine, single-use holdout
  once it arrives.
- **New features are never added directly to `features.FEATURE_NAMES`.**
  Every candidate — a new station-derived signal, a transformed existing
  one, anything — must first exist in `feature_candidates.py` with an
  honest `research_status`, and must clear every step of
  `feature_candidates.PROMOTION_PROCESS` (coverage validation →
  rolling-origin validation → calibration check → operational-reliability
  check → manual approval) before a human edits `features.py`, bumps
  `model.SCHEMA_VERSION`, and re-runs `backtest.py`. This applies
  regardless of how promising a single correlation or ablation number
  looks — see the 2026-reuse bullet above for why a single number isn't
  sufficient evidence on its own.
- **A station is not usable for anything beyond exploratory research until
  `stations.py` marks it `verification="confirmed"`.** Setting that field
  requires an actual successful `historical_data.py sync --station <id>`
  fetch, inspected by a human — never set it based on a station merely
  being named in a task description, a webcam page, or general knowledge
  of the region. See `docs/STATION_RESEARCH.md` for the full list of
  investigated-but-unconfirmed candidates and why each one is still
  unconfirmed.
- Calibration (`calibration.py`'s Platt scaling / isotonic regression) is
  always fit on a rolling fold's TRAINING split only and evaluated on that
  same fold's validation split — never fit against the final 2026
  reference data, for the same reason standardization in
  `model_regularized.py` is fit only on training data: fitting a
  transform on data it will later be scored against is leakage, even if
  the transform itself isn't the predictive model.
- `logs/historical/station_hourly/*.jsonl`, `logs/historical/datasets/*.jsonl`,
  and `logs/historical/datasets/*.csv` are gitignored on purpose (see
  `docs/DATA_ARCHITECTURE.md`'s "What's committed vs. regenerable") —
  don't try to force-add them or "fix" the `.gitignore` entry; they
  regenerate from committed data in seconds via `historical_data.py sync`
  and committing them permanently reproduced a real ~1.1GB repository-size
  blowup once already this project. `logs/historical/manifests/`,
  `logs/historical/forecast_vintages/`, and `logs/historical/reports/`
  ARE committed — see the same doc for why those three are irreplaceable
  while the rest is derived.
