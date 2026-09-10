"""The exactly-four tools any agent (leader or member) is ever given:
`list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver`.

No `run_sql` tool exists anywhere in this module or the codebase --
`run_finops_query` is the only path from agent output to SQL, and it goes
through the same `DSL -> parse -> typecheck -> authorize -> scope-inject ->
parameterize -> ClickHouse` pipeline the compiler already enforces
(Phase 4), never a raw string built from model output.

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
def run_finops_query(dsl: str, run_context: RunContext) -> str:
    """Run a FinOpsExpr query against the cube. `dsl` is a `query` per the
    grammar (e.g. `SELECT services_revenue BY practice WHERE geo_country =
    'PL' FOR PERIOD 2026-Q2`). Returns the result rows, the vintage they
    were read from, and the compiled DSL echoed back -- every number in an
    answer built from this must trace back to a row in here."""
    security_context = _security_context(run_context)
    try:
        query = parse_query(dsl)
        check_node(query)
        cube = CubeClient()
        result = cube.run(query, security_context)
    except (FinOpsExprError, CompilerError) as exc:
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
    rate_value: float | None = None,
) -> str:
    """Propose a new driver or a change to an existing one. Gated by Agno's
    tool-confirmation HITL: the write below only ever executes after a
    human has confirmed this specific proposal -- this is the "should the
    agent even suggest this" gate (see docs/APPROVAL_ARCHITECTURE.md); it
    is not the plan-version governance approval and not the recompute
    workflow's await_approval signal, both of which still apply downstream
    before this driver's value ever reaches a published plan.
    """
    security_context = _security_context(run_context)
    try:
        parse_expr(formula)
    except FinOpsExprError as exc:
        return json.dumps({"error": f"malformed driver formula: {exc}"})

    actor = run_context.user_id or "unknown-agent-caller"
    conn = await asyncpg.connect(app_dsn())
    try:
        row = await conn.fetchrow(
            # rate_value and created_by are deliberately absent from the UPDATE branch:
            # fpa_app (this tool's DB role) only holds column-level UPDATE on
            # name/formula/is_rate_driver/effective_date -- rate_value is
            # fpa_controller-only, enforced at the DB grant level, not here.
            """
            INSERT INTO plan_driver (name, formula, is_rate_driver, rate_value, effective_date, created_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (name) DO UPDATE SET
                formula = EXCLUDED.formula,
                is_rate_driver = EXCLUDED.is_rate_driver,
                effective_date = EXCLUDED.effective_date
            RETURNING id, name, formula, effective_date
            """,
            name,
            formula,
            is_rate_driver,
            rate_value,
            date.fromisoformat(effective_date),
            actor,
        )
    finally:
        await conn.close()
    return json.dumps(
        {
            "proposed_driver": row["name"],
            "formula": row["formula"],
            "effective_date": row["effective_date"].isoformat(),
            "note": "recorded; still requires a Locked plan version and a signed-off recompute to take effect",
        },
        default=str,
    )


ALL_TOOLS = [list_metrics, list_dimensions, run_finops_query, propose_driver]
