# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Malojawind: a self-improving forecast for the Maloja wind at Lake Silvaplana.
A logistic-regression-style model scores 20 engineered features (from 20+ raw
data points, including a multi-model wind ensemble) to predict whether a
given afternoon hour (15:00-18:00 — narrowed from 12:00-18:00, see below)
will be a good wingfoil session, sends Telegram alerts, verifies its own
predictions against a real MeteoSwiss station, and retrains its weights
automatically via scheduled GitHub Actions. There is no server, database, or
app framework — it's a handful of plain scripts orchestrated entirely by
cron-scheduled GitHub Actions jobs that commit their own output back to the
repo.

**Accuracy ceiling**: a 2026 holdout analysis found the 2026-07 model barely
beat a trivial baseline (67.9% vs 53.6% accuracy, AUC 0.75) — 15 of the then
16 features added almost nothing over the raw forecast wind alone (AUC
0.743). Root cause: the forecast-to-groundtruth correlation is only ~0.52
(Samedan is 10km from the target lake) and ~31% of hours land within ±2kt of
the labeling threshold, an inherent noise floor no amount of feature/model
tuning removes. The window narrowing, ensemble/persistence/interaction
features, and `cloud_score` removal below target that ceiling directly; a
further, larger lever (not yet done — needs a data source from the user) is
swapping Samedan for the private Silvaplana lake station referenced in
`verify_and_learn.py`'s docstring.

## Commands

```bash
pip install -r requirements.txt   # only dependency: requests

python forecast_and_log.py        # fetch forecast, score, send Telegram alert, log predictions
python verify_and_learn.py        # check past predictions against real Samedan data, update weights.json
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

## Architecture

**Data flow pipeline**, in the order jobs run:

1. **`features.py`** — pulls raw values from Open-Meteo (forecast or
   historical-archive API) across three locations (Silvaplana target spot,
   Bregaglia source valley, Maloja Pass upper air) plus Lugano/Zürich
   pressure and a multi-model wind ensemble (`icon_seamless`, `gfs_seamless`,
   `ecmwf_ifs025` via the `models=` param), and engineers them into 20 named
   features (`engineer_features`) — including an ensemble mean/agreement
   pair, a lag-based persistence feature, and two interaction terms on top
   of the original physical drivers. Two fetch paths: `fetch_raw` (live, ~3
   day forecast) and `fetch_raw_historical` (date-range archive, used only by
   `backtest.py`) — they must stay feature-compatible since the same
   `engineer_features` function scores both. The ensemble fetch
   (`_fetch_ensemble_wind`) is best-effort: on failure it returns `None` and
   `engineer_features` falls back to the single deterministic forecast with
   a neutral 0.5 agreement score, so a flaky extra API call can't take down
   the whole pipeline.
2. **`model.py`** — a from-scratch logistic unit: `score()` (sigmoid of
   `bias + Σ weight_i * feature_i`) and `update()` (one online
   gradient-descent step given an actual 0/1 outcome). No ML framework;
   weights live in `weights.json` (bias, per-feature weights, tier
   thresholds, `trained_samples` counter used to decay the learning rate).
3. **`meteoswiss.py`** — ground truth only. Pulls real Samedan (SAM) station
   observations from MeteoSwiss's open STAC catalog (not another model run).
   `SAM_PROXY_KT` (default 8.0) is the calibration knob converting "wind at
   Samedan" into "rideable at the lake" — tune it after real sessions, then
   re-run the backtest so labels regenerate consistently.
4. **`forecast_and_log.py`** (scheduled 07:00 & 10:00 CEST) — runs
   `features.py` + `model.py` over the live forecast, tiers each hour in the
   15:00-18:00 window into GOOD/MARGINAL/UNLIKELY via thresholds in
   `weights.json`, sends a Telegram message, appends every scored hour to
   `logs/predictions.jsonl` with `verified: false`. `WINDOW_START_HOUR` /
   `WINDOW_END_HOUR` here must match `backtest.py`'s — the model is trained
   on exactly the hours it's later scored on in production.
5. **`verify_and_learn.py`** (scheduled 20:00 CEST) — reads unverified
   predictions at least `MIN_AGE_HOURS` (20h) old, fetches real SAM
   observations, labels the outcome, and calls `model.update()` — but only
   once per target hour (the *latest* of possibly several predictions for
   that hour, since the same hour gets forecast repeatedly across the
   3-day rolling window) to avoid overweighting duplicate samples.
6. **`refresh_dashboard.py`** (scheduled after verify_and_learn) — rebuilds
   `docs/dashboard_data.json` purely from local files (no network): merges
   `logs/backtest_dataset.jsonl` (historical) with verified entries from
   `logs/predictions.jsonl` (live), re-scoring everything with the *current*
   weights so the dashboard's probability trace always reflects today's
   model. The frozen 2026 holdout metrics from the original backtest are
   carried over unchanged, not recomputed (recomputing would let holdout
   data leak into "training" through the deployed weights).
7. **`backtest.py`** (manual only, `workflow_dispatch`) — the only way to
   retrain from scratch. Builds a labeled dataset for May–Oct 2024/2025/2026
   from Open-Meteo's historical archive + real SAM obs, trains on
   2024+2025, evaluates on a 2026 holdout (reporting accuracy against a
   trivial "always-no" baseline), then folds the holdout into training
   before saving deployed weights. Also (re)calibrates the GOOD/MARGINAL
   probability thresholds stored in `weights.json`.

**Orchestration**: `.github/workflows/wingcheck.yml` defines three jobs
(`forecast`, `learn`, `backtest`) gated by cron schedule or
`workflow_dispatch` input — see the comment header in that file for exact
triggers. Each job commits its own output (`weights.json`,
`logs/*.jsonl`, `docs/dashboard_data.json`) straight back to `main` with a
bot identity; there's no PR review step for these automated commits.
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
  version 2 (all-zero weights, `trained_samples: 0`) on 2026-07-16 after a
  feature-schema change (dropped `cloud_score`, added ensemble/persistence/
  interaction features) — **do not let `forecast_and_log.py` run against
  this until `backtest.py` has been re-run**, or it'll alert on a flat,
  untrained probability for every hour.
- Timestamps: raw feature times are naive local (`Europe/Zurich`); ground
  truth timestamps from MeteoSwiss are UTC. Conversions happen explicitly at
  the boundary in `verify_and_learn.py` and `backtest.py` — match that
  pattern rather than comparing naive and aware datetimes directly.
- Both `verify_and_learn.py` and `backtest.py` independently derive the
  outcome label (`actual_kt >= SAM_PROXY_KT`) and train with the same
  gradient-descent math as `model.update()` — `backtest.py` doesn't call
  into `model.py`'s `update()` because it trains over the full dataset in
  epochs rather than one online step at a time. Keep the learning rule in
  sync across both if you change it.
