"""Exercises the plan_version lifecycle endpoints, including the two app-
layer refusals (self-approval, covenant breach) that sit in front of the
DB-level guarantees already covered by tests/governance/.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def _create(client, requested_by="alice"):
    resp = await client.post(
        "/plan-versions", json={"plan_code": "PV-TEST", "requested_by": requested_by}
    )
    assert resp.status_code == 201
    return resp.json()


async def test_create_plan_version(client):
    body = await _create(client)
    assert body["state"] == "Draft"
    assert body["requested_by"] == "alice"


async def test_submit_then_approve_by_a_different_actor(client):
    plan = await _create(client, requested_by="alice")
    resp = await client.post(f"/plan-versions/{plan['id']}/submit", json={"actor": "alice"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "In-Review"

    resp = await client.post(f"/plan-versions/{plan['id']}/approve", json={"actor": "bob"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "Approved"
    assert body["approved_by"] == "bob"


async def test_self_approval_is_rejected(client):
    plan = await _create(client, requested_by="alice")
    await client.post(f"/plan-versions/{plan['id']}/submit", json={"actor": "alice"})

    resp = await client.post(f"/plan-versions/{plan['id']}/approve", json={"actor": "alice"})
    assert resp.status_code == 403

    fetched = await client.get(f"/plan-versions/{plan['id']}")
    assert fetched.json()["state"] == "In-Review"


async def test_covenant_breach_blocks_approval_regardless_of_actor(client, controller_conn):
    plan = await _create(client, requested_by="alice")
    await client.post(f"/plan-versions/{plan['id']}/submit", json={"actor": "alice"})
    await controller_conn.execute(
        "UPDATE plan_version SET covenant_breach = true WHERE id = $1", plan["id"]
    )

    resp = await client.post(f"/plan-versions/{plan['id']}/approve", json={"actor": "bob"})
    assert resp.status_code == 403


async def test_illegal_transition_is_rejected(client):
    plan = await _create(client, requested_by="alice")
    # Draft -> Approved is not a legal transition; must go through In-Review.
    resp = await client.post(f"/plan-versions/{plan['id']}/approve", json={"actor": "bob"})
    assert resp.status_code == 409


async def test_reject_returns_plan_to_draft(client):
    plan = await _create(client, requested_by="alice")
    await client.post(f"/plan-versions/{plan['id']}/submit", json={"actor": "alice"})

    resp = await client.post(f"/plan-versions/{plan['id']}/reject", json={"actor": "bob"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "Draft"


async def test_reject_from_draft_is_illegal(client):
    plan = await _create(client, requested_by="alice")
    resp = await client.post(f"/plan-versions/{plan['id']}/reject", json={"actor": "bob"})
    assert resp.status_code == 409


async def test_full_lifecycle_to_locked(client):
    plan = await _create(client, requested_by="alice")
    await client.post(f"/plan-versions/{plan['id']}/submit", json={"actor": "alice"})
    await client.post(f"/plan-versions/{plan['id']}/approve", json={"actor": "bob"})
    resp = await client.post(f"/plan-versions/{plan['id']}/lock", json={"actor": "bob"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "Locked"


async def test_lifecycle_actions_are_audited(client, superuser_conn):
    plan = await _create(client, requested_by="alice")
    await client.post(f"/plan-versions/{plan['id']}/submit", json={"actor": "alice"})
    await client.post(f"/plan-versions/{plan['id']}/approve", json={"actor": "bob"})

    rows = await superuser_conn.fetch(
        "SELECT action FROM audit_event WHERE entity_id = $1 ORDER BY id", plan["id"]
    )
    actions = [row["action"] for row in rows]
    assert actions == ["create", "submit", "approve"]
