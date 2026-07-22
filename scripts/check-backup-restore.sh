#!/bin/sh
set -eu

repo="$(cd "$(dirname "$0")/.." && pwd)"
exec docker compose --project-directory "$repo" run --rm backup python local_service.py restore-check
