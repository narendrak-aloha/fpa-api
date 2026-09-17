#!/usr/bin/env bash
# Apply governance migrations and load db/seed.yaml. Safe to re-run.
#   scripts/seed_postgres.sh
# Env: PGHOST_PORT (default 5431), FPA_GOVERNANCE_DB_URL to target another database.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PGHOST_PORT:-5431}"
PY="${PYTHON:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
export FPA_GOVERNANCE_DB_URL="${FPA_GOVERNANCE_DB_URL:-postgresql+psycopg://postgres:fpa@localhost:${PORT}/fpa}"

echo "==> waiting for Postgres at ${FPA_GOVERNANCE_DB_URL##*@}"
for _ in $(seq 1 60); do
    "$PY" - <<'PYEOF' && break || sleep 1
import os, sys
import psycopg
url = os.environ["FPA_GOVERNANCE_DB_URL"].replace("postgresql+psycopg", "postgresql")
try:
    psycopg.connect(url, connect_timeout=2).close()
except Exception:
    sys.exit(1)
PYEOF
done

echo "==> applying migrations"
"$PY" -m alembic -c db/alembic.ini upgrade head

echo "==> loading db/seed.yaml"
"$PY" -m db.seed

echo "==> done: $("$PY" -m alembic -c db/alembic.ini current 2>/dev/null | tail -1)"
