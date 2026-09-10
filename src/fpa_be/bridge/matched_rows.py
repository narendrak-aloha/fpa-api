"""Pulls the raw, row-level plan/actual matched population the bridge
cascades over.

This is deliberately a different access pattern from the agent-facing
compiler (`fpa_be.compiler.compile`): the compiler only ever returns
pre-aggregated measures at whatever grain the DSL asked for, because that is
the one governed path an agent's query can take. The bridge is an internal,
non-agent-facing computation that needs raw matched (plan row, actual row)
pairs -- quantity and price separately, not just a summed amount_functional
-- so it talks to ClickHouse directly rather than going through compile().

Joins plan (`fact_plan_line`, scenario 'base', latest revision) to actual
(`actual_fact_source`, vintage-aware reconstruction shared with the
compiler) on the full matched key `(company, period_month, account,
dim_signature_hash)`, restricted to keys present on both sides, and looks up
plan/actual FX rates by `(functional_currency, period_month)` against
`to_currency = 'USD'`.
"""

from dataclasses import dataclass

from fpa_be.compiler.fact_source import actual_fact_source
from fpa_be.compiler.security import SecurityContext
from fpa_be.registry.reference import PLAN_VERSION


@dataclass(frozen=True)
class MatchedFactRow:
    company: str
    period_month: str
    account: str
    dim_signature_hash: str
    practice: str
    grade: str
    plan_qty: float
    plan_price: float
    plan_fx: float
    actual_qty: float
    actual_price: float
    actual_fx: float


def _security_where(security_context: SecurityContext) -> str:
    clauses = []
    if security_context.allowed_companies is not None:
        companies = ", ".join(f"'{c}'" for c in sorted(security_context.allowed_companies))
        clauses.append(f"company IN ({companies})")
    if security_context.allowed_geo_countries is not None:
        countries = ", ".join(f"'{c}'" for c in sorted(security_context.allowed_geo_countries))
        clauses.append(f"geo_country IN ({countries})")
    return " AND ".join(clauses) if clauses else "1=1"


def fetch_matched_rows(
    client,
    security_context: SecurityContext,
    period_start: str,
    period_end: str,
    resolved_vintage: int | None,
    scenario_id: str = "base",
) -> list[MatchedFactRow]:
    """Runs the matched plan/actual join for one period range, scoped to the
    caller's row-level security, and returns one MatchedFactRow per matched
    key. Only keys present on *both* sides are returned -- an unmatched plan
    or actual row can't be bridged, since the bridge decomposes a gap between
    two priced quantities, not a one-sided total.
    """
    where = _security_where(security_context)
    query = f"""
        SELECT
            p.company AS company,
            p.period_month AS period_month,
            p.account AS account,
            p.dim_signature_hash AS dim_signature_hash,
            p.practice AS practice,
            p.grade AS grade,
            p.quantity AS plan_qty,
            p.unit_price AS plan_price,
            fx_plan.rate AS plan_fx,
            a.quantity AS actual_qty,
            a.unit_price AS actual_price,
            fx_actual.rate AS actual_fx
        FROM
        (
            SELECT company, period_month, account, dim_signature_hash, practice, grade,
                   quantity, unit_price, functional_currency, geo_country
            FROM fact_plan_line FINAL
            WHERE plan_version = {{plan_version:String}}
              AND scenario_id = {{scenario_id:String}}
              AND {where}
              AND period_month >= {{period_start:Date}}
              AND period_month <= {{period_end:Date}}
        ) AS p
        INNER JOIN
        (
            SELECT company, period_month, account, dim_signature_hash,
                   quantity, unit_price, functional_currency, geo_country
            FROM {actual_fact_source(resolved_vintage)}
            WHERE {where}
              AND period_month >= {{period_start:Date}}
              AND period_month <= {{period_end:Date}}
        ) AS a
        ON p.company = a.company
           AND p.period_month = a.period_month
           AND p.account = a.account
           AND p.dim_signature_hash = a.dim_signature_hash
        INNER JOIN dim_fx_plan AS fx_plan
            ON fx_plan.plan_version = {{plan_version:String}}
               AND fx_plan.period_month = p.period_month
               AND fx_plan.from_currency = p.functional_currency
               AND fx_plan.to_currency = 'USD'
        INNER JOIN dim_fx_actual AS fx_actual
            ON fx_actual.period_month = a.period_month
               AND fx_actual.from_currency = a.functional_currency
               AND fx_actual.to_currency = 'USD'
    """
    result = client.query(
        query,
        parameters={
            "plan_version": PLAN_VERSION,
            "scenario_id": scenario_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )
    return [
        MatchedFactRow(
            company=row[0],
            period_month=row[1],
            account=row[2],
            dim_signature_hash=row[3],
            practice=row[4],
            grade=row[5],
            plan_qty=row[6],
            plan_price=row[7],
            plan_fx=row[8],
            actual_qty=row[9],
            actual_price=row[10],
            actual_fx=row[11],
        )
        for row in result.result_rows
    ]
