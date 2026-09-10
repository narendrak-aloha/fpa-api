"""ClickHouse client fixture for compiler tests -- these execute compiled
SQL against the live, already-seeded ClickHouse instance (docker-compose's
`clickhouse` service, host port 8123). Phase 5 owns the real read-path
wrapper; this fixture is test-only and deliberately doesn't reuse it.
"""

import os

import clickhouse_connect
import pytest

os.environ.setdefault("CLICKHOUSE_HOST", "localhost")
os.environ.setdefault("CLICKHOUSE_PORT", "8123")


@pytest.fixture(scope="session")
def ch_client():
    client = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ["CLICKHOUSE_PORT"]),
        username="default",
        password="fpa",
        database="fpa_cube",
    )
    try:
        yield client
    finally:
        client.close()
