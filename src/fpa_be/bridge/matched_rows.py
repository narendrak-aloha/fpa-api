"""Executes the compiler's matched plan/actual statement
(`fpa_be.compiler.compile.compile_matched_rows`) and maps each result row to
a `MatchedFactRow` -- the raw, row-level population the bridge cascades
over. The SQL itself, including the caller's row scope, is the compiler's
responsibility, not this module's.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from fpa_be.compiler.compile import CompiledQuery, compile_matched_rows
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


def run_matched_rows(client, compiled: CompiledQuery) -> list[MatchedFactRow]:
    result = client.query(compiled.sql, parameters=compiled.params)
    return [MatchedFactRow(**dict(zip(result.column_names, row, strict=True))) for row in result.result_rows]


def fetch_matched_rows(
    client,
    security_context: SecurityContext,
    period_start: str,
    period_end: str,
    resolved_vintage: int | None,
    scenario_id: str = "base",
    plan_version: str = PLAN_VERSION,
    accounts: tuple[str, ...] | None = None,
) -> list[MatchedFactRow]:
    """The matched set for one inclusive [period_start, period_end] range,
    scoped to the caller's row-level security."""
    compiled = compile_matched_rows(
        security_context,
        date.fromisoformat(period_start),
        date.fromisoformat(period_end) + timedelta(days=1),
        resolved_vintage,
        plan_version=plan_version,
        scenario_id=scenario_id,
        accounts=accounts,
    )
    return run_matched_rows(client, compiled)
