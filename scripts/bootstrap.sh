#!/usr/bin/env bash
# Start the stack and load both databases. Safe to re-run.
#   scripts/bootstrap.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
echo "==> checking dependencies"
"$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt

echo "==> docker compose up -d"
docker compose -f docker/docker-compose.yml up -d

scripts/seed_postgres.sh
scripts/seed_clickhouse.sh

echo
echo "ClickHouse  http://localhost:8123   Postgres  localhost:5431   Temporal UI  http://localhost:8233"
