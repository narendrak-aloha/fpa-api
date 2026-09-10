"""The bridge waterfall read endpoints the Vue interface polls: a
variance_report's rollup lines (legs + residual, never rounded away, plus
server-computed waterfall steps) and a drill-through from a line down to the
exact cube rows it cited -- compiled with the caller's row scope.
"""

import os

import clickhouse_connect
import pytest

from fpa_be.bridge.decompose import BridgeLeg, BridgeResult
from fpa_be.bridge.persist import persist_bridge

pytestmark = pytest.mark.asyncio

ALICE = {"x-api-key": "alice-planner-key"}  # group-wide scope
PL_PLANNER = {"x-api-key": "pl-planner-key"}  # RTPL1 only


async def _plan(client, code):
    return (await client.post("/plan-versions", json={"plan_code": code}, headers=ALICE)).json()


async def _seed_report(app_conn, plan_version_id, cited_row_keys=("deadbeefcafebabe",), vintage=1):
    result = BridgeResult(
        plan_total=100_000.0,
        reported_actual_total=141_431.19,
        legs=(BridgeLeg("Price", 30_000.0), BridgeLeg("Volume", 11_431.19)),
    )
    return await persist_bridge(
        app_conn,
        plan_version_id=str(plan_version_id),
        cut_label="2026-Q2",
        dimension_filter={"geo_country": "PL"},
        vintage=vintage,
        created_by="alice",
        result=result,
        rollup_path="root",
        cited_row_keys=list(cited_row_keys),
    )


def _us_signature_hash() -> str:
    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username="default",
        password="fpa",
        database="fpa_cube",
    )
    try:
        return client.query(
            "SELECT lower(hex(dim_signature_hash)) FROM fact_gl_actual WHERE company = 'RTUS1' LIMIT 1"
        ).result_rows[0][0]
    finally:
        client.close()


async def test_reports_require_an_api_key(client):
    plan = await _plan(client, "PV-VAR0")
    assert (await client.get(f"/variance-reports/{plan['id']}")).status_code == 401


async def test_list_variance_reports_returns_legs_residual_and_waterfall(client, app_conn):
    plan = await _plan(client, "PV-VAR")
    await _seed_report(app_conn, plan["id"])

    resp = await client.get(f"/variance-reports/{plan['id']}", headers=ALICE)
    assert resp.status_code == 200
    reports = resp.json()
    assert len(reports) == 1
    line = reports[0]["lines"][0]
    assert line["legs"]["price"] == 30_000.0
    assert line["legs"]["volume"] == 11_431.19
    assert line["group_gap"] == pytest.approx(41_431.19)
    assert line["residual"] == pytest.approx(0.0, abs=0.01)
    assert line["cited_rows"] == ["deadbeefcafebabe"]

    steps = line["waterfall"]
    assert [s["label"] for s in steps] == [
        "volume", "practice_mix", "grade_mix", "price", "efficiency", "rate", "fx", "residual",
    ]
    assert steps[0]["start"] == 0.0
    assert all(a["end"] == pytest.approx(b["start"]) for a, b in zip(steps, steps[1:]))
    assert steps[-1]["end"] == pytest.approx(line["group_gap"], abs=0.01)


async def test_drill_through_unknown_line_is_404(client):
    resp = await client.get("/variance-reports/lines/999999999/drill-through", headers=ALICE)
    assert resp.status_code == 404


async def test_drill_through_returns_vintage_and_rows(client, app_conn):
    plan = await _plan(client, "PV-VAR2")
    await _seed_report(app_conn, plan["id"])

    reports = (await client.get(f"/variance-reports/{plan['id']}", headers=ALICE)).json()
    line_id = reports[0]["lines"][0]["id"]

    resp = await client.get(f"/variance-reports/lines/{line_id}/drill-through", headers=ALICE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["vintage"] == 1
    assert body["rows"] == []


async def test_drill_through_only_returns_rows_in_the_callers_scope(client, app_conn):
    plan = await _plan(client, "PV-VAR3")
    await _seed_report(app_conn, plan["id"], cited_row_keys=(_us_signature_hash(),), vintage=0)
    line_id = (await client.get(f"/variance-reports/{plan['id']}", headers=ALICE)).json()[0]["lines"][0]["id"]

    group = (await client.get(f"/variance-reports/lines/{line_id}/drill-through", headers=ALICE)).json()
    poland_only = (await client.get(f"/variance-reports/lines/{line_id}/drill-through", headers=PL_PLANNER)).json()

    company_idx = group["columns"].index("company")
    assert "RTUS1" in {row[company_idx] for row in group["rows"]}
    assert {row[company_idx] for row in poland_only["rows"]} <= {"RTPL1"}
