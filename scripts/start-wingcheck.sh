#!/bin/sh
set -eu

repo="$(cd "$(dirname "$0")/.." && pwd)"
docker_bin="/usr/local/bin/docker"
attempt=0

# Docker Desktop and login agents start independently. Wait up to five
# minutes for its engine so Wingcheck does not lose a startup race at login.
until "$docker_bin" info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Docker Desktop did not become ready within five minutes" >&2
    exit 1
  fi
  sleep 5
done

exec "$docker_bin" compose --project-directory "$repo" up -d --remove-orphans
