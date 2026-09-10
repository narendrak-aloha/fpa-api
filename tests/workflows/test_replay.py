"""Phase 17 -- the mechanical proof of workflow determinism the assignment
asks for: replaying a *committed* PlanRecomputeWorkflow event history
(tests/workflows/fixtures/plan_recompute_history.json, generated once by
scripts/generate_replay_fixture.py against a real Temporal server) through
today's workflow code via Temporal's `Replayer`.

Replay re-executes only the *workflow* function against the recorded
history -- no activities run, no Temporal server is needed here at all --
and fails if today's code would have made a different scheduling decision
at any point in that history than what was actually recorded. That's the
regression this test exists to catch: a future edit to PlanRecomputeWorkflow
(reordering awaits, changing a signal/timer, adding a non-deterministic
call) that would silently break replay of every already-running production
workflow instance, something duplicate-run/worker-kill tests (already
covered in tests/workflows/test_plan_recompute.py and
tests/acceptance/test_full_acceptance_walkthrough.py) can't catch because
they only ever exercise *current* code against itself.
"""

import json
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from fpa_be.workflows.plan_recompute import (
    PartitionRecomputeWorkflow,
    PlanRecomputeWorkflow,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "plan_recompute_history.json"


@pytest.fixture(scope="module")
def committed_history() -> WorkflowHistory:
    fixture = json.loads(FIXTURE_PATH.read_text())
    # The parent workflow_id must be fed back in exactly as recorded: the
    # replayer derives PartitionRecomputeWorkflow's deterministic child
    # workflow ids from it, so any other value raises a spurious
    # NondeterminismError on the child-id mismatch rather than validating
    # the workflow's own logic.
    return WorkflowHistory.from_json(fixture["workflow_id"], fixture["history"])


class TestReplayAgainstCommittedHistory:
    async def test_replays_without_a_nondeterminism_error(self, committed_history):
        replayer = Replayer(workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow])
        # raise_on_replay_failure=True (the default): any nondeterminism
        # detected while re-executing the workflow against this history
        # raises here, failing the test.
        await replayer.replay_workflow(committed_history)

    async def test_fixture_captures_the_full_published_happy_path(self, committed_history):
        # Sanity on the fixture itself: it must actually cover the
        # signal-then-publish path (not e.g. a workflow that timed out
        # immediately), or a passing replay here would be proving nothing.
        event_types = {EventType.Name(event.event_type) for event in committed_history.events}
        assert "EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED" in event_types
        assert "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" in event_types
