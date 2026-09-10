import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app

TRUNCATE_TABLES = ("audit_event", "plan_version_line", "plan_driver", "plan_driver_proposal", "plan_version")


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(superuser_conn):
    await superuser_conn.execute(f"TRUNCATE {', '.join(TRUNCATE_TABLES)} RESTART IDENTITY CASCADE")
    yield


@pytest_asyncio.fixture
async def client():
    # FastAPI's own lifespan (app_pool startup/shutdown) doesn't run just
    # because we're driving the app in-process via ASGITransport, so it's
    # entered manually here rather than pulling in a lifespan-testing dep.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
