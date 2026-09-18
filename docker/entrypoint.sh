#!/usr/bin/env bash
# Wait for Postgres, migrate, seed, then run the given command.
# Seeding runs every start but only works once: db.seed upserts, and
# seed_clickhouse.sh skips a non-empty cube. FPA_SKIP_SEED=1 skips both.
set -euo pipefail

# Host claude CLI, for the claude-code provider.
if [ -d /opt/claude/versions ]; then
    latest="$(ls -d /opt/claude/versions/* 2>/dev/null | sort -V | tail -1)"
    [ -n "$latest" ] && ln -sf "$latest" /usr/local/bin/claude && echo "==> claude CLI $(basename "$latest") available"
fi

echo "==> waiting for Postgres"
until pg_isready -h "${PGHOST:-postgres}" -p "${PGPORT:-5432}" -U "${PGUSER:-postgres}" -q; do sleep 1; done

echo "==> alembic upgrade head"
alembic -c db/alembic.ini upgrade head
alembic -c db/alembic.ini current

if [ "${FPA_SKIP_SEED:-0}" = "1" ]; then
    echo "==> FPA_SKIP_SEED=1, not seeding"
else
    echo "==> seeding governance store from db/seed.yaml"
    python -m db.seed
    # ~1M rows the first time; a no-op after.
    scripts/seed_clickhouse.sh
fi

echo "==> API on http://localhost:8000"
exec "$@"
