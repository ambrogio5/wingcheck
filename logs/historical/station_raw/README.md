Reserved for raw provider payloads too large or too provider-specific to
normalize losslessly. Currently empty: the two enabled stations' raw data
already lives in the small, committed `logs/raw_cache/*.json` files (used
by `backtest.py`) and `historical_data.py` references those directly via
`manifests/assets.jsonl` rather than duplicating them here - see
docs/DATA_ARCHITECTURE.md's "What's committed vs. regenerable" section.
