# FP&A Query

## Run

Needs Docker.

```bash
make env                 # creates .env; add ANTHROPIC_API_KEY or GOOGLE_API_KEY here if you have one
make docker-local-run    # builds and starts everything
```

Without make:

```bash
cp .env.example .env
docker compose --env-file .env -f docker/docker-compose.yml up --build
```

The first start takes a few minutes while sample data loads. It's ready when
the log shows `==> API on http://localhost:8000`. Stop it with `Ctrl+C`.

## Use the frontend

Open **http://localhost:8000**.

- **Quick test:** click `SELECT services_revenue BY company FOR PERIOD 2026-Q2`.
- **Plain-English questions:** pick a model and ask, e.g. *"What was services
  revenue by practice in Q2 2026?"* (30–60 s).
  - **Claude (subscription):** no API key, uses your Claude Code login (Linux).
  - **Claude API key** / **Gemini:** needs the key in `.env`.

## More detail

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design, data flow, API
- [db/README.md](db/README.md): Postgres store and migrations
- [data/README.md](data/README.md): assignment brief and sample data
- [src/fpa_project/dsl/README.md](src/fpa_project/dsl/README.md): query language compiler
- [src/fpa_project/agent_team/README.md](src/fpa_project/agent_team/README.md): AI agent team
