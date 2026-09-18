# Local Docker stack: ClickHouse, Postgres, Temporal and the fpa_app-1 app container.
# --env-file: without it compose reads docker/.env, not the repo root.
ENV_FILE := .env
COMPOSE  := docker compose --env-file $(ENV_FILE) -f docker/docker-compose.yml
APP     := fpa-dev
SCHEMA  := fpa_governance
CUBE    := fpa_cube

.PHONY: help env env-check docker-local-run docker-local-run-d docker-local-stop docker-local-logs docker-seed-db docker-reinit docker-shell \
        docker-make-migrations docker-migrate docker-migrate-down docker-migrate-status

$(ENV_FILE):
	@cp .env.example $(ENV_FILE) && echo "created $(ENV_FILE) from .env.example — edit it to add API keys"

env: $(ENV_FILE) ## Create .env from .env.example if it does not exist yet (API keys)
	@echo "$(ENV_FILE):"
	@grep -vE '^\s*(#|$$)' $(ENV_FILE) | sed 's/^/  /' || true

env-check: $(ENV_FILE) ## Show the values compose will actually use (after .env and shell overrides)
	@$(COMPOSE) config | sed -n '/fpa-dev:/,/volumes:/p' | grep -E '^\s{6}[A-Z_]+:' | sed 's/^ */  /'

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-20s %s\n", $$1, $$2}'

# Attached: the first run spends ~12s seeding, which `up -d` made look like a hang.
# entrypoint.sh owns migrations and seeding; nothing here repeats them.
docker-local-run: $(ENV_FILE) ## Build and start every service in the foreground (Ctrl+C stops the stack)
	$(COMPOSE) up --build

docker-local-run-d: $(ENV_FILE) ## Same, but in the background, then follow the app log (Ctrl+C leaves it running)
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps
	@echo
	@$(COMPOSE) logs -f --tail=40 $(APP)

docker-local-stop: $(ENV_FILE) ## Stop every service, keeping the data volumes
	$(COMPOSE) stop

docker-local-logs: $(ENV_FILE) ## Follow the app container's logs
	$(COMPOSE) logs -f $(APP)

docker-shell: $(ENV_FILE) ## Open a shell inside fpa_app-1
	$(COMPOSE) exec $(APP) bash

# make treats -m's words as extra goals, so collect them here; the catch-all
# below keeps make quiet about them.
ifneq (,$(filter docker-make-migrations,$(MAKECMDGOALS)))
MIGRATION_MSG := $(or $(m),$(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS)))
%:
	@:
endif

docker-make-migrations: $(ENV_FILE) ## Generate a migration from db/models.py (use: make docker-make-migrations -m "add plan comment")
	@test -n "$(MIGRATION_MSG)" || { echo 'usage: make docker-make-migrations -m "short message"'; exit 2; }
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini revision --autogenerate -m "$(MIGRATION_MSG)"
	@echo "Review the new file in db/migrations/versions, then: make docker-migrate"

docker-migrate: $(ENV_FILE) ## Apply all pending migrations (alembic upgrade head)
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head

docker-migrate-down: $(ENV_FILE) ## Roll back one migration (use: make docker-migrate-down rev=006 to target a revision)
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini downgrade $(or $(rev),-1)

docker-migrate-status: $(ENV_FILE) ## Show the applied revision, pending changes and history
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini current
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini check
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini history

# The entrypoint already waited and migrated; seed only.
docker-seed-db: $(ENV_FILE) ## Re-run both seeders by hand (the app already seeds on first start)
	$(COMPOSE) exec $(APP) python -m db.seed
	$(COMPOSE) exec $(APP) scripts/seed_clickhouse.sh

docker-reinit: $(ENV_FILE) ## Drop the governance schema and the cube, then rebuild and seed both
	$(COMPOSE) exec $(APP) psql -h $$PGHOST -U postgres -d fpa -c "DROP SCHEMA IF EXISTS $(SCHEMA) CASCADE"
	$(COMPOSE) exec $(APP) curl -sS -u default:fpa "http://clickhouse:8123/" --data-binary "DROP DATABASE IF EXISTS $(CUBE)"
	$(COMPOSE) exec $(APP) alembic -c db/alembic.ini upgrade head
	$(COMPOSE) exec $(APP) python -m db.seed
	$(COMPOSE) exec -e RESEED_CUBE=1 $(APP) scripts/seed_clickhouse.sh
