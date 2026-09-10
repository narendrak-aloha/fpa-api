"""Writes a decomposed bridge (BridgeResult per rollup node) into the
`variance_report`/`variance_report_line` tables (Phase 2's plan spine),
citing the exact cube rows and vintage the bridge read. A gap over the
materiality threshold flips the report towards `Investigating` -- an agent
can do that much, but `Closed` is enforced human-only by
`ck_variance_report_close_requires_human` at the database layer, not
trusted to this module.
"""

import json

import asyncpg

from fpa_be.bridge.decompose import BridgeResult

MATERIALITY_THRESHOLD = 5000.00

# BridgeResult leg names -> variance_report_line's fixed columns. Revenue
# and cost bridges use disjoint leg-name sets, so a single row shape covers
# both -- whichever legs a given bridge doesn't produce are left at their
# zero default.
_LEG_COLUMNS = {
    "Price": "price",
    "Volume": "volume",
    "Mix(practice)": "practice_mix",
    "Mix(grade)": "grade_mix",
    "FX": "fx",
    "Rate": "rate",
    "Efficiency": "efficiency",
}


def _tolerance(line_count: int) -> float:
    return max(1.00, 0.01 * line_count)


async def persist_bridge(
    conn: asyncpg.Connection,
    plan_version_id: str,
    cut_label: str,
    dimension_filter: dict,
    vintage: int | None,
    created_by: str,
    result: BridgeResult,
    rollup_path: str,
    cited_row_keys: list[str],
) -> str:
    """Inserts one variance_report (if this is the first rollup node for
    this cut) plus one variance_report_line for `rollup_path`, and returns
    the report id so callers can reuse it across sibling rollup nodes for
    the same cut.
    """
    report_id = await conn.fetchval(
        """
        INSERT INTO variance_report
            (plan_version_id, cut_label, dimension_filter, vintage, created_by)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        plan_version_id,
        cut_label,
        json.dumps(dimension_filter),
        vintage if vintage is not None else 0,
        created_by,
    )
    await append_rollup_line(conn, report_id, result, rollup_path, cited_row_keys)
    return report_id


async def append_rollup_line(
    conn: asyncpg.Connection,
    report_id: str,
    result: BridgeResult,
    rollup_path: str,
    cited_row_keys: list[str],
) -> None:
    """Appends a variance_report_line for one rollup node of an existing
    report, and flips the report to Investigating if the node's gap clears
    the materiality threshold -- never to Closed, which stays human-only.
    """
    group_gap = result.reported_actual_total - result.plan_total
    columns = {name: 0.0 for name in _LEG_COLUMNS.values()}
    for leg in result.legs:
        column = _LEG_COLUMNS.get(leg.name)
        if column is not None:
            columns[column] = leg.amount
    tol = _tolerance(len(cited_row_keys))

    await conn.execute(
        """
        INSERT INTO variance_report_line
            (report_id, rollup_path, group_gap, price, volume, practice_mix,
             grade_mix, fx, rate, efficiency, residual, tol, cited_rows)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        """,
        report_id,
        rollup_path,
        group_gap,
        columns["price"],
        columns["volume"],
        columns["practice_mix"],
        columns["grade_mix"],
        columns["fx"],
        columns["rate"],
        columns["efficiency"],
        result.residual,
        tol,
        json.dumps(cited_row_keys),
    )

    if abs(group_gap) >= MATERIALITY_THRESHOLD:
        await conn.execute(
            """
            UPDATE variance_report
            SET state = 'Investigating', advanced_by = 'agent', updated_at = now()
            WHERE id = $1 AND state = 'Open'
            """,
            report_id,
        )
