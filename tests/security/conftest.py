import os

import clickhouse_connect
import pytest

os.environ.setdefault("CLICKHOUSE_HOST", "localhost")
os.environ.setdefault("CLICKHOUSE_PORT", "8123")

from tests.agents.conftest import make_run_context  # noqa: E402


@pytest.fixture
def run_context():
    return make_run_context()


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
