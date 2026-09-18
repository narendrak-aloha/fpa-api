# Application/tooling image: FastAPI service, Alembic migrations and both seeders.
FROM python:3.12-slim

# The venv lives outside /app because compose bind-mounts the repo over /app at runtime.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHON=python3 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl postgresql-client \
 && rm -rf /var/lib/apt/lists/*

# uv resolves and installs far faster than pip. Pinned: `latest` would make builds drift.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

# Dependencies come from uv.lock, so every build resolves to identical versions.
# --no-install-project: the source arrives via the bind mount, not the image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra dev

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
