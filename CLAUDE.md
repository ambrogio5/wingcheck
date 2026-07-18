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
tuning removes. The larger lever — replacing Samedan as the label — has
since been pulled TWICE: the live loop verifies against the real
kitesailing.ch lake reading (`kitesailing_weather.py`, scraped since no API
exists), and as of 2026-07-18 `backtest.py`'s historical retrain labels
from MeteoSwiss Segl-Maria (SIA, ~4km from the lake, real 108k-record
hourly archive) under the shared SIA-first ground-truth policy — closing
the old historical-vs-live labeling-criterion mismatch. All pre-2026-07-18
accuracy figures in this file were measured against Samedan-proxy labels
and are NOT comparable to SIA-labeled runs — see the "Ground truth and
retraining gate" section below and `backtest.py`'s docstring.

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

# Historical archive / station research / diagnostics (see docs/DATA_ARCHITECTURE.md,
# docs/STATION_RESEARCH.md, docs/MALOJA_DIAGNOSTICS.md)
python historical_data.py sync        # incrementally refresh the station archive (idempotent, no-op if nothing new)
python historical_data.py validate    # data-quality + gap/staleness checks (non-blocking diagnostic)
python historical_data.py coverage    # per-station record counts / date ranges
python historical_data.py import-csv --station <id> --file <path> --format <name>
                                       # ingest a one-off manually-provided file (no live API - see
                                       # NO_LIVE_SOURCE_STATIONS / manual_station_import.py)
python station_analysis.py            # ten fixed station-family comparisons + correlation screen + calibration
python refresh_research_dashboard.py  # rebuild docs/research/research_data.json from the latest report (no network)
```

There is no linter or build step configured in this repo; there is an
offline `unittest` suite under `tests/` (stdlib only, no network calls) —
run it before trusting a change to `model.py`/`metrics.py`/`ablation.py`/
`backtest.py`. The historical-archive/research commands above are ALSO
research-only and never write `weights.json` or `docs/dashboard_data.json`
(the main operational dashboard) — enforced by `tests/test_station_analysis.py`
asserting `weights.json`'s mtime is unchanged after calling into
`station_analysis.py`. `historical_data.py sync` does attempt real network
calls (best-effort, catches all exceptions) but falls back to ingesting
whatever's already in `logs/raw_cache/` for the three confirmed stations,
so it's always safe to run even fully offline. `forecast_and_log.py` needs
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
   against a real fetch. `SAM_PROXY_KT` (default 8.0) survives only for
   any explicitly named legacy research experiment - since 2026-07-18
   Samedan is no longer a label source anywhere (see the ground-truth
   conventions below): `backtest.py` labels from Segl-Maria (SIA)'s real
   108k-record hourly archive, and Samedan serves as a model feature
   (`samedan_morning_score`) and per-row context only.
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
   from Open-Meteo's historical archive, labeled SIA-first through
   `ground_truth.select_label` against SIA's real hourly archive
   (`historical_cache.get_sia_archive`; hours without an acceptable
   observation are excluded, never Samedan-labeled - see the ground-truth
   conventions below). Trains
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
wind/recommendation, shown separately for today and tomorrow), "Session
outlook" (from the optional `session_forecast`/`daily_diagnostics`/
`station_health`/`model_agreement` fields - see "Historical data archive,
station research, and diagnostics" below; the whole section hides itself
when no issuance data exists for today, so a fresh checkout or a repo
predating this feature renders identically to before), "Live performance"
(from `live_metrics`, with a provisional-sample note below n=30), "Frozen
holdout evaluation" (from `evaluation`, full window and the 14:00-18:00
diagnostic prime window side by side, `evaluation.generated_at` shown with
a staleness flag past 30 days, and a "Run fresh evaluation" button linking
to
`https://github.com/<owner>/<repo>/actions/workflows/wingcheck.yml` in a
new tab - deliberately just a link, since a static page cannot safely call
the GitHub API without embedding a credential in frontend code), "Feature
ablation", and a collapsed "Technical details" section (operational
threshold confusion matrices, reproducibility seed, the weights list, and
the monthly/timeline charts).

## Historical data archive, station research, and diagnostics (this PR)

A parallel line of work sits alongside the operational pipeline above: a
durable historical station archive, a fixed-family station-research
driver, and a station-derived diagnostics/session-summary layer feeding
the dashboard's "Session outlook" section - all deliberately isolated from
the production model. Full detail is in **`docs/DATA_ARCHITECTURE.md`**,
**`docs/STATION_RESEARCH.md`**, and **`docs/MALOJA_DIAGNOSTICS.md`**; this
section is the short version.

11. **`station_registry.py`** / **`config/stations.json`** — the station
    metadata registry (id, coordinates, provider, roles, an honest
    `verification` field: `"confirmed"` / `"unverified"`, and `enabled`).
    Exactly three stations are `confirmed` and `enabled` (`sam`, `lug`,
    `sma` - the same three already used in production) because this
    branch's sync attempts against `data.geo.admin.ch` were blocked by the
    sandbox's network policy (confirmed via `historical_data.py sync`'s
    own `[warn] ... ProxyError ... 403 Forbidden` log). Every other
    candidate is recorded `enabled: false, verification: "unverified"` -
    see `docs/STATION_RESEARCH.md` for the full list.
    `validate_registry()` enforces the honesty invariant that an enabled
    station must be confirmed, tested by `tests/test_station_registry.py`.
12. **`historical_data.py`** — the durable archive's CLI (`sync` /
    `validate` / `coverage` / `import-csv`). Normalizes every station's
    hourly data to one canonical schema (`NORMALIZED_FIELDS`, 21 fields
    including `clouds_raw`, explicit UTC + Europe/Zurich timestamps, nulls
    not invented values) under `logs/historical/`. `sync` is idempotent and
    never overwrites a richer existing record with a sparser one
    (`merge_normalized_records()`); it tries the already-committed
    `logs/raw_cache/*.json` first, then a best-effort live fetch (catches
    all exceptions) - except for any station in `NO_LIVE_SOURCE_STATIONS`
    (currently just `sils`, a real lake station with no API at all, only a
    user-provided historical CSV - see `manual_station_import.py` and
    `docs/STATION_RESEARCH.md`'s "Sils / Segl" section), which
    `_attempt_live_fetch`/`station_nowcast.py`'s `_fetch_normalized_recent`
    both short-circuit before ever reaching the generic MeteoSwiss-fetch
    fallback. **`logs/historical/station_hourly/*.jsonl` is gitignored,
    deliberately** - fully regenerable in seconds via `historical_data.py
    sync`, and substantially more verbose per row than `raw_cache/`'s
    compact format - see `docs/DATA_ARCHITECTURE.md`.
13. **`data_quality.py`** — implausible-value, negative-speed,
    gust-less-than-speed, future-timestamp, duplicate, and gap/staleness
    checks (`STALE_ARCHIVE_DAYS = 30`), wired into `historical_data.py
    validate`. **Flags, never silently discards.**
14. **`forecast_vintages.py`** — archives the raw, genuine multi-model
    forecast payload (issue time, target times, lead-time-in-hours,
    checksum) *before* `forecast_and_log.py` scores it, via a best-effort
    `archive_forecast_payload_safe()` call that can never break the
    actual forecast/Telegram send on failure (logs visibly to stderr
    instead). Deduped by content checksum, gzip-compressed, stored under
    `logs/historical/forecast_vintages/YYYY/MM/DD/` and **committed
    permanently** (unlike the station archive) - a forecast vintage can
    never be recreated once its lead time has passed.
15. **`station_features.py`** — pre-forecast station feature generation at
    the 07:00/10:00 issuance cutoffs, with per-station reporting-delay
    discipline (an observation timestamped T is only "available" at
    T + reporting_delay_minutes). Generic per-station features
    (`latest_wind_speed`, wind-vector components, trends, dew-point
    depression, etc.) plus pairwise station-comparison helpers. **None of
    these are added to `features.FEATURE_NAMES`** - research/diagnostics
    only.
16. **`maloja_diagnostics.py`** — seven fixed diagnostic families (source
    heating, pass activation, summit support, radiation support, pressure
    support, competing flow, data health), each returning a fixed
    `{score, status, raw_values, sources, explanation_key, missing}`
    shape with a small, registered explanation-key vocabulary (never
    free-form generated prose). **Four of the seven have zero real
    station coverage today** (no confirmed source_region/pass/summit
    station) and honestly report `missing: true` rather than a fabricated
    score - only `pressure_support` (lug/sma) has real data. Fully
    implemented and tested via fixtures regardless, so a future confirmed
    station activates them with no code change.
17. **`session_forecast.py`** — deterministic session-level summary
    (onset/peak/decline, expected wind/gust range, event probability,
    timing/strength confidence) derived from one day's already-scored
    hourly forecasts. `event_probability` is the MAX hourly probability,
    matching the already-validated production convention. Confidence
    rules are a fixed penalty-subtraction formula (never a hidden model) -
    see `docs/MALOJA_DIAGNOSTICS.md` for the exact factors and cutoffs.
18. **`research_metrics.py`** / **`research_report.py`** — chronological,
    day-grouped rolling-origin (expanding-window) evaluation splits with
    2026 labeled `kind="reference"` (not `"holdout"`, since
    `station_analysis.py` inspects it repeatedly), day-level bootstrap
    confidence intervals, Benjamini-Hochberg FDR correction, and a
    provenance envelope (commit SHA, input-file checksum, config,
    warnings) for every saved report.
19. **`station_analysis.py`** — runs exactly **ten pre-registered, fixed**
    station-family comparisons (`FAMILY_DEFINITIONS`) via chronological
    rolling-origin evaluation - never an open-ended search over feature
    combinations. Also runs a correlation screen (Pearson/Spearman/
    point-biserial/ROC-AUC with day-level bootstrap CIs and FDR
    correction) and a calibration reliability summary (ECE) for the
    production feature set. **Never writes `weights.json`** - asserted in
    its own `main()` and verified offline by
    `tests/test_station_analysis.py`. Saves a timestamped JSON+Markdown
    report to `logs/historical/reports/`.
20. **`refresh_research_dashboard.py`** / **`docs/research.html`** — a
    second, completely separate dashboard (`docs/research/research_data.json`,
    no network calls, never touches the main `docs/dashboard_data.json`)
    showing station coverage, the fixed family comparison, the
    correlation screen, calibration, and data-health warnings -
    explicitly labeled "EXPLORATORY RESEARCH — NOT THE OPERATIONAL
    DASHBOARD".
21. **Dashboard optional fields** (`refresh_dashboard.py`'s
    `optional_issuance_fields()`) — `daily_diagnostics`, `session_forecast`,
    `station_health`, `model_agreement`, `data_provenance`, built from the
    latest `logs/forecast_issuances.jsonl` record and degrading to `{}`
    when that log doesn't exist yet. `forecast_and_log.py`'s
    `_log_issuance()` (best-effort, never raises) appends one record per
    issuance with `issued_at`, `model_version`, `feature_schema_version`,
    `calibration_version`, `station_cutoff`, `station_inputs`,
    `station_input_age`, `station_quality_flags`, `diagnostics`,
    `session_forecast`, `hourly_predictions`, `raw_payload_checksums`, and
    `commit_sha` - appended, never rewritten, so historical forecast
    records are never altered after the fact.

**Workflow jobs for the above** (`.github/workflows/wingcheck.yml`, kept
byte-identical to `COPY-ME_workflow.yml`): the `forecast` job now runs
`historical_data.py sync` before `forecast_and_log.py` (fresh station data
for the diagnostics) and commits the append-only
`logs/historical/forecast_vintages/` and `logs/forecast_issuances.jsonl`
alongside its existing commit. `sync_historical_data` (daily, 03:30 CEST,
plus manual - commits only `logs/historical/manifests/`) and
`station_research` (manual only, via the `run_station_analysis` input -
runs `station_analysis.py` + `refresh_research_dashboard.py`, commits
reports + research dashboard data, **never** `weights.json`) are new. A
workflow-level `concurrency` group (`wingcheck-${{ github.ref }}`,
`cancel-in-progress: false`) prevents two commit-and-push jobs from
racing. The `forecast` job's manual-dispatch condition explicitly excludes
both new flags, so ticking `sync_historical_data` or `run_station_analysis`
alone can't also silently trigger a real forecast + Telegram send.
`tests/test_workflow.py` asserts each new job's safety properties directly.
**There is no workflow option to promote a station feature into
production** - see `docs/STATION_RESEARCH.md`'s explicit prohibition for
this PR.

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
- Ground-truth roles, live and historical, are now ALIGNED on the same
  SIA-first policy (`config/ground_truth_policy.json` v2 — see the
  "Ground truth and retraining gate" section below): the live loop
  (`verify_and_learn.py`) labels kitesailing lake reading → SIA →
  unverified; `backtest.py`'s historical retrain labels from SIA's real
  108k-record hourly archive (`historical_cache.get_sia_archive`) through
  the same `ground_truth.select_label` machinery, excluding (never
  proxy-labeling) hours without an acceptable observation. Both use the
  shared `ground_truth.SIA_REFERENCE_KT` (10kt) criterion — the old
  `SAM_PROXY_KT`-vs-`SILVAPLANA_MARGINAL_KT` labeling-criterion mismatch
  is CLOSED (2026-07-18). Samedan is still fetched by `backtest.py` — as
  a feature input (`samedan_morning_score`) and per-row context
  (`samedan_wind_kt`/`samedan_gust_kt`) — but never as the label.
  `SAM_PROXY_KT` survives in `meteoswiss.py` only for any explicitly
  named legacy research experiment. Backtest metrics from SIA-labeled
  runs are NOT comparable to older Samedan-labeled figures (the
  `reproducibility.label_source` field in `docs/dashboard_data.json`
  records which criterion produced the current numbers).
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
- **No station-derived feature is ever added directly to
  `features.FEATURE_NAMES`.** `station_features.py`'s generic features and
  `maloja_diagnostics.py`'s family scores exist only for
  `station_analysis.py`'s research comparisons and the dashboard's
  "Session outlook" - promoting one into production would require, at
  minimum, the underlying station being confirmed (see
  `docs/STATION_RESEARCH.md`), a stable improvement across multiple
  rolling-origin folds (not just the repeatedly-inspected 2026
  reference), and a deliberate, separate, human-reviewed source-code
  change. This PR does not do that, and is explicitly not scoped to.
- **A station is not usable for anything beyond exploratory research
  until `config/stations.json` marks it `verification: "confirmed"` AND
  `enabled: true`.** Setting those fields requires an actual successful
  `historical_data.py sync --station <id>` fetch, inspected by a human -
  never based on a station merely being named in a task description or
  general regional knowledge. `station_registry.validate_registry()`
  enforces the invariant that an enabled station must be confirmed.
- **2026 is a repeatedly-inspected reference for station research, not a
  pristine holdout**, even though `backtest.py`'s own evaluation/
  deployment split still treats it correctly (looking at it exactly once
  per run). `station_analysis.py` inspects it across all ten fixed family
  comparisons plus the correlation screen, which is a real
  multiple-comparison risk - that's why `research_metrics.rolling_origin_splits()`
  labels it `kind="reference"` rather than `"holdout"`, and why the five
  rolling folds (which train only on data strictly before their own
  validation period) are the primary evidence for any research finding,
  not the 2026 number alone.
- `logs/historical/station_hourly/*.jsonl` is gitignored on purpose (see
  `docs/DATA_ARCHITECTURE.md`'s "What's committed vs. regenerable") -
  don't force-add it or "fix" the `.gitignore` entry; it regenerates from
  committed data in seconds via `historical_data.py sync`.
  `logs/historical/manifests/`, `logs/historical/forecast_vintages/`, and
  `logs/historical/reports/` ARE committed - see the same doc for why
  those three are irreplaceable while the rest is derived.
- `logs/forecast_issuances.jsonl` is append-only, one record per
  `forecast_and_log.py` run (not per hour, unlike `logs/predictions.jsonl`)
  - never hand-edit or rewrite historical entries in it.

## Ground truth and retraining gate

- `ground_truth.py` is the canonical observation registry. Never collapse
  different stations at ingestion time or strip `label_provenance` from a
  prepared training row.
- Label priority (`config/ground_truth_policy.json`, policy_version 2, the
  provisional SIA-first policy): direct lake observations (`kitesailing` /
  `windsurfcenter` / `silvaplana_lake`) always outrank SIA; SIA is the
  principal reference when no lake reading exists; a missing lake+SIA hour
  stays UNLABELED. **Samedan is context only, never a default label**
  (`allow_samedan_fallback: false`) - re-enable it only inside an
  explicitly named research experiment, never silently.
  `verify_and_learn.py` implements the same priority live and never
  rewrites rows verified under the old samedan_fallback policy.
- SIA's measurement quality is a separate question from its equivalence to
  the lake target, which is UNMEASURED (`sia_confidence: null` on purpose;
  calibration maturity gates in `station_calibration.py`: <14 independent
  overlapping days = insufficient, 14-41 preliminary, 42+ calibration
  candidate). The calibration report is descriptive and must never
  silently change policy or `weights.json` - policy changes are reviewed
  edits of the versioned policy file citing a real report.
- SIA's real archive has an open 2010-2025 gap (see
  `docs/DATA_ARCHITECTURE.md`'s "SIA ingestion" section, including the
  correction of a fabricated earlier "108,116 rows" claim, and why
  `station_hourly/sia.jsonl` is a flagged, derived mean-of-10-minute
  product). `sia_import.py` is the ingestion path for the real raw files;
  informational derivation flags (`derived_from_10min_mean`,
  `n_10min_samples:N`) never disqualify a record from labeling -
  `ground_truth.blocking_flags()` is the arbiter of real quality flags.
- Confidence currently records evidence quality only. Do not use it as a loss
  weight without a separate rolling-origin experiment and review.
- `retraining_dataset.py` prepares rows; it does not retrain. Production
  retraining must start from `model.new_weights()` and preserve the existing
  evaluation/deployment separation. `model_comparison_sia.py` is the
  research-only comparison harness (asserts `weights.json` untouched);
  there is deliberately NO workflow option that promotes a model - the
  `run_ground_truth_research` dispatch job commits reports/manifests only.
- The retraining gate was PASSED and acted on (2026-07-18, human-approved):
  once the real CI fetch of SIA's full 108k-record hourly archive closed
  the label-coverage gap (3,111/3,111 backtest rows SIA-labelable, all
  three seasons), the research comparison showed consistent chronological
  improvement across both expanding-window year folds (validate-2025 acc
  0.744 vs 0.651, validate-2026 0.755 vs 0.583, Brier improved on both -
  `model_comparison_sia_20260717T220508.json`), and the repo owner
  approved. `backtest.py` now labels SIA-first (see the aligned-roles
  bullet in "Conventions" above). Lake/SIA calibration itself remains
  `insufficient_evidence` - the SIA-first policy does not claim
  equivalence, it claims SIA is the best available reference until lake
  overlap accumulates past the 14-independent-day gate.
