"""The live-progress endpoint the Vue interface polls, and the recompute's
approve/reject endpoints: a real, running PlanRecomputeWorkflow reached
through the same app.state.temporal_client the API serves requests with.
Only a controller may resolve a recompute, never the person who requested
it, and only while it is awaiting approval.
"""

import asyncio
import uuid

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from fpa_be.workflows.plan_recompute import PartitionRecomputeWorkflow, PlanRecomputeWorkflow
from tests.workflows.test_plan_recompute import _input, _mock_activities

pytestmark = pytest.mark.asyncio

ALICE = {"x-api-key": "alice-planner-key"}  # planner
BOB = {"x-api-key": "bob-controller-key"}  # controller


async def _parked(client, handle) -> dict:
    for _ in range(100):
        resp = await client.get(f"/workflows/{handle.id}/progress", headers=ALICE)
        if resp.status_code == 200 and resp.json()["phase"] == "awaiting_approval":
            return resp.json()
        await asyncio.sleep(0.1)
    raise AssertionError("workflow never reached awaiting_approval")


async def _start(real_client, task_queue, requested_by):
    return await real_client.start_workflow(
        PlanRecomputeWorkflow.run,
        _input(approval_timeout_seconds=30.0, requested_by=requested_by),
        id=f"wf-{uuid.uuid4()}",
        task_queue=task_queue,
    )


@pytest.fixture
async def worker():
    real_client = await Client.connect("localhost:7233", namespace="default")
    activities, _ = _mock_activities(locked=True)
    task_queue = f"tq-{uuid.uuid4()}"
    async with Worker(
        real_client,
        task_queue=task_queue,
        workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
        activities=activities,
    ):
        yield real_client, task_queue


async def test_progress_requires_an_api_key(client):
    assert (await client.get(f"/workflows/wf-{uuid.uuid4()}/progress")).status_code == 401


async def test_progress_endpoint_reports_a_running_workflow(client, worker):
    real_client, task_queue = worker
    handle = await _start(real_client, task_queue, requested_by="alice")
    body = await _parked(client, handle)
    assert body["dirty_set_fraction_complete"] == 1.0
    assert body["requested_by"] == "alice"
    assert "plan_version_id" in body

    resp = await client.post(f"/workflows/{handle.id}/reject", json={"reason": "cleaning up"}, headers=BOB)
    assert resp.status_code == 200
    assert (await handle.result()).status == "Rejected"


async def test_progress_endpoint_404s_for_unknown_workflow(client):
    resp = await client.get(f"/workflows/does-not-exist-{uuid.uuid4()}/progress", headers=ALICE)
    assert resp.status_code == 404


async def test_a_controller_other_than_the_requester_approves_and_it_is_audited(client, worker, superuser_conn):
    real_client, task_queue = worker
    handle = await _start(real_client, task_queue, requested_by="alice")
    parked = await _parked(client, handle)

    resp = await client.post(f"/workflows/{handle.id}/approve", headers=BOB)
    assert resp.status_code == 200
    assert resp.json()["actor"] == "bob"
    assert (await handle.result()).status == "Published"

    audit = await superuser_conn.fetchrow(
        "SELECT actor, actor_role FROM audit_event WHERE entity_id = $1 AND action = 'recompute_approve'",
        parked["plan_version_id"],
    )
    assert tuple(audit) == ("bob", "controller")


async def test_the_requester_cannot_approve_their_own_recompute(client, worker):
    real_client, task_queue = worker
    handle = await _start(real_client, task_queue, requested_by="bob")
    await _parked(client, handle)

    resp = await client.post(f"/workflows/{handle.id}/approve", headers=BOB)
    assert resp.status_code == 403
    assert (await client.get(f"/workflows/{handle.id}/progress", headers=ALICE)).json()["phase"] == "awaiting_approval"

    await client.post(f"/workflows/{handle.id}/reject", json={"reason": "cleaning up"}, headers=BOB)
    await handle.result()


async def test_a_planner_cannot_resolve_a_recompute(client, worker):
    real_client, task_queue = worker
    handle = await _start(real_client, task_queue, requested_by="carol")
    await _parked(client, handle)

    assert (await client.post(f"/workflows/{handle.id}/approve", headers=ALICE)).status_code == 403
    assert (await client.post(f"/workflows/{handle.id}/reject", json={}, headers=ALICE)).status_code == 403

    await client.post(f"/workflows/{handle.id}/reject", json={"reason": "cleaning up"}, headers=BOB)
    await handle.result()


async def test_a_finished_recompute_cannot_be_approved(client, worker):
    real_client, task_queue = worker
    handle = await _start(real_client, task_queue, requested_by="alice")
    await _parked(client, handle)
    await client.post(f"/workflows/{handle.id}/reject", json={"reason": "no"}, headers=BOB)
    await handle.result()

    resp = await client.post(f"/workflows/{handle.id}/approve", headers=BOB)
    assert resp.status_code == 409
