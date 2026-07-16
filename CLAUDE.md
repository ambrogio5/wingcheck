# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Malojawind: a self-improving forecast for the Maloja wind at Lake Silvaplana.
A logistic-regression-style model scores 21 engineered features (from 20+ raw
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
```

There is no test suite, linter, or build step configured in this repo — these
scripts are the only entry points. `forecast_and_log.py` needs
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
   `ecmwf_ifs025` via the `models=` param), and Samedan's own recent
   observations (via `meteoswiss.py`), and engineers them into 21 named
   features (`engineer_features`) — including an ensemble mean/agreement
   pair, a lag-based persistence feature, two interaction terms, and
   `samedan_morning_score` (Samedan's real measured wind around 07:00 local
   the same day — a genuine upstream nowcast, now that Samedan is no longer
   the ground truth, just a correlated secondary signal). Two fetch paths:
   `fetch_raw` (live, ~3 day forecast) and `fetch_raw_historical`
   (date-range archive, used only by `backtest.py`) — they must stay
   feature-compatible since the same `engineer_features` function scores
   both. `fetch_raw` fetches its own Samedan "recent" data for the nowcast
   feature; `fetch_raw_historical` does NOT (`backtest.py` already fetches
   the full multi-year Samedan archive once for labeling and injects it into
   `raw["samedan_obs"]` itself, rather than re-fetching that large archive
   per season). Both the ensemble fetch and the Samedan nowcast fetch are
   best-effort: on failure `engineer_features` falls back to a neutral value
   (0.5 agreement, 0.0 nowcast score) so a flaky extra API call can't take
   down the whole pipeline. `raw_snapshot(raw, idx)` is a companion function
   that returns every raw physical value, unnormalized - logged into
   `logs/predictions.jsonl` alongside the engineered features, since
   Open-Meteo's live API only serves ~3 months of history and even
   `backtest.py`'s archive fetch doesn't reproduce a genuine multi-day-lead
   forecast (see its own docstring). Once a live prediction ages out, this
   is the only remaining record of what the forecast actually said -
   without it, that data would be permanently unrecoverable for future
   feature engineering.
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
   and, via `features.py`, a live nowcast feature. Pulls real Samedan (SAM)
   station observations from MeteoSwiss's open STAC catalog (not another
   model run). `SAM_PROXY_KT` (default 8.0) is the calibration knob
   converting "wind at Samedan" into "rideable at the lake" for the fallback
   path only — tune it after real sessions, then re-run the backtest so
   labels regenerate consistently. It's also still the ONLY source with a
   multi-year historical archive, so `backtest.py` has to keep using it as
   the sole (not just fallback) ground truth for historical retraining.
5. **`forecast_and_log.py`** (scheduled 07:00 & 10:00 CEST) — runs
   `features.py` + `model.py` over the live forecast, tiers each hour in the
   12:00-18:00 window into GOOD/MARGINAL/UNLIKELY via thresholds in
   `weights.json`, sends a Telegram message, appends every scored hour to
   `logs/predictions.jsonl` with `verified: false`. `WINDOW_START_HOUR` /
   `WINDOW_END_HOUR` here must match `backtest.py`'s — the model is trained
   on exactly the hours it's later scored on in production.
6. **`sample_kitesailing`** job (scheduled every 15 min, 11:00-18:59 CEST -
   bracketing the 12:00-18:00 window with an hour of buffer each side,
   defined directly in the workflow file, no dedicated `.py` entry point
   beyond `kitesailing_weather.py`'s own `main()`) — the source of
   ground-truth data for step 7 below.
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
   model. The frozen 2026 holdout metrics from the original backtest are
   carried over unchanged, not recomputed (recomputing would let holdout
   data leak into "training" through the deployed weights). Also builds
   `upcoming_forecast` — the latest logged prediction per *future* target
   hour (`upcoming_forecast()`, deduped the same way as training) — this is
   what `docs/index.html` renders as "Next sessions"; it's the only part of
   the dashboard that answers the page's own headline question rather than
   reporting on past performance.
9. **`backtest.py`** (manual only, `workflow_dispatch`) — the only way to
   retrain from scratch. Builds a labeled dataset for May–Oct 2024/2025/2026
   from Open-Meteo's historical archive + real SAM obs (still the only
   ground truth available historically — see `meteoswiss.py` above), trains
   on 2024+2025, evaluates on a 2026 holdout (reporting accuracy against a
   trivial "always-no" baseline), then folds the holdout into training
   before saving deployed weights. Also (re)calibrates the GOOD/MARGINAL
   probability thresholds stored in `weights.json`. Goes through
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
    is refetched at most once per calendar day. The Samedan archive's
    expensive part (STAC catalog discovery + downloading every historical
    CSV) runs once ever; later calls just merge in the cheap "recent" file.

**Orchestration**: `.github/workflows/wingcheck.yml` defines four jobs
(`forecast`, `sample_kitesailing`, `learn`, `backtest`) gated by cron
schedule or `workflow_dispatch` input — see the comment header in that file
for exact triggers. Each job commits its own output (`weights.json`,
`logs/*.jsonl`, `docs/dashboard_data.json`) straight back to `main` with a
bot identity; there's no PR review step for these automated commits.
`sample_kitesailing` is the one job with a dependency beyond `requests`
(Playwright + Chromium) and caches the browser binary (`actions/cache`,
keyed on a pinned Playwright version) since it runs every 15 minutes.
`COPY-ME_workflow.yml` is a duplicate of the same file, meant to be copied
into `.github/workflows/` when bootstrapping a new deployment from this
template — keep the two in sync if you edit the workflow.

**`docs/`** is a static dashboard (GitHub Pages, served from `/docs` on
`main`) that fetches `dashboard_data.json` client-side; it has no build step,
just a single `index.html` with inline CSS/JS and Chart.js from a CDN.

## Conventions specific to this codebase

- Feature names in `engineer_features()`'s returned dict, `weights.json`'s
  `weights` object, and any place a feature is looked up by name
  (`model.py`) must all match exactly — there's no schema enforcement, just
  a shared naming convention across files.
- All prediction/backtest logs are JSONL (one JSON object per line),
  appended-to or fully rewritten with `save_predictions`/similar helpers —
  never hand-edit these files.
- `weights.json` is treated as a live, auto-updated artifact. Only hand-edit
  it to force a deliberate reset, and back it up first — any job run
  afterward will keep learning from wherever you leave it. It was reset to
  version 2 (all-zero weights, `trained_samples: 0`) on 2026-07-16, first
  after a feature-schema change (dropped `cloud_score`, added
  ensemble/persistence/interaction features), then again the same day after
  adding `samedan_morning_score` — **do not let `forecast_and_log.py` run
  against this until `backtest.py` has been re-run**, or it'll alert on a
  flat, untrained probability for every hour.
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
  `samedan_morning_score` lookup — match that pattern rather than comparing
  naive and aware datetimes directly.
- Both `verify_and_learn.py` and `backtest.py` independently derive the
  outcome label and train with the same gradient-descent math as
  `model.update()` — `backtest.py` doesn't call into `model.py`'s
  `update()` because it trains over the full dataset in epochs rather than
  one online step at a time. Keep the learning rule in sync across both if
  you change it.
