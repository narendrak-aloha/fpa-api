"""Temporal worker entrypoint: registers PlanRecomputeWorkflow, its
partition-fan-out child workflow, and every activity they call."""

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from fpa_be.workflows import activities as acts
from fpa_be.workflows.plan_recompute import PartitionRecomputeWorkflow, PlanRecomputeWorkflow

WORKFLOWS = [PlanRecomputeWorkflow, PartitionRecomputeWorkflow]
ACTIVITIES = [
    acts.check_plan_locked,
    acts.snapshot_drivers,
    acts.resolve_dirty_set_activity,
    acts.evaluate_partition,
    acts.write_plan_lines,
    acts.publish_to_cube,
    acts.rollback_cube_publish,
    acts.commit_to_treasury,
    acts.compensate_commitment,
    acts.compute_variance,
]


async def main() -> None:
    target = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "fpa-recompute")

    client = await Client.connect(target, namespace=namespace)
    print(f"connected to temporal at {target}, namespace={namespace}")

    worker = Worker(client, task_queue=task_queue, workflows=WORKFLOWS, activities=ACTIVITIES)
    print(f"worker listening on task queue '{task_queue}' ({len(WORKFLOWS)} workflows, {len(ACTIVITIES)} activities)")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
