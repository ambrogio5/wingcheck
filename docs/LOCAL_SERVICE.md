# Running Wingcheck continuously on a Mac

GitHub remains the source of truth for code, pull requests, and CI. Collected
weather, model state, generated dashboard data, PostgreSQL files, service
status, and backups live in `WINGCHECK_DATA_DIR`, outside the checkout.

## First setup

1. Install Docker Desktop and ensure it starts when you sign in.
2. Copy `.env.example` to `.env`. Set `WINGCHECK_DATA_DIR` to an absolute
   location such as `/Users/you/Wingcheck/data`, choose a strong database
   password, and optionally add the Telegram values.
3. Create the data directory: `mkdir -p /Users/you/Wingcheck/data`.
4. Build and start: `docker compose up -d --build`.
5. Open the **main live dashboard** at <http://127.0.0.1:8081>. This is the
   canonical Wingcheck interface; do not run a second static preview server
   for normal use. Operational status is at
   <http://localhost:8080/api/status>.

The first start seeds the existing logs, weights, and dashboard data into the
external directory. Subsequent image rebuilds never overwrite that state.

## Automatic startup

Run `chmod +x scripts/install-macos-service.sh`, then
`scripts/install-macos-service.sh`. The LaunchAgent asks Docker Compose to
start the stack after login; Docker's restart policies recover individual
services after crashes. Docker Desktop must be running. A MacBook must remain
awake and connected; the display may sleep.

## Services and schedule

- `collector`: automatic lake scrape every 15 minutes. The script enforces its
  daylight collection window; no manual Telegram readings are required.
- `telegram`: checks every 30 seconds for an authorized `/report` request and
  replies with the latest lake wind and forecast summary.
- `scheduler`: station/archive sync at 03:30, forecasts at 07:00 and 10:00,
  learning at 20:00, all in `TZ` (default Europe/Zurich). The evening job also
  regenerates the SIA/lake calibration report; maturity gates remain advisory
  and it never changes the ground-truth policy automatically.
- `api`: local health, service status, and dashboard-data endpoints.
- `dashboard`: the canonical live dashboard on port 8081, served by nginx and
  connected to the local API/runtime data.
- `postgres`: durable database foundation for migration away from JSONL.
  Current forecasting scripts still use the external JSONL archive, so no
  historical behavior changes in this deployment.
- `backup`: nightly 02:00 database dump and runtime archive, retained 30 days.

The application image deliberately installs PostgreSQL client 16 to match the
PostgreSQL 16 server. Keeping `pg_dump` and the restore target on the same major
version avoids producing dumps with settings an older server cannot understand.

Jobs use per-job file locks, bounded retries, and atomic status files. Docker
logs rotate at five 10 MB files per service.

## Operations

Use `docker compose ps` for health, `docker compose logs -f collector scheduler`
for activity, `docker compose restart SERVICE` to restart one component, and
`docker compose down` to stop the stack without deleting data. Never use
`docker compose down -v` unless you intentionally want Docker-managed volumes
removed; Wingcheck's main data remains at `WINGCHECK_DATA_DIR`.

Before a code update, commit or stash development changes. Then `git pull` and
`docker compose up -d --build`. Runtime data is not committed or overwritten.
GitHub Actions stays enabled for pull-request validation; once local collection
is verified, disable only the scheduled operational workflows in GitHub to
avoid duplicate observations. Manual workflow dispatches remain useful as an
emergency fallback.

## Backup recovery

Runtime archives and PostgreSQL dumps are under
`$WINGCHECK_DATA_DIR/backups`. Stop the stack before restoring a runtime
archive. Restore PostgreSQL with `pg_restore` into an empty database. Test a
restore periodically; an untested backup is not a recovery plan.

Run `scripts/check-backup-restore.sh` to verify the newest pair. It safely
extracts the runtime archive into a temporary directory, parses key JSON files,
restores the PostgreSQL dump into a uniquely named temporary database, runs a
query, and drops that temporary database. It never modifies the production
runtime or database.
