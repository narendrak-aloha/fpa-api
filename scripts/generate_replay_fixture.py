"""One-off generator for tests/workflows/fixtures/plan_recompute_history.json
(Phase 17): runs PlanRecomputeWorkflow's happy path to completion against a
real Temporal server, fetches its committed event history, and writes it out
as the frozen fixture tests/workflows/test_replay.py replays on every CI run.

Not a test itself, and not run automatically -- re-run this manually only
when PlanRecomputeWorkflow's *history shape* deliberately changes (a new
activity, a new signal, etc.); the whole point of the fixture is that it
stays fixed while the workflow code around it evolves, so replay can catch
an accidental determinism break.

Usage: POSTGRES_HOST=localhost POSTGRES_PORT=5431 uv run python scripts/generate_replay_fixture.py
"""

import asyncio
import json
import uuid
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from fpa_be.workflows.plan_recompute import (
    PartitionRecomputeWorkflow,
    PlanRecomputeWorkflow,
)
from tests.workflows.test_plan_recompute import _input, _mock_activities

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "workflows" / "fixtures" / "plan_recompute_history.json"


async def main():
    client = await Client.connect("localhost:7233", namespace="default")
    activities, _ = _mock_activities(locked=True)
    task_queue = f"tq-fixture-gen-{uuid.uuid4()}"
    workflow_id = f"wf-fixture-gen-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
        activities=activities,
    ):
        handle = await client.start_workflow(
            PlanRecomputeWorkflow.run,
            _input(plan_version_id=str(uuid.uuid4()), approval_timeout_seconds=30.0),
            id=workflow_id,
            task_queue=task_queue,
        )
        await handle.signal(PlanRecomputeWorkflow.approve, "cfo@example.com")
        result = await handle.result()
        print("workflow result:", result)

        history = await handle.fetch_history()

    # The replayer derives PartitionRecomputeWorkflow's deterministic child
    # workflow ids from the *parent* workflow_id at replay time -- it must
    # be fed back in exactly as recorded, or replay itself raises a
    # (spurious) NondeterminismError on the child-id mismatch. Raw history
    # JSON alone doesn't carry the workflow_id, so it's persisted alongside
    # it here rather than reconstructed from the events.
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps({"workflow_id": workflow_id, "history": json.loads(history.to_json())}, indent=2))
    print(f"wrote {FIXTURE_PATH} (workflow_id={workflow_id})")


if __name__ == "__main__":
    asyncio.run(main())
