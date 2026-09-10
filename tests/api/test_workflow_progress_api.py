"""The live-progress endpoint the Vue interface polls: it queries a real,
running PlanRecomputeWorkflow through the same app.state.temporal_client
the API serves requests with (not the time-skipping test environment used
for the workflow's own behavioral tests)."""

import uuid

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from fpa_be.workflows.plan_recompute import PartitionRecomputeWorkflow, PlanRecomputeWorkflow
from tests.workflows.test_plan_recompute import _input, _mock_activities

pytestmark = pytest.mark.asyncio


async def test_progress_endpoint_reports_a_running_workflow(client):
    real_client = await Client.connect("localhost:7233", namespace="default")
    activities, _ = _mock_activities(locked=True)
    task_queue = f"tq-{uuid.uuid4()}"
    workflow_id = f"wf-{uuid.uuid4()}"

    async with Worker(
        real_client,
        task_queue=task_queue,
        workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
        activities=activities,
    ):
        handle = await real_client.start_workflow(
            PlanRecomputeWorkflow.run,
            _input(approval_timeout_seconds=30.0),
            id=workflow_id,
            task_queue=task_queue,
        )

        resp = await client.get(f"/workflows/{workflow_id}/progress")
        assert resp.status_code == 200
        body = resp.json()
        assert "phase" in body
        assert "dirty_set_fraction_complete" in body

        await handle.signal(PlanRecomputeWorkflow.reject, args=["planner@example.com", "cleaning up test"])
        await handle.result()


async def test_progress_endpoint_404s_for_unknown_workflow(client):
    resp = await client.get(f"/workflows/does-not-exist-{uuid.uuid4()}/progress")
    assert resp.status_code == 404
