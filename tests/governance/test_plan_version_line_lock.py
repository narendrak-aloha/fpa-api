"""A Locked plan_version's lines are immutable -- enforced by
fn_plan_version_line_guard, not merely an app-layer check.
"""

import datetime

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

PERIOD = datetime.date(2026, 1, 1)


async def _insert_plan(conn, state="Draft"):
    row = await conn.fetchrow(
        "INSERT INTO plan_version (plan_code, requested_by, state) VALUES ($1, $2, $3) RETURNING id",
        "PV-TEST", "alice", state,
    )
    return row["id"]


async def _insert_line(conn, plan_id, **overrides):
    fields = {
        "plan_version_id": plan_id,
        "scenario_id": "base",
        "revision": 0,
        "company": "US01",
        "account": "41000",
        "period_month": PERIOD,
        "dim_signature_hash": "deadbeef",
        "quantity": 10,
        "unit_price": 100,
        "amount_functional": 1000,
        "driver_derivation_trace": "{}",
        **overrides,
    }
    cols = ", ".join(fields)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(fields)))
    row = await conn.fetchrow(
        f"INSERT INTO plan_version_line ({cols}) VALUES ({placeholders}) RETURNING id",
        *fields.values(),
    )
    return row["id"]


async def test_line_is_editable_while_draft(superuser_conn):
    plan_id = await _insert_plan(superuser_conn, state="Draft")
    line_id = await _insert_line(superuser_conn, plan_id)
    await superuser_conn.execute(
        "UPDATE plan_version_line SET quantity = 20 WHERE id = $1", line_id
    )
    row = await superuser_conn.fetchrow("SELECT quantity FROM plan_version_line WHERE id = $1", line_id)
    assert row["quantity"] == 20


async def test_line_update_rejected_once_locked(superuser_conn):
    plan_id = await _insert_plan(superuser_conn, state="Locked")
    line_id = await _insert_line(superuser_conn, plan_id)
    with pytest.raises(asyncpg.RaiseError):
        await superuser_conn.execute(
            "UPDATE plan_version_line SET quantity = 20 WHERE id = $1", line_id
        )


async def test_line_delete_rejected_once_locked(superuser_conn):
    plan_id = await _insert_plan(superuser_conn, state="Locked")
    line_id = await _insert_line(superuser_conn, plan_id)
    with pytest.raises(asyncpg.RaiseError):
        await superuser_conn.execute("DELETE FROM plan_version_line WHERE id = $1", line_id)
