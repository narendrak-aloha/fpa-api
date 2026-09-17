#!/usr/bin/env bash
# Wait for Postgres, apply Alembic migrations, then run the given command.
set -euo pipefail

echo "==> waiting for Postgres"
until pg_isready -h "${PGHOST:-postgres}" -p "${PGPORT:-5432}" -U "${PGUSER:-postgres}" -q; do sleep 1; done

echo "==> alembic upgrade head"
alembic -c db/alembic.ini upgrade head

exec "$@"
