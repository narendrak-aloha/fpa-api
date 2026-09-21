"""Compiled SQL against the real cube.

These are the assertions that cannot be made about SQL text: that ClickHouse
prunes partitions for a period predicate, that ``AS OF`` returns the books as
that close saw them, that a time function produces a real monthly series.
Skipped, not failed, when the cube is not up.
"""

from __future__ import annotations

import pytest

from fpa_project.config import clickhouse as clickhouse_settings
from fpa_project.dsl.compiler import SecurityContext, compile_query, vintage_lookup

pytestmark = pytest.mark.integration


def _client():
    import clickhouse_connect

    settings = clickhouse_settings()
    return clickhouse_connect.get_client(
        host=settings.host, port=settings.port, username=settings.user, password=settings.password,
        connect_timeout=2,
    )


try:
    _client().query("SELECT 1")
except Exception:  # noqa: BLE001 - any failure means no cube
    pytest.skip("needs ClickHouse: run `make docker-local-run-d`", allow_module_level=True)


@pytest.fixture(scope="module")
def cube():
    return _client()


def run(cube, dsl: str, scope: SecurityContext | None = None):
    compiled = compile_query(dsl, security_context=scope)
    result = cube.query(compiled.sql, parameters=compiled.params)
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


PL_Q2 = "WHERE geo_country = 'PL' FOR PERIOD 2026-Q2"


def test_period_predicate_prunes_partitions(cube):
    """Asserted against EXPLAIN indexes=1, not against having thought about it."""
    compiled = compile_query(f"SELECT services_revenue BY practice {PL_Q2}")
    plan = "\n".join(row[0] for row in cube.query("EXPLAIN indexes=1 " + compiled.sql, parameters=compiled.params).result_rows)
    assert "ReadFromMergeTree (fpa_cube.fact_gl_actual)" in plan
    # The partition key is toYYYYMM(period_month); the plan must show the
    # period condition reaching it, and fewer parts read than exist.
    assert "toYYYYMM(period_month)" in plan
    minmax = plan.split("Min-Max", 1)[1].split("Parts:", 1)[1].split("\n", 1)[0].strip()
    selected, total = (int(x) for x in minmax.split("/"))
    assert selected < total, f"no pruning: read {selected}/{total} parts"


def test_as_of_reads_the_books_as_that_close_saw_them(cube):
    """The August restatement pushed Poland's Q2 delivery cost up; July must not see it."""
    current = run(cube, f"SELECT delivery_cost {PL_Q2}")[0]["delivery_cost"]
    july = run(cube, f"SELECT delivery_cost {PL_Q2} AS OF '2026-07-05T18:00:00'")[0]["delivery_cost"]
    august = run(cube, f"SELECT delivery_cost {PL_Q2} AS OF '2026-08-12T09:30:00'")[0]["delivery_cost"]
    assert august == current, "a read at the latest close must equal a current read"
    assert july < current
    assert 1.10 < float(current) / float(july) < 1.25, "the restatement is roughly a fifth, not noise and not double"


def test_as_of_before_the_first_close_reads_nothing_and_names_no_vintage(cube):
    rows = run(cube, f"SELECT delivery_cost {PL_Q2} AS OF '2026-01-01T00:00:00'")
    assert rows[0]["delivery_cost"] == 0
    sql, params = vintage_lookup("2026-01-01T00:00:00")
    assert cube.query(sql, parameters=params).result_rows == []


def test_time_functions_produce_a_monthly_series(cube):
    rows = run(cube, "SELECT YOY(services_revenue), PRIOR(services_revenue, 1) AS prior, services_revenue BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
    assert len(rows) == 6  # 2 practices x 3 months
    assert all(row["yoy_services_revenue"] is not None for row in rows)
    months = sorted({row["period_month"].isoformat() for row in rows})
    assert months == ["2026-04-01", "2026-05-01", "2026-06-01"]


def test_rolling_ties_to_its_own_months(cube):
    rows = run(cube, "SELECT ROLLING(services_revenue, 3) AS rolling, services_revenue BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-02..2026-04")
    by_practice: dict[str, list] = {}
    for row in rows:
        by_practice.setdefault(row["practice"], []).append(row)
    for series in by_practice.values():
        series.sort(key=lambda r: r["period_month"])
        assert series[-1]["rolling"] == sum(r["services_revenue"] for r in series)


def test_row_scope_returns_less_because_of_who_asked(cube):
    # Every entity carries PL-tagged rows: the intercompany mirror on 51500 is
    # booked on the buying entity with the trade's dimensions. So the unscoped
    # read is wide, and the scoped one is exactly the caller's entity.
    everyone = run(cube, f"SELECT services_revenue BY company {PL_Q2}")
    one_entity = run(cube, f"SELECT services_revenue BY company {PL_Q2}", SecurityContext(frozenset({"RTPL1"})))
    assert len(everyone) > 3
    assert [row["company"] for row in one_entity] == ["RTPL1"]
    assert one_entity[0]["services_revenue"] > 0
    assert run(cube, f"SELECT services_revenue BY company {PL_Q2}", SecurityContext(frozenset())) == []


def test_bridge_lines_are_matched_at_signature_grain(cube):
    rows = run(cube, f"SELECT services_revenue BY practice, grade {PL_Q2} COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE")
    assert rows, "the Poland Q2 matched set is not empty"
    keys = {(r["a.company"], r["a.period_month"], r["a.account"], r["a.dim_signature_hash"]) for r in rows}
    assert len(keys) == len(rows)
    row = rows[0]
    assert row["plan_fx"] != row["actual_fx"], "the assumed rate is not the real rate"
    assert isinstance(row["a.dim_signature_hash"], (str, bytes))
