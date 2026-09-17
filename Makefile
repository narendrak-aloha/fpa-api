# Local Docker stack: ClickHouse, Postgres, Temporal and the fpa-dev-1 app container.
COMPOSE := docker compose -f docker/docker-compose.yml
APP     := fpa-dev
SCHEMA  := fpa_governance
CUBE    := fpa_cube

.PHONY: help docker-local-run docker-local-stop docker-local-logs docker-seed-db docker-reinit docker-shell \
        docker-make-migrations docker-migrate docker-migrate-down docker-migrate-status

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-20s %s\n", $$1, $$2}'

docker-local-run: ## Build and start every service, migrate, then follow the app log (Ctrl+C leaves it running)
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps
	@echo
	@echo "==> alembic upgrade head"
	@$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head
	@$(COMPOSE) exec $(APP) alembic -c db/alembic.ini current
	@echo
	@echo "API http://localhost:8000  |  following fpa-dev-1 logs, Ctrl+C detaches without stopping"
	@$(COMPOSE) logs -f --tail=20 $(APP)

docker-local-stop: ## Stop every service, keeping the data volumes
	$(COMPOSE) stop

docker-local-logs: ## Follow the app container's logs
	$(COMPOSE) logs -f $(APP)

docker-shell: ## Open a shell inside fpa-dev-1
	$(COMPOSE) exec $(APP) bash

# `make docker-make-migrations -m "add plan comment"`: make ignores -m and passes the
# words as extra goals, so collect them here; the catch-all below keeps make quiet.
ifneq (,$(filter docker-make-migrations,$(MAKECMDGOALS)))
MIGRATION_MSG := $(or $(m),$(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS)))
%:
	@:
endif

docker-make-migrations: ## Generate a migration from db/models.py (use: make docker-make-migrations -m "add plan comment")
	@test -n "$(MIGRATION_MSG)" || { echo 'usage: make docker-make-migrations -m "short message"'; exit 2; }
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini revision --autogenerate -m "$(MIGRATION_MSG)"
	@echo "Review the new file in db/migrations/versions, then: make docker-migrate"

docker-migrate: ## Apply all pending migrations (alembic upgrade head)
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head

docker-migrate-down: ## Roll back one migration (use: make docker-migrate-down rev=006 to target a revision)
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini downgrade $(or $(rev),-1)

docker-migrate-status: ## Show the applied revision, pending changes and history
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini current
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini check
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini history

docker-seed-db: ## Seed Postgres (migrations + seed.yaml) and the ClickHouse cube
	$(COMPOSE) exec $(APP) scripts/seed_postgres.sh
	$(COMPOSE) exec $(APP) scripts/seed_clickhouse.sh

docker-reinit: ## Drop the governance schema and the cube, then rebuild and seed both
	$(COMPOSE) exec $(APP) psql -h $$PGHOST -U postgres -d fpa -c "DROP SCHEMA IF EXISTS $(SCHEMA) CASCADE"
	$(COMPOSE) exec $(APP) curl -sS -u default:fpa "http://clickhouse:8123/" --data-binary "DROP DATABASE IF EXISTS $(CUBE)"
	$(COMPOSE) exec $(APP) scripts/seed_postgres.sh
	$(COMPOSE) exec -e RESEED_CUBE=1 $(APP) scripts/seed_clickhouse.sh
