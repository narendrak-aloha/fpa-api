"""Temporal worker entrypoint.

Placeholder for Phase 0: proves the worker container can connect to the
Temporal server and stay up. Workflows and activities (PlanRecomputeWorkflow
and friends) are registered here in Phase 8.
"""

import asyncio
import os

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker


@workflow.defn(name="PlaceholderWorkflow")
class PlaceholderWorkflow:
    """Stands in for PlanRecomputeWorkflow until Phase 8 registers the real thing."""

    @workflow.run
    async def run(self) -> str:
        return "not implemented until phase 8"


async def main() -> None:
    target = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "fpa-recompute")

    client = await Client.connect(target, namespace=namespace)
    print(f"connected to temporal at {target}, namespace={namespace}")

    worker = Worker(client, task_queue=task_queue, workflows=[PlaceholderWorkflow])
    print(f"worker listening on task queue '{task_queue}' (PlanRecomputeWorkflow lands in phase 8)")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
