"""Fixtures for tests that attack the governance schema directly over the
wire, the same way a `psql` session would -- these are integration tests
against a live Postgres, not unit tests, and are skipped if none is up.

Connection fixtures (superuser_conn, app_conn, controller_conn) live in
tests/conftest.py and are auto-discovered by pytest from here.
"""

import pytest_asyncio

TRUNCATE_TABLES = (
    "audit_event",
    "llm_disclosure_log",
    "variance_report_line",
    "variance_report",
    "plan_fx_rate",
    "plan_version_line",
    "plan_driver",
    "plan_driver_proposal",
    "plan_version",
)


@pytest_asyncio.fixture(autouse=True)
async def clean_governance_tables(superuser_conn):
    async with superuser_conn.transaction():
        await superuser_conn.execute(f"TRUNCATE {', '.join(TRUNCATE_TABLES)} RESTART IDENTITY CASCADE")
    yield
