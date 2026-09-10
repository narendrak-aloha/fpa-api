"""variance_report can only be Closed by a human -- an agent can advance it
as far as Investigating, never Closed, enforced by a CHECK constraint
rather than trusted to the app/agent layer.
"""

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


async def _insert_plan(conn):
    row = await conn.fetchrow(
        "INSERT INTO plan_version (plan_code, requested_by) VALUES ($1, $2) RETURNING id",
        "PV-TEST", "alice",
    )
    return row["id"]


async def _insert_report(conn, plan_id, **overrides):
    fields = {
        "plan_version_id": plan_id,
        "cut_label": "Poland Q2",
        "dimension_filter": "{}",
        "vintage": 1,
        "created_by": "alice",
        **overrides,
    }
    cols = ", ".join(fields)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(fields)))
    row = await conn.fetchrow(
        f"INSERT INTO variance_report ({cols}) VALUES ({placeholders}) RETURNING id",
        *fields.values(),
    )
    return row["id"]


async def test_agent_cannot_insert_a_closed_report(superuser_conn):
    plan_id = await _insert_plan(superuser_conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_report(superuser_conn, plan_id, state="Closed", advanced_by="agent")


async def test_human_can_insert_a_closed_report(superuser_conn):
    plan_id = await _insert_plan(superuser_conn)
    report_id = await _insert_report(superuser_conn, plan_id, state="Closed", advanced_by="human")
    assert report_id is not None


async def test_agent_can_advance_to_investigating(superuser_conn):
    plan_id = await _insert_plan(superuser_conn)
    report_id = await _insert_report(superuser_conn, plan_id, state="Investigating", advanced_by="agent")
    assert report_id is not None


async def test_agent_cannot_close_via_update(superuser_conn):
    plan_id = await _insert_plan(superuser_conn)
    report_id = await _insert_report(superuser_conn, plan_id, state="Investigating", advanced_by="agent")
    with pytest.raises(asyncpg.CheckViolationError):
        await superuser_conn.execute(
            "UPDATE variance_report SET state = 'Closed' WHERE id = $1", report_id
        )


async def test_human_close_via_update_succeeds(superuser_conn):
    plan_id = await _insert_plan(superuser_conn)
    report_id = await _insert_report(superuser_conn, plan_id, state="Investigating", advanced_by="agent")
    await superuser_conn.execute(
        "UPDATE variance_report SET state = 'Closed', advanced_by = 'human' WHERE id = $1", report_id
    )
    row = await superuser_conn.fetchrow("SELECT state FROM variance_report WHERE id = $1", report_id)
    assert row["state"] == "Closed"
