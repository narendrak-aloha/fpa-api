import os

import clickhouse_connect
import pytest

from fpa_be.cube.client import CubeClient

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


@pytest.fixture
def cube(ch_client):
    return CubeClient(client=ch_client)
