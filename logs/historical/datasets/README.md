# `datasets/`

Research-ready exports produced by `historical_data.py export-training`
(the normalized station archive) and any dataset built by the analysis
scripts (`station_analysis.py`, `regime_analysis.py`, etc.) for their own
reproducibility.

Like `../station_hourly/`, the actual `.jsonl`/`.csv` files here are
git-ignored by default - they're regenerable from the committed archive
manifests and `logs/backtest_dataset.jsonl`. See
`docs/DATA_ARCHITECTURE.md`.
