"""The plan_version lifecycle endpoints: who acts comes from the API key,
which role may make which transition comes from state_transition, and the
app-layer refusals (self-approval, covenant breach, stale write) sit in
front of the DB-level guarantees already covered by tests/governance/.
"""

import asyncio

import pytest
from temporalio.client import Client

pytestmark = pytest.mark.asyncio

ALICE = {"x-api-key": "alice-planner-key"}  # planner
BOB = {"x-api-key": "bob-controller-key"}  # controller
CAROL = {"x-api-key": "carol-controller-key"}  # controller


async def _create(client, headers=ALICE):
    resp = await client.post("/plan-versions", json={"plan_code": "PV-TEST"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _to_locked(client):
    plan = await _create(client)
    for action, who in (("submit", ALICE), ("approve", BOB), ("lock", BOB)):
        resp = await client.post(f"/plan-versions/{plan['id']}/{action}", headers=who)
        assert resp.status_code == 200, resp.text
    return resp.json()


async def test_requests_without_an_api_key_are_rejected(client):
    assert (await client.post("/plan-versions", json={"plan_code": "PV-TEST"})).status_code == 401


async def test_requester_comes_from_the_api_key_not_the_body(client):
    resp = await client.post("/plan-versions", json={"plan_code": "PV-TEST", "requested_by": "mallory"}, headers=ALICE)
    assert resp.status_code == 201
    assert resp.json()["requested_by"] == "alice"
    assert resp.json()["state"] == "Draft"


async def test_submit_then_approve_by_a_controller(client):
    plan = await _create(client)
    resp = await client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE)
    assert resp.status_code == 200
    assert resp.json()["state"] == "In-Review"

    resp = await client.post(f"/plan-versions/{plan['id']}/approve", headers=BOB)
    assert resp.status_code == 200
    assert resp.json()["state"] == "Approved"
    assert resp.json()["approved_by"] == "bob"


async def test_a_planner_cannot_approve(client):
    plan = await _create(client)
    await client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE)
    resp = await client.post(f"/plan-versions/{plan['id']}/approve", headers=ALICE)
    assert resp.status_code == 403
    assert "role" in resp.json()["detail"]


async def test_self_approval_is_rejected_even_for_a_controller(client):
    plan = await _create(client, headers=BOB)
    await client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE)

    resp = await client.post(f"/plan-versions/{plan['id']}/approve", headers=BOB)
    assert resp.status_code == 403
    assert "self-approved" in resp.json()["detail"]
    assert (await client.get(f"/plan-versions/{plan['id']}", headers=ALICE)).json()["state"] == "In-Review"

    resp = await client.post(f"/plan-versions/{plan['id']}/approve", headers=CAROL)
    assert resp.status_code == 200


async def test_covenant_breach_blocks_approval_regardless_of_actor(client, controller_conn):
    plan = await _create(client)
    await client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE)
    await controller_conn.execute("UPDATE plan_version SET covenant_breach = true WHERE id = $1", plan["id"])

    resp = await client.post(f"/plan-versions/{plan['id']}/approve", headers=BOB)
    assert resp.status_code == 403


async def test_illegal_transition_is_rejected(client):
    plan = await _create(client)
    # Draft -> Approved is not a legal transition; must go through In-Review.
    resp = await client.post(f"/plan-versions/{plan['id']}/approve", headers=BOB)
    assert resp.status_code == 409


async def test_reject_returns_plan_to_draft(client):
    plan = await _create(client)
    await client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE)

    resp = await client.post(f"/plan-versions/{plan['id']}/reject", headers=BOB)
    assert resp.status_code == 200
    assert resp.json()["state"] == "Draft"


async def test_reject_from_draft_is_illegal(client):
    plan = await _create(client)
    resp = await client.post(f"/plan-versions/{plan['id']}/reject", headers=BOB)
    assert resp.status_code == 409


async def test_concurrent_writers_one_wins_one_is_told_it_lost(client):
    plan = await _create(client)
    first, second = await asyncio.gather(
        client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE),
        client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]


async def test_full_lifecycle_to_locked_starts_no_recompute(client):
    locked = await _to_locked(client)
    assert locked["state"] == "Locked"
    assert "workflow_id" not in locked


async def test_lifecycle_actions_are_audited_with_the_authenticated_actor(client, superuser_conn):
    plan = await _create(client)
    await client.post(f"/plan-versions/{plan['id']}/submit", headers=ALICE)
    await client.post(f"/plan-versions/{plan['id']}/approve", headers=BOB)

    rows = await superuser_conn.fetch(
        "SELECT action, actor, actor_role FROM audit_event WHERE entity_id = $1 ORDER BY id", plan["id"]
    )
    assert [tuple(r) for r in rows] == [
        ("create", "alice", "planner"),
        ("submit", "alice", "planner"),
        ("approve", "bob", "controller"),
    ]


class TestRecomputeRequest:
    async def test_only_a_locked_version_can_be_recomputed(self, client):
        plan = await _create(client)
        resp = await client.post(
            f"/plan-versions/{plan['id']}/recompute",
            json={"driver_shocks": [{"name": "utilisation", "value": 0.74}]},
            headers=ALICE,
        )
        assert resp.status_code == 409

    async def test_an_unknown_driver_is_refused(self, client):
        locked = await _to_locked(client)
        resp = await client.post(
            f"/plan-versions/{locked['id']}/recompute",
            json={"driver_shocks": [{"name": "no_such_driver", "value": 1.0}]},
            headers=ALICE,
        )
        assert resp.status_code == 422

    async def test_at_least_one_shock_is_required(self, client):
        locked = await _to_locked(client)
        resp = await client.post(f"/plan-versions/{locked['id']}/recompute", json={"driver_shocks": []}, headers=ALICE)
        assert resp.status_code == 422

    async def test_a_shock_on_a_locked_version_starts_its_recompute_and_is_audited(self, client, superuser_conn):
        locked = await _to_locked(client)
        await superuser_conn.execute(
            "INSERT INTO plan_driver (name, formula, effective_date, created_by) "
            "VALUES ('utilisation', '0.75', '2026-01-01', 'alice')"
        )
        resp = await client.post(
            f"/plan-versions/{locked['id']}/recompute",
            json={"driver_shocks": [{"name": "utilisation", "value": 0.74}]},
            headers=ALICE,
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        try:
            assert body["workflow_id"] == f"plan-recompute-{locked['id']}-r{locked['revision']}"
            assert body["delivery"] == "started"
            audit = await superuser_conn.fetchrow(
                "SELECT actor, payload FROM audit_event WHERE entity_id = $1 AND action = 'recompute_requested'",
                locked["id"],
            )
            assert audit["actor"] == "alice"
        finally:
            temporal = await Client.connect("localhost:7233", namespace="default")
            await temporal.get_workflow_handle(body["workflow_id"]).terminate(reason="test cleanup")
