"""Persisting a decomposed bridge into variance_report/variance_report_line
against the live seeded Postgres -- the Closed-requires-human enforcement
these tests rely on is Phase 2's DB-level CHECK, not re-tested here (see
tests/governance/test_variance_report.py for that).
"""

import pytest_asyncio

from fpa_be.bridge.decompose import BridgeLeg, BridgeResult
from fpa_be.bridge.persist import append_rollup_line, persist_bridge

pytestmark = __import__("pytest").mark.asyncio

TRUNCATE_TABLES = ("variance_report_line", "variance_report", "plan_version_line", "plan_driver", "plan_version")


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(superuser_conn):
    await superuser_conn.execute(f"TRUNCATE {', '.join(TRUNCATE_TABLES)} RESTART IDENTITY CASCADE")
    yield


async def _insert_plan(conn):
    row = await conn.fetchrow(
        "INSERT INTO plan_version (plan_code, requested_by) VALUES ($1, $2) RETURNING id",
        "PV-BRIDGE-TEST",
        "alice",
    )
    return row["id"]


def _material_result():
    return BridgeResult(
        plan_total=100_000.0,
        reported_actual_total=93_000.0,
        legs=(
            BridgeLeg("Volume", -3000.0),
            BridgeLeg("Mix(practice)", -1000.0),
            BridgeLeg("Mix(grade)", -500.0),
            BridgeLeg("Price", -2000.0),
            BridgeLeg("FX", -500.0),
        ),
    )


def _immaterial_result():
    return BridgeResult(
        plan_total=100_000.0,
        reported_actual_total=100_050.0,
        legs=(
            BridgeLeg("Volume", 20.0),
            BridgeLeg("Mix(practice)", 10.0),
            BridgeLeg("Mix(grade)", 5.0),
            BridgeLeg("Price", 10.0),
            BridgeLeg("FX", 5.0),
        ),
    )


class TestPersistBridge:
    async def test_writes_report_and_line_citing_vintage_and_rows(self, superuser_conn):
        plan_id = await _insert_plan(superuser_conn)
        report_id = await persist_bridge(
            superuser_conn, str(plan_id), "Poland Q2", {"geo_country": "PL"}, 1, "alice",
            _material_result(), "root", ["k1", "k2"],
        )
        report = await superuser_conn.fetchrow("SELECT * FROM variance_report WHERE id = $1", report_id)
        assert report["vintage"] == 1
        assert report["cut_label"] == "Poland Q2"

        line = await superuser_conn.fetchrow("SELECT * FROM variance_report_line WHERE report_id = $1", report_id)
        assert line["rollup_path"] == "root"
        assert line["volume"] == -3000.0
        assert line["fx"] == -500.0
        assert line["residual"] == 0.0
        assert line["cited_rows"] == '["k1", "k2"]'

    async def test_material_gap_flips_state_to_investigating_never_closed(self, superuser_conn):
        plan_id = await _insert_plan(superuser_conn)
        report_id = await persist_bridge(
            superuser_conn, str(plan_id), "Poland Q2", {}, 1, "alice",
            _material_result(), "root", ["k1"],
        )
        report = await superuser_conn.fetchrow("SELECT state, advanced_by FROM variance_report WHERE id = $1", report_id)
        assert report["state"] == "Investigating"
        assert report["advanced_by"] == "agent"

    async def test_immaterial_gap_leaves_report_open(self, superuser_conn):
        plan_id = await _insert_plan(superuser_conn)
        report_id = await persist_bridge(
            superuser_conn, str(plan_id), "Poland Q2", {}, 1, "alice",
            _immaterial_result(), "root", ["k1"],
        )
        report = await superuser_conn.fetchrow("SELECT state FROM variance_report WHERE id = $1", report_id)
        assert report["state"] == "Open"

    async def test_multiple_rollup_nodes_append_to_the_same_report(self, superuser_conn):
        plan_id = await _insert_plan(superuser_conn)
        report_id = await persist_bridge(
            superuser_conn, str(plan_id), "Poland Q2", {}, 1, "alice",
            _material_result(), "root", ["k1"],
        )
        await append_rollup_line(superuser_conn, report_id, _material_result(), "root/PL", ["k1"])
        rows = await superuser_conn.fetch("SELECT rollup_path FROM variance_report_line WHERE report_id = $1", report_id)
        assert {r["rollup_path"] for r in rows} == {"root", "root/PL"}
