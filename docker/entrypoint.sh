#!/usr/bin/env bash
# Wait for Postgres, apply Alembic migrations, then run the given command.
set -euo pipefail

# Install anything in requirements.txt that the image does not already satisfy;
# a no-op when the image is current, and it fails the start when a package is unavailable.
echo "==> checking dependencies"
uv pip install --system --quiet -r requirements.txt

# Expose the host's Claude Code CLI (mounted read-only) so the claude-code provider works.
if [ -d /opt/claude/versions ]; then
    latest="$(ls -d /opt/claude/versions/* 2>/dev/null | sort -V | tail -1)"
    [ -n "$latest" ] && ln -sf "$latest" /usr/local/bin/claude && echo "==> claude CLI $(basename "$latest") available"
fi

echo "==> waiting for Postgres"
until pg_isready -h "${PGHOST:-postgres}" -p "${PGPORT:-5432}" -U "${PGUSER:-postgres}" -q; do sleep 1; done

echo "==> alembic upgrade head"
alembic -c db/alembic.ini upgrade head

exec "$@"
