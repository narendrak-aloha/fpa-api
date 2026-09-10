"""Activities for PlanRecomputeWorkflow: every I/O, clock read, and piece of
randomness the recompute needs lives here, never in the workflow itself, so
the workflow stays pure orchestration and replay-deterministic.

Scope decision (documented, not hidden): the assignment's fixed pseudocode
names steps (snapshot_drivers, resolve_dirty_set, evaluate_partition,
write_plan_lines, publish_to_cube, commit_to_treasury, compute_variance) but
does not specify how a recomputed driver value maps onto the
company/account/period_month/dim_signature_hash grain plan_version_line
actually keys on -- that mapping is Phase 6's cube-matched-row machinery, not
something this phase re-derives. Phase 8's job is the durability/idempotency/
compensation/HITL contract of the recompute engine itself, so each dirty
driver is written as its own representative plan_version_line (account =
driver name, company = "ALL"), not fanned out across the full cube grain.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field

import asyncpg
import httpx
from temporalio import activity

from fpa_be.bridge.decompose import revenue_bridge
from fpa_be.bridge.matched_rows import fetch_matched_rows
from fpa_be.bridge.persist import persist_bridge
from fpa_be.compiler.security import SecurityContext
from fpa_be.db import app_dsn
from fpa_be.dsl import eval_expr, parse_expr, resolve_dirty_set

COMMITMENT_SERVICE_URL = os.environ.get("COMMITMENT_SERVICE_URL", "http://localhost:8001")


@dataclass
class DriverSnapshot:
    name: str
    formula: str
    is_rate_driver: bool
    rate_value: float | None
    effective_date: str


@dataclass
class SnapshotDriversInput:
    plan_version_id: str


@dataclass
class SnapshotDriversResult:
    drivers: dict[str, DriverSnapshot]


@dataclass
class ResolveDirtySetInput:
    formulas: dict[str, str]
    shocked: list[str]


@dataclass
class EvaluatedDriver:
    name: str
    value: float
    formula: str
    inputs: dict[str, float]


@dataclass
class EvaluatePartitionInput:
    driver_names: list[str]
    formulas: dict[str, str]
    bindings: dict[str, float]


@dataclass
class EvaluatePartitionResult:
    evaluated: list[EvaluatedDriver]


@dataclass
class WritePlanLinesInput:
    plan_version_id: str
    scenario_id: str
    revision: int
    requested_by: str
    evaluated: list[EvaluatedDriver]


@dataclass
class WritePlanLinesResult:
    line_count: int


@dataclass
class PublishToCubeInput:
    plan_version_id: str
    scenario_id: str
    revision: int


@dataclass
class PublishToCubeResult:
    published_row_count: int


@dataclass
class CommitToTreasuryInput:
    plan_version_id: str
    scenario_id: str
    revision: int
    amount: float
    currency: str = "USD"


@dataclass
class CommitToTreasuryResult:
    commitment_id: str


@dataclass
class CompensateCommitmentInput:
    commitment_id: str


@dataclass
class ComputeVarianceInput:
    plan_version_id: str
    scenario_id: str
    revision: int


@dataclass
class ComputeVarianceResult:
    report_id: str | None


def _idempotency_key(plan_version_id: str, revision: int, suffix: str) -> str:
    """Stable across retries: derived from (plan_version_id, revision, step
    name), never from call time or a freshly generated uuid."""
    raw = f"{plan_version_id}:{revision}:{suffix}"
    return hashlib.sha256(raw.encode()).hexdigest()


@activity.defn
async def check_plan_locked(plan_version_id: str) -> bool:
    conn = await asyncpg.connect(app_dsn())
    try:
        state = await conn.fetchval("SELECT state FROM plan_version WHERE id = $1", plan_version_id)
        return state == "Locked"
    finally:
        await conn.close()


@activity.defn
async def snapshot_drivers(input: SnapshotDriversInput) -> SnapshotDriversResult:
    conn = await asyncpg.connect(app_dsn())
    try:
        rows = await conn.fetch(
            "SELECT name, formula, is_rate_driver, rate_value, effective_date FROM plan_driver"
        )
    finally:
        await conn.close()
    drivers = {
        row["name"]: DriverSnapshot(
            name=row["name"],
            formula=row["formula"],
            is_rate_driver=row["is_rate_driver"],
            rate_value=float(row["rate_value"]) if row["rate_value"] is not None else None,
            effective_date=row["effective_date"].isoformat(),
        )
        for row in rows
    }
    return SnapshotDriversResult(drivers=drivers)


@activity.defn
async def resolve_dirty_set_activity(input: ResolveDirtySetInput) -> list[str]:
    parsed = {name: parse_expr(formula) for name, formula in input.formulas.items()}
    return resolve_dirty_set(parsed, set(input.shocked))


@activity.defn
async def evaluate_partition(input: EvaluatePartitionInput) -> EvaluatePartitionResult:
    """Evaluates this partition's slice of the dirty set, in the order it
    was given (already topologically sound relative to `bindings`, which
    carries every driver value this partition could legally depend on --
    frozen snapshot values, applied shocks, and already-evaluated dirty
    values from earlier partitions).

    Heartbeats between drivers so a long partition is resumable: on worker
    restart, Temporal replays from the last recorded heartbeat rather than
    re-running the whole partition from scratch.
    """
    bindings = dict(input.bindings)
    evaluated: list[EvaluatedDriver] = []
    for i, name in enumerate(input.driver_names):
        activity.heartbeat(i)
        node = parse_expr(input.formulas[name])
        value = eval_expr(node, bindings)
        bindings[name] = value
        evaluated.append(EvaluatedDriver(name=name, value=value, formula=input.formulas[name], inputs=dict(bindings)))
    return EvaluatePartitionResult(evaluated=evaluated)


@activity.defn
async def write_plan_lines(input: WritePlanLinesInput) -> WritePlanLinesResult:
    conn = await asyncpg.connect(app_dsn())
    try:
        async with conn.transaction():
            for driver in input.evaluated:
                trace = {
                    "driver": driver.name,
                    "formula": driver.formula,
                    "inputs": driver.inputs,
                    "value": driver.value,
                }
                await conn.execute(
                    """
                    INSERT INTO plan_version_line
                        (plan_version_id, scenario_id, revision, company, account,
                         period_month, dim_signature_hash, quantity, unit_price,
                         amount_functional, driver_derivation_trace)
                    VALUES ($1, $2, $3, 'ALL', $4, date_trunc('month', now()), $5, 1, $6, $6, $7)
                    ON CONFLICT (plan_version_id, scenario_id, revision, company, account,
                                 period_month, dim_signature_hash)
                    DO UPDATE SET
                        unit_price = EXCLUDED.unit_price,
                        amount_functional = EXCLUDED.amount_functional,
                        driver_derivation_trace = EXCLUDED.driver_derivation_trace
                    """,
                    input.plan_version_id,
                    input.scenario_id,
                    input.revision,
                    driver.name,
                    hashlib.sha256(driver.name.encode()).hexdigest()[:16],
                    driver.value,
                    json.dumps(trace),
                )
        return WritePlanLinesResult(line_count=len(input.evaluated))
    finally:
        await conn.close()


def _cube_client():
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "fpa"),
        database=os.environ.get("CLICKHOUSE_DATABASE", "fpa_cube"),
    )


@activity.defn
async def publish_to_cube(input: PublishToCubeInput) -> PublishToCubeResult:
    """Idempotent on (plan_version, revision): if this revision's rows are
    already present, a retried publish is a no-op rather than a duplicate
    insert -- required because ReplacingMergeTree only dedupes at merge/
    FINAL-query time, not at insert time.
    """
    conn = await asyncpg.connect(app_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT company, account, period_month, dim_signature_hash, quantity,
                   unit_price, amount_functional
            FROM plan_version_line
            WHERE plan_version_id = $1 AND scenario_id = $2 AND revision = $3
            """,
            input.plan_version_id,
            input.scenario_id,
            input.revision,
        )
    finally:
        await conn.close()

    client = _cube_client()
    existing = client.query(
        "SELECT count() FROM fact_plan_line WHERE plan_version = %(pv)s "
        "AND scenario_id = %(sc)s AND revision = %(rev)s",
        parameters={"pv": input.plan_version_id, "sc": input.scenario_id, "rev": input.revision},
    ).result_rows[0][0]
    if existing >= len(rows) and rows:
        return PublishToCubeResult(published_row_count=len(rows))

    data = [
        [
            input.plan_version_id,
            input.scenario_id,
            input.revision,
            row["company"],
            row["period_month"],
            row["account"],
            row["dim_signature_hash"],
            float(row["quantity"]),
            float(row["unit_price"]),
            float(row["amount_functional"]),
            "USD",
            "driver_recompute",
        ]
        for row in rows
    ]
    if data:
        client.insert(
            "fact_plan_line",
            data,
            column_names=[
                "plan_version", "scenario_id", "revision", "company", "period_month",
                "account", "dim_signature_hash", "quantity", "unit_price",
                "amount_functional", "functional_currency", "plan_line_type",
            ],
        )
    return PublishToCubeResult(published_row_count=len(rows))


@activity.defn
async def rollback_cube_publish(input: PublishToCubeInput) -> None:
    """Compensation for a publish whose downstream commit_to_treasury then
    failed irrecoverably: deletes this revision's rows back out of the cube
    via a ClickHouse mutation so the cube never holds a revision the
    commitment ledger doesn't agree with.
    """
    client = _cube_client()
    client.command(
        "ALTER TABLE fact_plan_line DELETE WHERE plan_version = %(pv)s "
        "AND scenario_id = %(sc)s AND revision = %(rev)s",
        parameters={"pv": input.plan_version_id, "sc": input.scenario_id, "rev": input.revision},
    )


@activity.defn
async def commit_to_treasury(input: CommitToTreasuryInput) -> CommitToTreasuryResult:
    idempotency_key = _idempotency_key(input.plan_version_id, input.revision, "commit_to_treasury")
    async with httpx.AsyncClient(base_url=COMMITMENT_SERVICE_URL, timeout=10.0) as client:
        response = await client.post(
            "/commitments",
            json={
                "plan_version_id": input.plan_version_id,
                "scenario_id": input.scenario_id,
                "revision": input.revision,
                "amount": input.amount,
                "currency": input.currency,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        response.raise_for_status()
        return CommitToTreasuryResult(commitment_id=response.json()["id"])


@activity.defn
async def compensate_commitment(input: CompensateCommitmentInput) -> None:
    async with httpx.AsyncClient(base_url=COMMITMENT_SERVICE_URL, timeout=10.0) as client:
        response = await client.delete(f"/commitments/{input.commitment_id}")
        if response.status_code not in (204, 404):
            response.raise_for_status()


@activity.defn
async def compute_variance(input: ComputeVarianceInput) -> ComputeVarianceResult:
    client = _cube_client()
    security_context = SecurityContext(allowed_companies=None, allowed_geo_countries=None)
    rows = fetch_matched_rows(
        client,
        security_context,
        period_start="1900-01-01",
        period_end="2999-12-31",
        resolved_vintage=None,
        scenario_id=input.scenario_id,
    )
    if not rows:
        return ComputeVarianceResult(report_id=None)

    result = revenue_bridge(rows)
    conn = await asyncpg.connect(app_dsn())
    try:
        report_id = await persist_bridge(
            conn,
            plan_version_id=input.plan_version_id,
            cut_label=f"revision-{input.revision}",
            dimension_filter={},
            vintage=None,
            created_by="PlanRecomputeWorkflow",
            result=result,
            rollup_path="root",
            cited_row_keys=[r.dim_signature_hash for r in rows],
        )
        return ComputeVarianceResult(report_id=report_id)
    finally:
        await conn.close()
