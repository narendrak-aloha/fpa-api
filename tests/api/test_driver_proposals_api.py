"""An agent's proposed assumption is a Draft until a controller who is not
the proposer resolves it; approval is the only thing that writes the live
plan_driver, and it lands in the audit chain."""

import pytest

pytestmark = pytest.mark.asyncio

ALICE = {"x-api-key": "alice-planner-key"}  # planner
BOB = {"x-api-key": "bob-controller-key"}  # controller


async def _proposal(conn, proposed_by="pl-planner", formula="0.74"):
    return await conn.fetchval(
        "INSERT INTO plan_driver_proposal (name, formula, effective_date, proposed_by) "
        "VALUES ('utilisation', $1, '2026-01-01', $2) RETURNING id",
        formula,
        proposed_by,
    )


async def test_listing_requires_an_api_key(client):
    assert (await client.get("/driver-proposals")).status_code == 401


async def test_a_controller_approves_and_only_then_the_driver_is_live(client, app_conn, superuser_conn):
    proposal_id = await _proposal(app_conn)
    assert await superuser_conn.fetchval("SELECT count(*) FROM plan_driver WHERE name = 'utilisation'") == 0

    resp = await client.post(f"/driver-proposals/{proposal_id}/approve", headers=BOB)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "Approved"
    assert resp.json()["resolved_by"] == "bob"

    driver = await superuser_conn.fetchrow("SELECT formula, created_by FROM plan_driver WHERE name = 'utilisation'")
    assert tuple(driver) == ("0.74", "pl-planner")
    audit = await superuser_conn.fetchrow(
        "SELECT actor FROM audit_event WHERE entity_type = 'plan_driver_proposal' AND action = 'approve'"
    )
    assert audit["actor"] == "bob"


async def test_the_proposer_cannot_approve_their_own_proposal(client, app_conn):
    proposal_id = await _proposal(app_conn, proposed_by="bob")
    resp = await client.post(f"/driver-proposals/{proposal_id}/approve", headers=BOB)
    assert resp.status_code == 403


async def test_a_planner_cannot_resolve_a_proposal(client, app_conn):
    proposal_id = await _proposal(app_conn)
    assert (await client.post(f"/driver-proposals/{proposal_id}/approve", headers=ALICE)).status_code == 403


async def test_a_resolved_proposal_cannot_be_resolved_again(client, app_conn):
    proposal_id = await _proposal(app_conn)
    await client.post(f"/driver-proposals/{proposal_id}/reject", headers=BOB)
    assert (await client.post(f"/driver-proposals/{proposal_id}/approve", headers=BOB)).status_code == 409


async def test_rejection_leaves_the_live_driver_untouched(client, app_conn, superuser_conn):
    proposal_id = await _proposal(app_conn)
    resp = await client.post(f"/driver-proposals/{proposal_id}/reject", headers=BOB)
    assert resp.json()["state"] == "Rejected"
    assert await superuser_conn.fetchval("SELECT count(*) FROM plan_driver WHERE name = 'utilisation'") == 0


async def test_a_malformed_formula_cannot_be_approved(client, app_conn):
    proposal_id = await _proposal(app_conn, formula="heads * (")
    assert (await client.post(f"/driver-proposals/{proposal_id}/approve", headers=BOB)).status_code == 422
