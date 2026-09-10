.PHONY: bootstrap up down seed migrate test lint

bootstrap:
	./scripts/bootstrap.sh

up:
	docker compose up -d --build

down:
	docker compose down

seed:
	uv run python seed_fpa.py --password fpa --drop

migrate:
	uv run alembic upgrade head

test:
	uv run pytest

lint:
	uv run ruff check .
