"""PlanRecomputeWorkflow behavioral contract tests, run against Temporal's
time-skipping test environment with every activity mocked out (the
activities themselves -- real Postgres/ClickHouse/HTTP calls -- are each
exercised on their own; these tests are about the workflow's orchestration
logic: HITL parking, timeout expiry, second-shock rejection, and the
publish-then-commit-fails compensation saga).
"""

import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fpa_be.workflows import activities as acts
from fpa_be.workflows.plan_recompute import (
    DriverShock,
    PartitionRecomputeWorkflow,
    PlanRecomputeInput,
    PlanRecomputeWorkflow,
)

_DRIVERS = {
    "bill_rate": acts.DriverSnapshot(
        name="bill_rate", formula="150", is_rate_driver=True, rate_value=150.0, effective_date="2026-01-01"
    ),
    "utilisation": acts.DriverSnapshot(
        name="utilisation", formula="0.75", is_rate_driver=True, rate_value=0.75, effective_date="2026-01-01"
    ),
    "revenue_forecast": acts.DriverSnapshot(
        name="revenue_forecast",
        formula="utilisation * bill_rate",
        is_rate_driver=False,
        rate_value=None,
        effective_date="2026-01-01",
    ),
}


def _mock_activities(*, locked: bool = True, commit_should_fail: bool = False):
    calls = {"publish": 0, "rollback": 0, "commit": 0}

    @activity.defn(name="check_plan_locked")
    async def check_plan_locked(plan_version_id: str) -> bool:
        return locked

    @activity.defn(name="snapshot_drivers")
    async def snapshot_drivers(input: acts.SnapshotDriversInput) -> acts.SnapshotDriversResult:
        return acts.SnapshotDriversResult(drivers=dict(_DRIVERS))

    @activity.defn(name="resolve_dirty_set_activity")
    async def resolve_dirty_set_activity(input: acts.ResolveDirtySetInput) -> list[str]:
        from fpa_be.dsl import parse_expr, resolve_dirty_set

        parsed = {n: parse_expr(f) for n, f in input.formulas.items()}
        return resolve_dirty_set(parsed, set(input.shocked))

    @activity.defn(name="evaluate_partition")
    async def evaluate_partition(input: acts.EvaluatePartitionInput) -> acts.EvaluatePartitionResult:
        from fpa_be.dsl import eval_expr, parse_expr

        bindings = dict(input.bindings)
        evaluated = []
        for name in input.driver_names:
            value = eval_expr(parse_expr(input.formulas[name]), bindings)
            bindings[name] = value
            evaluated.append(
                acts.EvaluatedDriver(name=name, value=value, formula=input.formulas[name], inputs=dict(bindings))
            )
        return acts.EvaluatePartitionResult(evaluated=evaluated)

    @activity.defn(name="write_plan_lines")
    async def write_plan_lines(input: acts.WritePlanLinesInput) -> acts.WritePlanLinesResult:
        return acts.WritePlanLinesResult(line_count=len(input.evaluated))

    @activity.defn(name="publish_to_cube")
    async def publish_to_cube(input: acts.PublishToCubeInput) -> acts.PublishToCubeResult:
        calls["publish"] += 1
        return acts.PublishToCubeResult(published_row_count=1)

    @activity.defn(name="rollback_cube_publish")
    async def rollback_cube_publish(input: acts.PublishToCubeInput) -> None:
        calls["rollback"] += 1

    @activity.defn(name="commit_to_treasury")
    async def commit_to_treasury(input: acts.CommitToTreasuryInput) -> acts.CommitToTreasuryResult:
        calls["commit"] += 1
        if commit_should_fail:
            raise RuntimeError("commitment service: simulated failure")
        return acts.CommitToTreasuryResult(commitment_id="commitment-123")

    @activity.defn(name="compensate_commitment")
    async def compensate_commitment(input: acts.CompensateCommitmentInput) -> None:
        pass

    @activity.defn(name="compute_variance")
    async def compute_variance(input: acts.ComputeVarianceInput) -> acts.ComputeVarianceResult:
        return acts.ComputeVarianceResult(report_id="variance-report-1")

    activities = [
        check_plan_locked,
        snapshot_drivers,
        resolve_dirty_set_activity,
        evaluate_partition,
        write_plan_lines,
        publish_to_cube,
        rollback_cube_publish,
        commit_to_treasury,
        compensate_commitment,
        compute_variance,
    ]
    return activities, calls


def _input(**overrides) -> PlanRecomputeInput:
    defaults = dict(
        plan_version_id=str(uuid.uuid4()),
        scenario_id="base",
        revision=1,
        requested_by="planner@example.com",
        driver_shocks=[DriverShock(name="bill_rate", value=175.0)],
        approval_timeout_seconds=5.0,
    )
    defaults.update(overrides)
    return PlanRecomputeInput(**defaults)


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


class TestHappyPath:
    async def test_approve_signal_publishes_and_commits(self, env):
        activities, calls = _mock_activities()
        task_queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                PlanRecomputeWorkflow.run,
                _input(),
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            await handle.signal(PlanRecomputeWorkflow.approve, "cfo@example.com")
            result = await handle.result()

        assert result.status == "Published"
        assert result.commitment_id == "commitment-123"
        assert result.variance_report_id == "variance-report-1"
        assert "revenue_forecast" in result.dirty_set
        assert calls["publish"] == 1
        assert calls["commit"] == 1
        assert calls["rollback"] == 0


class TestRejection:
    async def test_reject_signal_rejects_without_publishing(self, env):
        activities, calls = _mock_activities()
        task_queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                PlanRecomputeWorkflow.run,
                _input(),
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            await handle.signal(PlanRecomputeWorkflow.reject, args=["cfo@example.com", "numbers look wrong"])
            result = await handle.result()

        assert result.status == "Rejected"
        assert result.reason == "numbers look wrong"
        assert calls["publish"] == 0
        assert calls["commit"] == 0

    async def test_not_locked_is_rejected_before_any_recompute_activity(self, env):
        activities, calls = _mock_activities(locked=False)
        task_queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                PlanRecomputeWorkflow.run,
                _input(),
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )

        assert result.status == "Rejected"
        assert "Locked" in result.reason
        assert calls["publish"] == 0

    async def test_approval_timeout_expires_without_publishing(self, env):
        activities, calls = _mock_activities()
        task_queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
            activities=activities,
        ):
            # No signal sent: time-skipping advances past the (short)
            # approval timeout and the workflow expires into Rejected.
            result = await env.client.execute_workflow(
                PlanRecomputeWorkflow.run,
                _input(approval_timeout_seconds=1.0),
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )

        assert result.status == "Rejected"
        assert result.reason == "timeout"
        assert calls["publish"] == 0
        assert calls["commit"] == 0


class TestCompensation:
    async def test_commit_failure_after_publish_rolls_back_cube(self, env):
        activities, calls = _mock_activities(commit_should_fail=True)
        task_queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                PlanRecomputeWorkflow.run,
                _input(),
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            await handle.signal(PlanRecomputeWorkflow.approve, "cfo@example.com")
            result = await handle.result()

        assert result.status == "Rejected"
        assert "rolled back" in result.reason
        assert calls["publish"] == 1
        assert calls["rollback"] == 1


class TestSecondShockRejection:
    """Decision 2 is enforced by the update *validator*, which runs
    independently of any particular timing race in the workflow body, so
    it's exercised directly against the workflow instance rather than
    through a full time-skipping run.
    """

    def test_validator_rejects_shock_once_dirty_set_is_resolved(self):
        wf = PlanRecomputeWorkflow()
        wf._dirty_set_resolved = True
        with pytest.raises(ApplicationError, match="dirty set already resolved"):
            wf.validate_shock_driver(DriverShock(name="bill_rate", value=200.0))

    def test_validator_allows_shock_before_dirty_set_is_resolved(self):
        wf = PlanRecomputeWorkflow()
        wf.validate_shock_driver(DriverShock(name="bill_rate", value=200.0))


class TestProgressQuery:
    async def test_progress_reports_done_phase_after_completion(self, env):
        activities, _ = _mock_activities()
        task_queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanRecomputeWorkflow, PartitionRecomputeWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                PlanRecomputeWorkflow.run,
                _input(),
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            await handle.signal(PlanRecomputeWorkflow.approve, "cfo@example.com")
            await handle.result()
            progress = await handle.query(PlanRecomputeWorkflow.progress)

        assert progress["phase"] == "done"
        assert progress["dirty_set_fraction_complete"] == 1.0
