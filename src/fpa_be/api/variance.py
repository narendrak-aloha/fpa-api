"""Read endpoints for the bridge waterfall the Vue interface renders:
variance_report + its rollup lines (Phase 6/8's compute_variance activity
writes these), and a drill-through from a bridge line down to the exact
cube rows (by dim_signature_hash) and vintage that produced it.
"""

import json
import os
import uuid

import asyncpg
import clickhouse_connect
from fastapi import APIRouter, HTTPException, Request

from fpa_be.compiler.fact_source import actual_fact_source

router = APIRouter(prefix="/variance-reports", tags=["variance-reports"])

_LEG_COLUMNS = ("price", "volume", "practice_mix", "grade_mix", "fx", "rate", "efficiency")


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.app_pool


def _serialize_line(row: asyncpg.Record) -> dict:
    return {
        "id": row["id"],
        "rollup_path": row["rollup_path"],
        "group_gap": float(row["group_gap"]),
        "legs": {leg: float(row[leg]) for leg in _LEG_COLUMNS},
        "residual": float(row["residual"]),
        "tol": float(row["tol"]),
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
async def list_variance_reports(plan_version_id: uuid.UUID, request: Request):
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
async def drill_through(line_id: int, request: Request):
    """The exact cube rows (plan + actual, at the vintage the bridge read)
    behind one bridge line's cited_rows (dim_signature_hash keys)."""
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
        return {"vintage": line["vintage"], "rows": []}

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "fpa"),
        database=os.environ.get("CLICKHOUSE_DATABASE", "fpa_cube"),
    )
    # persist_bridge stores 0 (not NULL) for "vintage unresolved at write time"
    # -- actual_fact_source's "latest" sentinel is None, not 0.
    source = actual_fact_source(line["vintage"] or None)
    try:
        result = client.query(
            f"SELECT company, period_month, account, dim_signature_hash, quantity, amount_functional "
            f"FROM {source} WHERE dim_signature_hash IN {{hashes:Array(String)}}",
            parameters={"hashes": cited_rows},
        )
    finally:
        client.close()
    return {
        "vintage": line["vintage"],
        "columns": list(result.column_names),
        "rows": [list(row) for row in result.result_rows],
    }
