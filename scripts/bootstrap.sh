#!/usr/bin/env bash
# One-command clean-machine bring-up: containers -> health -> seed -> migrations.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from .env.example (edit in your LLM API key if you want the agent tier live)"
fi

echo "==> starting containers"
docker compose up -d --build

wait_for() {
  local name="$1" cmd="$2" tries=0
  until eval "$cmd" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 60 ]; then
      echo "!! $name did not become healthy in time" >&2
      exit 1
    fi
    sleep 2
  done
  echo "==> $name is up"
}

wait_for "postgres" "docker compose exec -T postgres pg_isready -U postgres -d fpa"
wait_for "clickhouse" "curl -sf -u default:fpa 'http://localhost:8123/?query=SELECT+1'"
wait_for "temporal" "curl -sf http://localhost:8233"

echo "==> seeding clickhouse cube"
uv run python seed_fpa.py --password fpa --drop

if [ -f alembic.ini ]; then
  echo "==> running postgres migrations"
  # bootstrap runs on the host, so hit postgres on its published port, not
  # the in-network hostname the api/worker containers use.
  DATABASE_URL="postgresql+asyncpg://postgres:fpa@localhost:5431/fpa" uv run alembic upgrade head
fi

echo "==> bootstrap complete"
