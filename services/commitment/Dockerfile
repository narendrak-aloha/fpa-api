FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY . .
RUN uv pip install --system .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
