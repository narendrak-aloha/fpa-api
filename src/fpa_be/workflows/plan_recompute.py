"""PlanRecomputeWorkflow: the assignment's fixed pseudocode --

    1 snapshot_drivers      2 resolve_dirty_set   3 evaluate_partition
    4 write_plan_lines      5 await_approval       6 publish_to_cube
    7 commit_to_treasury    8 compute_variance

-- as a deterministic Temporal workflow. Every I/O, clock read, and random
choice lives in `fpa_be.workflows.activities`; this module is pure
orchestration so it replays identically every time.

Design decisions locked in by the plan (`optimized-gathering-wozniak.md`):
- A second driver shock arriving after the dirty set has been resolved for
  the in-flight revision is *rejected* (Decision 2), not folded in -- an
  `update` validator enforces this, not just the handler body, so the
  rejection happens before the update is even accepted into history.
- An approval that isn't signalled within the timeout window *expires* into
  `Rejected(reason="timeout")` (Decision 3) -- nothing publishes, nothing
  commits.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from fpa_be.workflows import activities as acts

PARTITION_SIZE = 3
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 600.0


@dataclass
class DriverShock:
    name: str
    value: float


@dataclass
class PlanRecomputeInput:
    plan_version_id: str
    scenario_id: str
    revision: int
    requested_by: str
    driver_shocks: list[DriverShock] = field(default_factory=list)
    approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS

    # Continue-as-new resume state. Empty on a fresh run; populated only by
    # this workflow's own continue_as_new call, never by an external caller.
    resume_formulas: dict[str, str] = field(default_factory=dict)
    resume_bindings: dict[str, float] = field(default_factory=dict)
    resume_dirty_set: list[str] = field(default_factory=list)
    resume_partition_index: int = 0
    resume_evaluated: list[acts.EvaluatedDriver] = field(default_factory=list)


@dataclass
class PlanRecomputeResult:
    status: str  # "Published" | "Rejected"
    reason: str | None = None
    dirty_set: list[str] = field(default_factory=list)
    commitment_id: str | None = None
    variance_report_id: str | None = None


_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ApplicationError"])


@workflow.defn
class PartitionRecomputeWorkflow:
    """One child workflow per chunk of the topologically-ordered dirty set.
    Chunks are evaluated by the parent in order (a chunk's formulas may
    depend on an earlier chunk's results, never a later one), but each gets
    its own child-workflow history, so a worker restart mid-fan-out resumes
    from the last completed chunk instead of re-evaluating everything.
    """

    @workflow.run
    async def run(self, input: acts.EvaluatePartitionInput) -> acts.EvaluatePartitionResult:
        return await workflow.execute_activity(
            acts.evaluate_partition,
            input,
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=20),
            retry_policy=_ACTIVITY_RETRY,
        )


@workflow.defn
class PlanRecomputeWorkflow:
    def __init__(self) -> None:
        self._phase = "not_started"
        self._dirty_set_resolved = False
        self._partitions_done = 0
        self._partitions_total = 0
        self._rejected_shocks: list[str] = []
        self._approval: bool | None = None  # None = pending, True = approved, False = rejected
        self._approval_actor: str | None = None
        self._approval_reason: str | None = None

    @workflow.run
    async def run(self, input: PlanRecomputeInput) -> PlanRecomputeResult:
        resuming = bool(input.resume_dirty_set)

        if not resuming:
            self._phase = "checking_lock"
            locked = await workflow.execute_activity(
                acts.check_plan_locked,
                input.plan_version_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            )
            if not locked:
                self._phase = "done"
                return PlanRecomputeResult(status="Rejected", reason="plan_version is not Locked")

            self._phase = "snapshotting_drivers"
            snapshot = await workflow.execute_activity(
                acts.snapshot_drivers,
                acts.SnapshotDriversInput(plan_version_id=input.plan_version_id),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            )
            formulas = {name: d.formula for name, d in snapshot.drivers.items()}
            bindings = {
                name: (d.rate_value if d.rate_value is not None else 0.0)
                for name, d in snapshot.drivers.items()
            }
            shocks = {s.name: s.value for s in input.driver_shocks}
            bindings.update(shocks)

            self._phase = "resolving_dirty_set"
            dirty_set = await workflow.execute_activity(
                acts.resolve_dirty_set_activity,
                acts.ResolveDirtySetInput(formulas=formulas, shocked=list(shocks)),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            )
            # Decision 2: from this point on, a shock update for this
            # revision is rejected -- the dirty set it would need to widen
            # has already been resolved and handed to the fan-out below.
            self._dirty_set_resolved = True
            partition_start = 0
            evaluated_all: list[acts.EvaluatedDriver] = []
        else:
            formulas = input.resume_formulas
            bindings = dict(input.resume_bindings)
            dirty_set = input.resume_dirty_set
            partition_start = input.resume_partition_index
            evaluated_all = list(input.resume_evaluated)
            self._dirty_set_resolved = True

        self._phase = "evaluating_partitions"
        partitions = [dirty_set[i : i + PARTITION_SIZE] for i in range(0, len(dirty_set), PARTITION_SIZE)]
        self._partitions_total = len(partitions)
        self._partitions_done = partition_start

        for i in range(partition_start, len(partitions)):
            if i > partition_start and workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(
                    PlanRecomputeInput(
                        plan_version_id=input.plan_version_id,
                        scenario_id=input.scenario_id,
                        revision=input.revision,
                        requested_by=input.requested_by,
                        approval_timeout_seconds=input.approval_timeout_seconds,
                        resume_formulas=formulas,
                        resume_bindings=bindings,
                        resume_dirty_set=dirty_set,
                        resume_partition_index=i,
                        resume_evaluated=evaluated_all,
                    )
                )

            result = await workflow.execute_child_workflow(
                PartitionRecomputeWorkflow.run,
                acts.EvaluatePartitionInput(
                    driver_names=partitions[i], formulas=formulas, bindings=dict(bindings)
                ),
                id=f"{workflow.info().workflow_id}-partition-{i}",
            )
            for driver in result.evaluated:
                bindings[driver.name] = driver.value
            evaluated_all.extend(result.evaluated)
            self._partitions_done = i + 1

        self._phase = "writing_plan_lines"
        await workflow.execute_activity(
            acts.write_plan_lines,
            acts.WritePlanLinesInput(
                plan_version_id=input.plan_version_id,
                scenario_id=input.scenario_id,
                revision=input.revision,
                requested_by=input.requested_by,
                evaluated=evaluated_all,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )

        self._phase = "awaiting_approval"
        try:
            await workflow.wait_condition(
                lambda: self._approval is not None, timeout=timedelta(seconds=input.approval_timeout_seconds)
            )
        except TimeoutError:
            self._phase = "done"
            return PlanRecomputeResult(status="Rejected", reason="timeout", dirty_set=dirty_set)

        if not self._approval:
            self._phase = "done"
            return PlanRecomputeResult(
                status="Rejected", reason=self._approval_reason or "rejected_by_human", dirty_set=dirty_set
            )

        self._phase = "checking_lock_before_publish"
        still_locked = await workflow.execute_activity(
            acts.check_plan_locked,
            input.plan_version_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )
        if not still_locked:
            self._phase = "done"
            return PlanRecomputeResult(status="Rejected", reason="plan_version is no longer Locked", dirty_set=dirty_set)

        published = False
        try:
            self._phase = "publishing_to_cube"
            publish_input = acts.PublishToCubeInput(
                plan_version_id=input.plan_version_id, scenario_id=input.scenario_id, revision=input.revision
            )
            await workflow.execute_activity(
                acts.publish_to_cube,
                publish_input,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            )
            published = True

            self._phase = "committing_to_treasury"
            total_amount = sum(d.value for d in evaluated_all)
            commit_result = await workflow.execute_activity(
                acts.commit_to_treasury,
                acts.CommitToTreasuryInput(
                    plan_version_id=input.plan_version_id,
                    scenario_id=input.scenario_id,
                    revision=input.revision,
                    amount=total_amount,
                ),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except (ActivityError, ApplicationError):
            if published:
                self._phase = "compensating"
                await workflow.execute_activity(
                    acts.rollback_cube_publish,
                    publish_input,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_ACTIVITY_RETRY,
                )
            self._phase = "done"
            return PlanRecomputeResult(
                status="Rejected", reason="commit_to_treasury failed; cube publish rolled back", dirty_set=dirty_set
            )
        except asyncio.CancelledError:
            if published:
                self._phase = "compensating"
                await workflow.execute_activity(
                    acts.rollback_cube_publish,
                    publish_input,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_ACTIVITY_RETRY,
                )
            raise

        self._phase = "computing_variance"
        variance = await workflow.execute_activity(
            acts.compute_variance,
            acts.ComputeVarianceInput(
                plan_version_id=input.plan_version_id, scenario_id=input.scenario_id, revision=input.revision
            ),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_ACTIVITY_RETRY,
        )

        self._phase = "done"
        return PlanRecomputeResult(
            status="Published",
            dirty_set=dirty_set,
            commitment_id=commit_result.commitment_id,
            variance_report_id=variance.report_id,
        )

    @workflow.update
    async def shock_driver(self, shock: DriverShock) -> None:
        # Body left empty on purpose: acceptance/rejection is entirely the
        # validator's job below, so a rejected update never enters history.
        return None

    @shock_driver.validator
    def validate_shock_driver(self, shock: DriverShock) -> None:
        if self._dirty_set_resolved:
            raise ApplicationError(
                "dirty set already resolved for this revision; wait for publish or rejection, "
                "then re-shock as part of the next revision",
                non_retryable=True,
            )

    @workflow.signal
    def approve(self, actor: str) -> None:
        if self._approval is None:
            self._approval = True
            self._approval_actor = actor

    @workflow.signal
    def reject(self, actor: str, reason: str = "") -> None:
        if self._approval is None:
            self._approval = False
            self._approval_actor = actor
            self._approval_reason = reason or "rejected_by_human"

    @workflow.query
    def progress(self) -> dict:
        fraction = self._partitions_done / self._partitions_total if self._partitions_total else 0.0
        return {"phase": self._phase, "dirty_set_fraction_complete": fraction}
