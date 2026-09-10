"""Connection helpers for the three Postgres roles the governance schema
distinguishes: superuser (migrations only), fpa_app (normal API writes),
and fpa_controller (covenant flag / rate field writes -- Postgres, not the
app, is what actually enforces that split).
"""

import os

import asyncpg


def _dsn(user: str, password_env: str, default_password: str) -> str:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "fpa")
    password = os.environ.get(password_env, default_password)
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def app_dsn() -> str:
    return _dsn("fpa_app", "FPA_APP_DB_PASSWORD", "fpa_app_dev")


def controller_dsn() -> str:
    return _dsn("fpa_controller", "FPA_CONTROLLER_DB_PASSWORD", "fpa_controller_dev")


async def app_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(app_dsn())


async def controller_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(controller_dsn())
