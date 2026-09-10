"""The bridge waterfall read endpoints the Vue interface polls: a
variance_report's rollup lines (legs + residual, never rounded away) and a
drill-through from a line down to the exact cube rows it cited.
"""

import pytest

from fpa_be.bridge.decompose import BridgeLeg, BridgeResult
from fpa_be.bridge.persist import persist_bridge

pytestmark = pytest.mark.asyncio


async def _seed_report(app_conn, plan_version_id):
    result = BridgeResult(
        plan_total=100_000.0,
        reported_actual_total=141_431.19,
        legs=(BridgeLeg("Price", 30_000.0), BridgeLeg("Volume", 11_431.19)),
    )
    report_id = await persist_bridge(
        app_conn,
        plan_version_id=str(plan_version_id),
        cut_label="2026-Q2",
        dimension_filter={"geo_country": "PL"},
        vintage=1,
        created_by="alice",
        result=result,
        rollup_path="root",
        cited_row_keys=["deadbeefcafebabe"],
    )
    return report_id


async def test_list_variance_reports_returns_legs_and_residual(client, app_conn):
    plan = (
        await client.post("/plan-versions", json={"plan_code": "PV-VAR", "requested_by": "alice"})
    ).json()
    await _seed_report(app_conn, plan["id"])

    resp = await client.get(f"/variance-reports/{plan['id']}")
    assert resp.status_code == 200
    reports = resp.json()
    assert len(reports) == 1
    line = reports[0]["lines"][0]
    assert line["legs"]["price"] == 30_000.0
    assert line["legs"]["volume"] == 11_431.19
    assert line["group_gap"] == pytest.approx(41_431.19)
    assert line["residual"] == pytest.approx(0.0, abs=0.01)
    assert line["cited_rows"] == ["deadbeefcafebabe"]


async def test_drill_through_unknown_line_is_404(client):
    resp = await client.get("/variance-reports/lines/999999999/drill-through")
    assert resp.status_code == 404


async def test_drill_through_returns_vintage_and_rows(client, app_conn):
    plan = (
        await client.post("/plan-versions", json={"plan_code": "PV-VAR2", "requested_by": "alice"})
    ).json()
    await _seed_report(app_conn, plan["id"])

    reports = (await client.get(f"/variance-reports/{plan['id']}")).json()
    line_id = reports[0]["lines"][0]["id"]

    resp = await client.get(f"/variance-reports/lines/{line_id}/drill-through")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vintage"] == 1
    assert body["rows"] == []
