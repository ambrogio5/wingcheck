# `station_raw/meteoswiss/`

This directory is where **original, unmodified source bytes** (the raw CSV
files as downloaded from `data.geo.admin.ch`, or a lossless compressed
form of them) are meant to live, one file per `<provider>_<station>_
<granularity>_<start>_<end>_<checksum-prefix>.csv` per `historical_data.py`'s
naming scheme.

**This directory is currently empty.** The project's existing raw fetch
path (`historical_cache.py` / `meteoswiss.py`, built before this
historical-archive work) only ever kept the *parsed* Python dict in
`logs/raw_cache/*.json` - the original CSV bytes returned by MeteoSwiss
were never written to disk. `historical_data.py sync` normalizes from
that already-parsed cache for the three confirmed stations (`sam`, `lug`,
`sma`), so today's `station_hourly/` files are real, but they are not
literally traceable back to a byte-identical original source file the
way this directory is designed to support going forward.

Starting from the next real sync run in an environment with actual
network access to `data.geo.admin.ch` (this raw-vs-normalized separation
was added in a sandboxed session where that host was blocked at the
network policy level - see `docs/STATION_RESEARCH.md`), new fetches
should populate this directory with the real source files, so future
re-normalization or a schema change never requires re-downloading.
