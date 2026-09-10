"""plan_driver.rate_value is controller-only, mirroring plan_version's
covenant_breach split -- same column-level-grant-after-full-revoke pattern.
"""

import datetime

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


async def _insert_driver(conn, **overrides):
    fields = {
        "name": "bill_rate",
        "formula": "150",
        "effective_date": datetime.date(2026, 1, 1),
        "created_by": "alice",
        **overrides,
    }
    cols = ", ".join(fields)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(fields)))
    row = await conn.fetchrow(
        f"INSERT INTO plan_driver ({cols}) VALUES ({placeholders}) RETURNING id",
        *fields.values(),
    )
    return row["id"]


async def test_fpa_app_cannot_write_rate_value(superuser_conn, app_conn):
    driver_id = await _insert_driver(superuser_conn, is_rate_driver=True)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(
            "UPDATE plan_driver SET rate_value = 200 WHERE id = $1", driver_id
        )


async def test_fpa_app_can_write_formula(superuser_conn, app_conn):
    driver_id = await _insert_driver(superuser_conn)
    await app_conn.execute(
        "UPDATE plan_driver SET formula = '175' WHERE id = $1", driver_id
    )
    row = await superuser_conn.fetchrow("SELECT formula FROM plan_driver WHERE id = $1", driver_id)
    assert row["formula"] == "175"


async def test_fpa_controller_can_write_rate_value(superuser_conn, controller_conn):
    driver_id = await _insert_driver(superuser_conn, is_rate_driver=True)
    await controller_conn.execute(
        "UPDATE plan_driver SET rate_value = 200 WHERE id = $1", driver_id
    )
    row = await superuser_conn.fetchrow("SELECT rate_value FROM plan_driver WHERE id = $1", driver_id)
    assert row["rate_value"] == 200


async def test_fpa_controller_cannot_write_formula(superuser_conn, controller_conn):
    driver_id = await _insert_driver(superuser_conn)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await controller_conn.execute(
            "UPDATE plan_driver SET formula = '175' WHERE id = $1", driver_id
        )
