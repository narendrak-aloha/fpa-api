"""Vintage drift reconciliation: compares a measure's total between the two
most recent ledger vintages and raises a structural drift flag if they
disagree by more than `threshold_pct`.

This is deliberately a plain function, not a prompt instruction -- the
drift-detection member's tool wraps it and returns `drift_flag` in its
structured result. No leader synthesis step can suppress a `True` here: the
Team-level post-hook in `fpa_be.agents.team` checks this field directly and
appends the warning itself if the model's own prose omitted it.
"""

import json
from dataclasses import dataclass

from fpa_be.compiler.security import SecurityContext
from fpa_be.cube.client import CubeClient
from fpa_be.dsl.parser import parse_query
from fpa_be.dsl.resolver import check_node

DEFAULT_DRIFT_THRESHOLD_PCT = 0.5


@dataclass(frozen=True)
class DriftResult:
    metric: str
    vintage_a: int
    vintage_b: int
    total_a: float
    total_b: float
    delta_pct: float
    drift_flag: bool


def _latest_two_vintages(client) -> tuple[int, int]:
    result = client.query("SELECT vintage FROM dim_ledger_vintage ORDER BY closed_at DESC LIMIT 2")
    vintages = [int(row[0]) for row in result.result_rows]
    if len(vintages) < 2:
        raise ValueError("need at least two closed vintages to reconcile drift")
    return vintages[1], vintages[0]  # (older, newer)


def check_vintage_drift(
    metric: str,
    for_period_clause: str,
    security_context: SecurityContext,
    threshold_pct: float = DEFAULT_DRIFT_THRESHOLD_PCT,
    client: CubeClient | None = None,
) -> DriftResult:
    """`for_period_clause` is the full clause text, e.g. `FOR PERIOD 2026-Q2`."""
    cube = client or CubeClient()
    vintage_a, vintage_b = _latest_two_vintages(cube._client)

    def _total(vintage: int) -> float:
        query = parse_query(f"SELECT {metric} {for_period_clause} AS OF {vintage}")
        check_node(query)
        result = cube.run(query, security_context)
        return sum(row[-1] for row in result.rows) if result.rows else 0.0

    total_a, total_b = _total(vintage_a), _total(vintage_b)
    delta_pct = abs(total_b - total_a) / abs(total_a) * 100 if total_a else (100.0 if total_b else 0.0)
    return DriftResult(
        metric=metric,
        vintage_a=vintage_a,
        vintage_b=vintage_b,
        total_a=total_a,
        total_b=total_b,
        delta_pct=delta_pct,
        drift_flag=delta_pct > threshold_pct,
    )


def drift_result_to_json(result: DriftResult) -> str:
    return json.dumps(
        {
            "metric": result.metric,
            "vintage_a": result.vintage_a,
            "vintage_b": result.vintage_b,
            "total_a": result.total_a,
            "total_b": result.total_b,
            "delta_pct": result.delta_pct,
            "drift_flag": result.drift_flag,
        },
        default=str,
    )
