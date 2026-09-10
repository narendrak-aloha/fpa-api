"""Read endpoints for the bridge waterfall the Vue interface renders:
variance_report + its rollup lines (the compute_variance activity writes
these), and a drill-through from a bridge line down to the exact cube rows
(by dim_signature_hash) and vintage that produced it. The drill-through is
compiled like every other cube read (`compile_drill_through`), with the
authenticated caller's row scope injected.
"""

import json
import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, Request

from fpa_be.api.auth import CurrentPrincipal
from fpa_be.compiler.compile import compile_drill_through
from fpa_be.cube.client import CubeClient

router = APIRouter(prefix="/variance-reports", tags=["variance-reports"])

_LEG_COLUMNS = ("price", "volume", "practice_mix", "grade_mix", "fx", "rate", "efficiency")
# The decomposition's own sequence (docs: bridge convention): the revenue
# legs, then cost's efficiency/rate, then FX; the residual closes the bar.
_WATERFALL_ORDER = ("volume", "practice_mix", "grade_mix", "price", "efficiency", "rate", "fx")

# A rollup node cites every row it bridged -- at the root of the seeded cube
# that's ~65k hashes, which overruns ClickHouse's HTTP field limit if they're
# all bound into one IN clause. Drill-through is a "show me the rows behind
# this number" affordance, not an export, so it returns a bounded sample and
# says how much it left out (`cited_row_count` / `truncated`).
DRILL_THROUGH_MAX_ROWS = 500


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.app_pool


def _waterfall(legs: dict[str, float], residual: float) -> list[dict]:
    """Running start/end per leg, then the residual, from 0 to the group
    gap -- computed here so the UI draws bars without deriving any number."""
    steps, running = [], 0.0
    for label in _WATERFALL_ORDER:
        steps.append({"label": label, "start": running, "end": running + legs[label]})
        running += legs[label]
    steps.append({"label": "residual", "start": running, "end": running + residual})
    return steps


def _serialize_line(row: asyncpg.Record) -> dict:
    legs = {leg: float(row[leg]) for leg in _LEG_COLUMNS}
    residual = float(row["residual"])
    return {
        "id": row["id"],
        "rollup_path": row["rollup_path"],
        "group_gap": float(row["group_gap"]),
        "legs": legs,
        "residual": residual,
        "tol": float(row["tol"]),
        "waterfall": _waterfall(legs, residual),
        "cited_rows": json.loads(row["cited_rows"]) if isinstance(row["cited_rows"], str) else row["cited_rows"],
    }


def _serialize_report(row: asyncpg.Record) -> dict:
    return {
        "id": str(row["id"]),
        "plan_version_id": str(row["plan_version_id"]),
        "cut_label": row["cut_label"],
        "vintage": row["vintage"],
        "state": row["state"],
        "materiality_flag": row["materiality_flag"],
    }


@router.get("/{plan_version_id}")
async def list_variance_reports(
    plan_version_id: uuid.UUID, request: Request, principal: CurrentPrincipal
):
    """Every variance report for a plan version, each with its rollup lines
    (bridge legs + residual, never rounded away) -- the waterfall the UI
    draws."""
    async with _pool(request).acquire() as conn:
        reports = await conn.fetch(
            "SELECT * FROM variance_report WHERE plan_version_id = $1 ORDER BY created_at", plan_version_id
        )
        result = []
        for report in reports:
            lines = await conn.fetch(
                "SELECT * FROM variance_report_line WHERE report_id = $1 ORDER BY rollup_path", report["id"]
            )
            payload = _serialize_report(report)
            payload["lines"] = [_serialize_line(line) for line in lines]
            result.append(payload)
        return result


@router.get("/lines/{line_id}/drill-through")
async def drill_through(line_id: int, request: Request, principal: CurrentPrincipal):
    """The cube rows behind one bridge line's cited_rows, at the vintage the
    bridge read, limited to what the caller's scope allows."""
    async with _pool(request).acquire() as conn:
        line = await conn.fetchrow(
            "SELECT vl.*, r.vintage FROM variance_report_line vl "
            "JOIN variance_report r ON r.id = vl.report_id WHERE vl.id = $1",
            line_id,
        )
    if line is None:
        raise HTTPException(status_code=404, detail="variance_report_line not found")

    cited_rows = json.loads(line["cited_rows"]) if isinstance(line["cited_rows"], str) else line["cited_rows"]
    if not cited_rows:
        return {"vintage": line["vintage"], "rows": [], "cited_row_count": 0, "truncated": False}

    sample = cited_rows[:DRILL_THROUGH_MAX_ROWS]
    # cited_rows are hex-encoded dim_signature_hash values (bytes aren't
    # JSON-serializable for the jsonb column); FixedString comparison needs
    # the raw bytes back. persist_bridge stores vintage 0 for "latest", whose
    # compiler sentinel is None.
    compiled = compile_drill_through(
        [bytes.fromhex(h) for h in sample],
        line["vintage"] or None,
        principal.security_context,
        DRILL_THROUGH_MAX_ROWS,
    )
    cube = CubeClient()
    try:
        result = cube.execute(compiled)
    finally:
        cube.close()
    rows = [list(row) for row in result.result_rows]
    return {
        "vintage": line["vintage"],
        "columns": list(result.column_names),
        "rows": rows,
        "cited_row_count": len(cited_rows),
        "truncated": len(cited_rows) > len(sample) or len(rows) >= DRILL_THROUGH_MAX_ROWS,
    }
