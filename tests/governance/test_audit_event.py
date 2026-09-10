"""audit_event is append-only and hash-chained. Tamper detection is tested
by disabling the append-only trigger just long enough to simulate the kind
of direct-row edit the trigger exists to stop, then confirming
fn_verify_audit_chain() catches it.
"""

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


async def _insert_event(conn, **overrides):
    fields = {
        "entity_type": "plan_version",
        "entity_id": "PV-TEST",
        "action": "create",
        "actor": "alice",
        "actor_role": "planner",
        "payload": "{}",
        **overrides,
    }
    cols = ", ".join(fields)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(fields)))
    row = await conn.fetchrow(
        f"INSERT INTO audit_event ({cols}) VALUES ({placeholders}) RETURNING id, prev_hash, hash",
        *fields.values(),
    )
    return row


async def test_first_event_chains_from_genesis_hash(superuser_conn):
    row = await _insert_event(superuser_conn)
    assert row["prev_hash"] == "0" * 64
    assert row["hash"] is not None


async def test_second_event_links_to_first(superuser_conn):
    first = await _insert_event(superuser_conn)
    second = await _insert_event(superuser_conn, action="update")
    assert second["prev_hash"] == first["hash"]


async def test_update_is_rejected(superuser_conn):
    row = await _insert_event(superuser_conn)
    with pytest.raises(asyncpg.RaiseError):
        await superuser_conn.execute("UPDATE audit_event SET action = 'update' WHERE id = $1", row["id"])


async def test_delete_is_rejected(superuser_conn):
    row = await _insert_event(superuser_conn)
    with pytest.raises(asyncpg.RaiseError):
        await superuser_conn.execute("DELETE FROM audit_event WHERE id = $1", row["id"])


async def test_verify_chain_is_clean_on_untampered_log(superuser_conn):
    await _insert_event(superuser_conn)
    await _insert_event(superuser_conn, action="update")
    await _insert_event(superuser_conn, action="approve")
    bad_rows = await superuser_conn.fetch("SELECT * FROM fn_verify_audit_chain()")
    assert bad_rows == []


async def test_verify_chain_flags_tampered_row(superuser_conn):
    await _insert_event(superuser_conn)
    victim = await _insert_event(superuser_conn, action="update")
    await _insert_event(superuser_conn, action="approve")

    async with superuser_conn.transaction():
        await superuser_conn.execute("ALTER TABLE audit_event DISABLE TRIGGER trg_audit_event_append_only")
        await superuser_conn.execute(
            "UPDATE audit_event SET payload = '{\"tampered\": true}' WHERE id = $1", victim["id"]
        )
        await superuser_conn.execute("ALTER TABLE audit_event ENABLE TRIGGER trg_audit_event_append_only")

    bad_rows = await superuser_conn.fetch("SELECT bad_id, reason FROM fn_verify_audit_chain()")
    bad_ids = {row["bad_id"] for row in bad_rows}
    assert victim["id"] in bad_ids
