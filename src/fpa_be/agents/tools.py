"""The exactly-four tools any agent (leader or member) is ever given:
`list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver`.

No `run_sql` tool exists anywhere in this module or the codebase --
`run_finops_query` is the only path from agent output to SQL, and it goes
through the same `DSL -> parse -> typecheck -> authorize -> scope-inject ->
parameterize -> ClickHouse` pipeline the compiler already enforces, never a
raw string built from model output. Masking and the disclosure log are not
in these bodies: they are the `disclosure_tool_hook` every agent carries.

Every tool takes `run_context: RunContext` so Agno injects the current
run's context automatically; the caller's row scope comes from
`run_context.dependencies["security_context"]`, resolved once per request
by the authenticated HTTP boundary that starts the run -- never baked into
a prompt, and never something the model can override by asking nicely.
"""

import json
from datetime import date

import asyncpg
from agno.run.base import RunContext
from agno.tools import tool

from fpa_be.compiler.errors import CompilerError
from fpa_be.compiler.security import SecurityContext
from fpa_be.cube.client import CubeClient
from fpa_be.cube.errors import CubeError
from fpa_be.db import app_dsn
from fpa_be.dsl.errors import FinOpsExprError
from fpa_be.dsl.parser import parse_expr, parse_query
from fpa_be.dsl.resolver import check_node
from fpa_be.registry.dimensions import DIM_COLUMNS, SEPARATE_AXES
from fpa_be.registry.measures import public_measures


class NoSecurityContextError(RuntimeError):
    """A tool was invoked without a resolved SecurityContext in
    run_context.dependencies -- this is a caller bug (the authenticated
    HTTP boundary is supposed to set this on every run), not something a
    user's prompt can talk its way around."""


def _security_context(run_context: RunContext) -> SecurityContext:
    deps = run_context.dependencies or {}
    security_context = deps.get("security_context")
    if security_context is None:
        raise NoSecurityContextError(
            "no security_context in run_context.dependencies; refusing to run scoped to nothing"
        )
    return security_context


@tool()
def list_metrics(run_context: RunContext) -> str:
    """List every metric the DSL can query: name, description, and
    aggregation kind (additive / semi_additive / ratio)."""
    _security_context(run_context)  # every tool call is scoped, even a read-only listing
    measures = [
        {"name": name, "description": m.description, "kind": m.kind.value}
        for name, m in sorted(public_measures().items())
    ]
    return json.dumps(measures)


@tool()
def list_dimensions(run_context: RunContext) -> str:
    """List every dimension the DSL can group/filter by: the 19-column
    compound key plus company/account/period_month."""
    _security_context(run_context)
    return json.dumps({"dimensions": list(DIM_COLUMNS), "separate_axes": list(SEPARATE_AXES)})


@tool()
async def run_finops_query(dsl: str, run_context: RunContext) -> str:
    """Run a FinOpsExpr query against the cube. `dsl` is a `query` per the
    grammar, e.g. `SELECT services_revenue BY practice WHERE geo_country =
    'PL' FOR PERIOD 2026-Q2`. To explain a plan-vs-actual gap, bridge one
    revenue or cost measure: append `COMPARE PLAN pv='PV-2026-0001' TO ACTUAL
    BRIDGE` (optionally `BY practice|grade|company|account`); every rollup
    row then carries its legs, residual and tolerance. Returns the rows, the
    vintage they were read from, and the DSL echoed back -- every number in
    an answer built from this must trace back to a row in here."""
    security_context = _security_context(run_context)
    try:
        query = parse_query(dsl)
        check_node(query)
        result = CubeClient().run(query, security_context)
    except (FinOpsExprError, CompilerError, CubeError) as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        {
            "dsl": dsl,
            "vintage": result.vintage,
            "columns": list(result.columns),
            "rows": [list(row) for row in result.rows],
        },
        default=str,
    )


@tool(requires_confirmation=True)
async def propose_driver(
    name: str,
    formula: str,
    effective_date: str,
    run_context: RunContext,
    is_rate_driver: bool = False,
) -> str:
    """Propose a new driver or a change to an existing one. This only ever
    records a Draft `plan_driver_proposal` -- it never writes `plan_driver`.
    Two humans stand between it and the plan: Agno's confirmation HITL
    decides whether the proposal is recorded at all, and a controller who is
    not the proposer approves it through /driver-proposals before it becomes
    a live driver (see docs/APPROVAL_ARCHITECTURE.md).
    """
    _security_context(run_context)
    if not run_context.user_id:
        return json.dumps({"error": "no authenticated caller on this run; a proposal must name who it is for"})
    try:
        parse_expr(formula)
    except FinOpsExprError as exc:
        return json.dumps({"error": f"malformed driver formula: {exc}"})
    try:
        effective = date.fromisoformat(effective_date)
    except ValueError as exc:
        return json.dumps({"error": f"effective_date must be YYYY-MM-DD: {exc}"})

    conn = await asyncpg.connect(app_dsn())
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO plan_driver_proposal (name, formula, is_rate_driver, effective_date, proposed_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, name, formula, effective_date, state
            """,
            name,
            formula,
            is_rate_driver,
            effective,
            run_context.user_id,
        )
    finally:
        await conn.close()
    return json.dumps(
        {
            "proposal_id": str(row["id"]),
            "proposed_driver": row["name"],
            "formula": row["formula"],
            "effective_date": row["effective_date"].isoformat(),
            "state": row["state"],
            "note": "Draft only: a controller other than the proposer must approve it before it becomes a live driver",
        }
    )


ALL_TOOLS = [list_metrics, list_dimensions, run_finops_query, propose_driver]
