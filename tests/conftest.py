"""Shared Postgres connection fixtures for integration tests that run
against a live database (governance attacks, API lifecycle tests).
"""

import os

import asyncpg
import pytest_asyncio

# Tests run on the host against the docker-compose postgres service, which
# publishes on 5431. CI running *inside* the compose network would set these
# explicitly (POSTGRES_HOST=postgres, POSTGRES_PORT=5432) before pytest runs.
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5431")

from fpa_be.db import app_dsn, controller_dsn

SUPERUSER_DSN = f"postgresql://postgres:fpa@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/fpa"


@pytest_asyncio.fixture
async def superuser_conn():
    conn = await asyncpg.connect(SUPERUSER_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app_conn():
    conn = await asyncpg.connect(app_dsn())
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def controller_conn():
    conn = await asyncpg.connect(controller_dsn())
    try:
        yield conn
    finally:
        await conn.close()
