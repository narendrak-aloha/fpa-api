"""Activities for PlanRecomputeWorkflow: every I/O, clock read, and piece of
randomness the recompute needs lives here, never in the workflow itself, so
the workflow stays pure orchestration and replay-deterministic.

Scope decision (documented, not hidden): the assignment's fixed pseudocode
names steps (snapshot_drivers, resolve_dirty_set, evaluate_partition,
write_plan_lines, publish_to_cube, commit_to_treasury, compute_variance) but
does not specify how a recomputed driver value maps onto the
company/account/period_month/dim_signature_hash grain plan_version_line
actually keys on. Each dirty driver is therefore written as its own
representative plan_version_line (account = driver name, company = "ALL",
period = the plan year's first month), not fanned out across the full cube
grain. That is also why compute_variance bridges the governed version's
cube plan (its plan_code) rather than these representative lines: they have
no matched actual keys to bridge against.
"""

import datetime
import hashlib
import json
import os
from dataclasses import dataclass

import asyncpg
import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fpa_be.bridge.decompose import revenue_bridge
from fpa_be.bridge.matched_rows import fetch_matched_rows
from fpa_be.bridge.persist import persist_bridge
from fpa_be.compiler.security import SecurityContext
from fpa_be.db import app_dsn
from fpa_be.dsl import eval_expr, parse_expr, resolve_dirty_set
from fpa_be.registry.measures import MEASURES
from fpa_be.registry.reference import PLAN_YEAR

COMMITMENT_SERVICE_URL = os.environ.get("COMMITMENT_SERVICE_URL", "http://localhost:8001")
COMMITMENT_HTTP_TIMEOUT_SECONDS = float(os.environ.get("COMMITMENT_HTTP_TIMEOUT_SECONDS", "10"))

# A fixed period, never the wall clock: a re-run of the same recompute must
# land on the same line key.
_PLAN_LINE_PERIOD = datetime.date(PLAN_YEAR, 1, 1)
_REVENUE_ACCOUNTS = MEASURES["total_revenue"].accounts


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
    shocked: bool = False


@dataclass
class EvaluatePartitionInput:
    driver_names: list[str]
    formulas: dict[str, str]
    bindings: dict[str, float]
    shocked: dict[str, float]


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
class CommitmentInput:
    """Identifies the revision whose published forecast is committed (or
    reversed). The amount is not carried here: it is read from the lines
    that were published, so the ledger and the cube cannot disagree on it."""

    plan_version_id: str
    scenario_id: str
    revision: int


@dataclass
class CommitToTreasuryResult:
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
        shocked = name in input.shocked
        # A shocked driver is its shocked value; re-evaluating its own formula
        # would silently undo the shock the recompute was started for.
        value = input.shocked[name] if shocked else eval_expr(parse_expr(input.formulas[name]), bindings)
        bindings[name] = value
        evaluated.append(
            EvaluatedDriver(
                name=name, value=value, formula=input.formulas[name], inputs=dict(bindings), shocked=shocked
            )
        )
    return EvaluatePartitionResult(evaluated=evaluated)


@activity.defn
async def write_plan_lines(input: WritePlanLinesInput) -> WritePlanLinesResult:
    """Idempotent: re-running the same recompute inserts nothing and finds
    every line already there with the same value and derivation trace. A
    line already there with a *different* value means this Locked revision
    was recomputed differently before; that is refused, because a Locked
    version's lines are immutable (fn_plan_version_line_guard) and a changed
    plan needs a new version that supersedes this one."""
    conn = await asyncpg.connect(app_dsn())
    try:
        async with conn.transaction():
            for driver in input.evaluated:
                trace = json.dumps(
                    {
                        "driver": driver.name,
                        "formula": driver.formula,
                        "shocked": driver.shocked,
                        "inputs": driver.inputs,
                        "value": driver.value,
                    }
                )
                key = (
                    input.plan_version_id,
                    input.scenario_id,
                    input.revision,
                    driver.name,
                    hashlib.sha256(driver.name.encode()).hexdigest()[:16],
                    _PLAN_LINE_PERIOD,
                )
                inserted = await conn.fetchval(
                    """
                    INSERT INTO plan_version_line
                        (plan_version_id, scenario_id, revision, company, account,
                         period_month, dim_signature_hash, quantity, unit_price,
                         amount_functional, driver_derivation_trace)
                    VALUES ($1, $2, $3, 'ALL', $4, $6, $5, 1, $7, $7, $8)
                    ON CONFLICT (plan_version_id, scenario_id, revision, company, account,
                                 period_month, dim_signature_hash)
                    DO NOTHING
                    RETURNING id
                    """,
                    *key,
                    driver.value,
                    trace,
                )
                if inserted is not None:
                    continue
                same = await conn.fetchval(
                    """
                    SELECT unit_price = round($7::numeric, 6) AND driver_derivation_trace = $8::jsonb
                    FROM plan_version_line
                    WHERE plan_version_id = $1 AND scenario_id = $2 AND revision = $3 AND company = 'ALL'
                      AND account = $4 AND dim_signature_hash = $5 AND period_month = $6
                    """,
                    *key,
                    driver.value,
                    trace,
                )
                if not same:
                    raise ApplicationError(
                        f"revision {input.revision} of plan_version {input.plan_version_id} already has a different "
                        f"value for driver {driver.name!r}; a changed plan needs a new version that supersedes it",
                        non_retryable=True,
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


async def _published_amount(input: CommitmentInput) -> float:
    conn = await asyncpg.connect(app_dsn())
    try:
        total = await conn.fetchval(
            "SELECT COALESCE(sum(amount_functional), 0) FROM plan_version_line "
            "WHERE plan_version_id = $1 AND scenario_id = $2 AND revision = $3",
            input.plan_version_id,
            input.scenario_id,
            input.revision,
        )
    finally:
        await conn.close()
    return float(total)


def _commitment_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=COMMITMENT_SERVICE_URL, timeout=COMMITMENT_HTTP_TIMEOUT_SECONDS)


def _raise_for_status(response: httpx.Response) -> None:
    """A 4xx is the counterparty refusing the request as made -- retrying it
    unchanged cannot help, so it is non-retryable. 5xx responses and
    timeouts are transient and left to the activity's retry policy."""
    if 400 <= response.status_code < 500:
        raise ApplicationError(
            f"commitment service refused the request ({response.status_code}): {response.text}", non_retryable=True
        )
    response.raise_for_status()


async def _post_commitment(client: httpx.AsyncClient, input: CommitmentInput) -> str:
    response = await client.post(
        "/commitments",
        json={
            "plan_version_id": input.plan_version_id,
            "scenario_id": input.scenario_id,
            "revision": input.revision,
            "amount": await _published_amount(input),
            "currency": "USD",
        },
        headers={"Idempotency-Key": _idempotency_key(input.plan_version_id, input.revision, "commit_to_treasury")},
    )
    _raise_for_status(response)
    return response.json()["id"]


@activity.defn
async def commit_to_treasury(input: CommitmentInput) -> CommitToTreasuryResult:
    async with _commitment_client() as client:
        return CommitToTreasuryResult(commitment_id=await _post_commitment(client, input))


@activity.defn
async def compensate_commitment(input: CommitmentInput) -> None:
    """Reverses whatever commitment exists under this revision's idempotency
    key. The POST is replayed with that same key first: if the original
    landed -- including one whose response was lost to a timeout -- the
    replay returns it; if it never landed, the replay claims the key now, so
    a late-arriving original can no longer create a second one. Either way
    the id it returns is then reversed, and the ledger ends with nothing
    active for this revision."""
    async with _commitment_client() as client:
        commitment_id = await _post_commitment(client, input)
        response = await client.delete(f"/commitments/{commitment_id}")
        if response.status_code != 404:
            _raise_for_status(response)


@activity.defn
async def compute_variance(input: ComputeVarianceInput) -> ComputeVarianceResult:
    """Step 8: bridges the governed version's plan in the cube (its
    plan_code and scenario) against the ledger, revenue accounts, for the
    plan year -- and says exactly that in the cut label. Idempotent: a
    re-run returns the report already written for the same cut."""
    conn = await asyncpg.connect(app_dsn())
    try:
        plan_code = await conn.fetchval("SELECT plan_code FROM plan_version WHERE id = $1", input.plan_version_id)
        if plan_code is None:
            raise ApplicationError(f"plan_version {input.plan_version_id} not found", non_retryable=True)
        cut_label = f"{plan_code}/{input.scenario_id} vs actual, revenue, FY{PLAN_YEAR} (revision {input.revision})"
        existing = await conn.fetchval(
            "SELECT id FROM variance_report WHERE plan_version_id = $1 AND cut_label = $2",
            input.plan_version_id,
            cut_label,
        )
        if existing is not None:
            return ComputeVarianceResult(report_id=str(existing))

        rows = fetch_matched_rows(
            _cube_client(),
            SecurityContext(allowed_companies=None, allowed_geo_countries=None),
            period_start=f"{PLAN_YEAR}-01-01",
            period_end=f"{PLAN_YEAR}-12-31",
            resolved_vintage=None,
            scenario_id=input.scenario_id,
            plan_version=plan_code,
            accounts=_REVENUE_ACCOUNTS,
        )
        if not rows:
            return ComputeVarianceResult(report_id=None)

        async with conn.transaction():
            report_id = await persist_bridge(
                conn,
                plan_version_id=input.plan_version_id,
                cut_label=cut_label,
                dimension_filter={"plan_code": plan_code, "scenario_id": input.scenario_id, "accounts": "revenue"},
                vintage=None,
                created_by="PlanRecomputeWorkflow",
                result=revenue_bridge(rows),
                rollup_path="root",
                # Hex, not the raw FixedString bytes ClickHouse hands back:
                # cited_rows is jsonb, and the drill-through endpoint decodes
                # these with bytes.fromhex to compare against the cube again.
                cited_row_keys=[r.dim_signature_hash.hex() for r in rows],
            )
        return ComputeVarianceResult(report_id=str(report_id))
    finally:
        await conn.close()
