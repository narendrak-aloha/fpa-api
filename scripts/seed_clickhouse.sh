#!/usr/bin/env bash
# Load the ClickHouse cube from data/seed_fpa.py, only when it is empty.
#   scripts/seed_clickhouse.sh          seed if empty
#   RESEED_CUBE=1 scripts/seed_clickhouse.sh   drop and rebuild
# Env: CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD.
set -euo pipefail

cd "$(dirname "$0")/.."
HOST="${CLICKHOUSE_HOST:-localhost}"; PORT="${CLICKHOUSE_PORT:-8123}"
USER="${CLICKHOUSE_USER:-default}"; PASSWORD="${CLICKHOUSE_PASSWORD:-fpa}"
PY="${PYTHON:-python3}"
query() { curl -sS --fail-with-body -u "${USER}:${PASSWORD}" "http://${HOST}:${PORT}/" --data-binary "$1"; }

echo "==> waiting for ClickHouse on ${HOST}:${PORT}"
for _ in $(seq 1 60); do query "SELECT 1" >/dev/null 2>&1 && break || sleep 1; done

ROWS=$(query "SELECT count() FROM fpa_cube.fact_gl_actual" 2>/dev/null || echo 0)
if [ "${RESEED_CUBE:-0}" != "1" ] && [ "${ROWS:-0}" -gt 0 ]; then
    echo "==> cube already has ${ROWS} actual rows; skipping (RESEED_CUBE=1 to rebuild)"
    exit 0
fi

echo "==> seeding cube (about 12s)"
"$PY" data/seed_fpa.py --host "$HOST" --port "$PORT" --user "$USER" --password "$PASSWORD" --drop --out data/out

echo "==> done: $(query "SELECT count() FROM fpa_cube.fact_gl_actual") actual rows"
