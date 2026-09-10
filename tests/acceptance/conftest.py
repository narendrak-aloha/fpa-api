import os

import clickhouse_connect
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("CLICKHOUSE_HOST", "localhost")
os.environ.setdefault("CLICKHOUSE_PORT", "8123")

from main import app

TRUNCATE_TABLES = ("audit_event", "plan_version_line", "plan_driver", "plan_version")


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(superuser_conn):
    """Step 1/2 of the evaluator sequence ("start clean stack, seed") is a
    docker-compose + seed_fpa.py concern outside pytest's reach; truncating
    the governance tables here is the part of "clean" this suite can and
    should assert for itself before walking the narrative.
    """
    await superuser_conn.execute(f"TRUNCATE {', '.join(TRUNCATE_TABLES)} RESTART IDENTITY CASCADE")
    yield


@pytest_asyncio.fixture
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture(scope="module")
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
