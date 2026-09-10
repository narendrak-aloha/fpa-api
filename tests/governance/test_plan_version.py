"""Attacks the plan_version governance rules the same way the manual psql
session did: self-approval, covenant-flag column privilege, and the
optimistic-concurrency revision bump.
"""

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


async def _insert_plan(conn, **overrides):
    fields = {
        "plan_code": "PV-TEST",
        "requested_by": "alice",
        **overrides,
    }
    cols = ", ".join(fields)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(fields)))
    row = await conn.fetchrow(
        f"INSERT INTO plan_version ({cols}) VALUES ({placeholders}) RETURNING id, revision",
        *fields.values(),
    )
    return row["id"], row["revision"]


async def test_self_approval_is_rejected_by_check_constraint(superuser_conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_plan(superuser_conn, requested_by="alice", approved_by="alice")


async def test_distinct_approver_is_allowed(superuser_conn):
    plan_id, _ = await _insert_plan(superuser_conn, requested_by="alice", approved_by="bob")
    assert plan_id is not None


async def test_self_approval_rejected_on_update_too(superuser_conn):
    plan_id, _ = await _insert_plan(superuser_conn, requested_by="alice")
    with pytest.raises(asyncpg.CheckViolationError):
        await superuser_conn.execute(
            "UPDATE plan_version SET approved_by = requested_by WHERE id = $1", plan_id
        )


async def test_fpa_app_cannot_write_covenant_breach(superuser_conn, app_conn):
    plan_id, _ = await _insert_plan(superuser_conn)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(
            "UPDATE plan_version SET covenant_breach = true WHERE id = $1", plan_id
        )


async def test_fpa_app_can_still_write_its_own_columns(superuser_conn, app_conn):
    plan_id, _ = await _insert_plan(superuser_conn)
    await app_conn.execute(
        "UPDATE plan_version SET state = 'In-Review' WHERE id = $1", plan_id
    )
    row = await superuser_conn.fetchrow("SELECT state FROM plan_version WHERE id = $1", plan_id)
    assert row["state"] == "In-Review"


async def test_fpa_controller_can_write_covenant_breach(superuser_conn, controller_conn):
    plan_id, _ = await _insert_plan(superuser_conn)
    await controller_conn.execute(
        "UPDATE plan_version SET covenant_breach = true WHERE id = $1", plan_id
    )
    row = await superuser_conn.fetchrow("SELECT covenant_breach FROM plan_version WHERE id = $1", plan_id)
    assert row["covenant_breach"] is True


async def test_fpa_controller_cannot_write_app_owned_columns(superuser_conn, controller_conn):
    plan_id, _ = await _insert_plan(superuser_conn)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await controller_conn.execute(
            "UPDATE plan_version SET state = 'In-Review' WHERE id = $1", plan_id
        )


async def test_revision_auto_bumps_on_update(superuser_conn):
    plan_id, revision = await _insert_plan(superuser_conn)
    assert revision == 0
    row = await superuser_conn.fetchrow(
        "UPDATE plan_version SET state = 'In-Review' WHERE id = $1 RETURNING revision", plan_id
    )
    assert row["revision"] == 1


async def test_stale_revision_write_is_a_no_op(superuser_conn):
    plan_id, revision = await _insert_plan(superuser_conn)
    await superuser_conn.execute("UPDATE plan_version SET state = 'In-Review' WHERE id = $1", plan_id)
    # A writer that read the stale revision=0 and gates its UPDATE on it
    # affects zero rows instead of silently overwriting the newer state.
    result = await superuser_conn.execute(
        "UPDATE plan_version SET state = 'Draft' WHERE id = $1 AND revision = $2", plan_id, revision
    )
    assert result == "UPDATE 0"
    row = await superuser_conn.fetchrow("SELECT state FROM plan_version WHERE id = $1", plan_id)
    assert row["state"] == "In-Review"
