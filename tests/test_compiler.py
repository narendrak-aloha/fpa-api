import pytest

from fpa_project.dsl.compiler import SecurityContext, compile_query
from fpa_project.dsl.errors import DSLValidationError


def test_compiles_parameterised_clickhouse_sql():
    compiled = compile_query(
        "SELECT services_revenue, gross_margin_pct AS margin "
        "BY company, practice "
        "WHERE geo_country = 'PL' "
        "FOR PERIOD 2026-Q2 LIMIT 10"
    )
    assert "FROM fpa_cube.fact_gl_actual AS a FINAL" in compiled.sql
    assert "GROUP BY company, practice" in compiled.sql
    assert "toDate({p1:String})" in compiled.sql
    assert "{p0:String}" in compiled.sql
    assert "PL" not in compiled.sql
    assert compiled.params["p0"] == "PL"
    assert compiled.params["p1"] == "2026-04-01"
    assert compiled.params["p2"] == "2026-07-01"


def test_plan_compiles_to_plan_fact_and_scenario_predicates():
    compiled = compile_query(
        "SELECT delivery_cost BY geo_country "
        "FOR PERIOD 2026-H1 "
        "COMPARE PLAN pv='PV-2026-0001', scenario='stretch' TO ACTUAL"
    )
    assert "fact_plan_line AS p" in compiled.sql
    assert "p.plan_version =" in compiled.sql
    assert "p.scenario_id =" in compiled.sql
    assert "PV-2026-0001" in compiled.params.values()
    assert "stretch" in compiled.params.values()


def test_time_function_is_compiled():
    compiled = compile_query("SELECT YOY(services_revenue) BY practice FOR PERIOD 2026-Q2")
    assert "lagInFrame" in compiled.sql
    assert "OVER" in compiled.sql


@pytest.mark.parametrize(
    "source, message",
    [
        ("SELECT services_revenue BY not_a_dimension", "unknown dimension"),
        ("SELECT not_a_measure", "unknown measure"),
        ("SELECT services_revenue WHERE not_a_field = 'x'", "unknown predicate field"),
        (
            "SELECT services_revenue COMPARE PLAN pv='PV-1', scenario='unknown' TO ACTUAL",
            "unknown scenario",
        ),
    ],
)
def test_schema_validation_errors(source, message):
    with pytest.raises(DSLValidationError, match=message):
        compile_query(source)


def test_values_are_not_interpolated_into_sql():
    compiled = compile_query("SELECT services_revenue WHERE customer = 'Robert''; DROP TABLE users' LIMIT 100")
    assert "DROP TABLE" not in compiled.sql
    assert compiled.params["p0"] == "Robert'; DROP TABLE users"


def test_or_predicate_is_preserved():
    compiled = compile_query(
        "SELECT services_revenue WHERE geo_country = 'PL' OR engine = 'Services' LIMIT 100"
    )
    assert "({p0:String} IS NOT NULL" not in compiled.sql
    assert " OR " in compiled.sql


def test_security_scope_is_injected():
    compiled = compile_query(
        "SELECT services_revenue BY company",
        security_context=SecurityContext(frozenset({"C001", "C002"})),
    )
    assert "a.company IN ({p0:String}, {p1:String})" in compiled.sql
    assert set(compiled.params.values()) == {"C001", "C002"}


def test_query_budget_is_enforced():
    with pytest.raises(DSLValidationError, match="row budget"):
        compile_query(
            "SELECT services_revenue",
            security_context=SecurityContext(max_estimated_rows=10),
        )


def test_as_of_is_parameterised_and_reported():
    compiled = compile_query("SELECT delivery_cost FOR PERIOD 2026-Q2 AS OF '2026-07-05T18:00:00'")
    assert "_version =" in compiled.sql
    assert compiled.vintage == "2026-07-05T18:00:00"


def test_bridge_compiles_matched_plan_actual_sql():
    compiled = compile_query(
        "SELECT services_revenue BY practice FOR PERIOD 2026-Q2 "
        "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE"
    )
    assert "FROM fpa_cube.fact_gl_actual AS a FINAL" in compiled.sql
    assert "INNER JOIN fpa_cube.fact_plan_line" in compiled.sql
    assert "dim_signature_hash" in compiled.sql
    assert "price_variance" in compiled.sql
    assert "volume_variance" in compiled.sql
    assert "residual" in compiled.sql


def test_unavailable_seed_measure_is_rejected():
    with pytest.raises(DSLValidationError, match="not available"):
        compile_query("SELECT open_pipeline")


def test_semi_additive_measure_cannot_span_time_without_closing_rule():
    with pytest.raises(DSLValidationError, match="closing period"):
        compile_query("SELECT headcount FOR PERIOD 2026-Q1..2026-Q2")


def test_as_of_cannot_be_silently_ignored_for_plan_reads():
    with pytest.raises(DSLValidationError, match="only valid for actual-ledger"):
        compile_query("SELECT delivery_cost FOR PERIOD 2026-Q2 AS OF '2026-07-05T18:00:00' COMPARE PLAN pv='PV-1'")


def test_reverse_period_range_is_rejected():
    with pytest.raises(DSLValidationError, match="period range"):
        compile_query("SELECT services_revenue FOR PERIOD 2026-Q3..2026-Q2")


def test_alias_is_checked_before_sql_generation():
    compiled = compile_query("SELECT services_revenue AS safe_alias")
    assert "AS safe_alias" in compiled.sql


def test_security_context_rejects_invalid_budget():
    with pytest.raises(ValueError, match="max_estimated_rows"):
        SecurityContext(max_estimated_rows=-1)
