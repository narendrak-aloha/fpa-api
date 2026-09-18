# Application/tooling image: FastAPI service, Alembic migrations and both seeders.
FROM python:3.12-slim

# Venv outside /app: compose bind-mounts the repo over /app.
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl postgresql-client \
 && rm -rf /var/lib/apt/lists/*

# Pinned: `latest` would make builds drift.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

# From uv.lock, so every build resolves identically. Source arrives via the bind mount.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra dev

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# --reload pairs with the bind mount.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
