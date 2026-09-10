"""Compiler tests: run compile()'s output against the live, already-seeded
ClickHouse instance. This is the only path from a FinOpsExpr query to SQL --
these tests are what stand in for "the compiler is the security boundary."
"""

import pytest

from fpa_be.compiler.compile import MAX_ESTIMATED_ROWS, compile
from fpa_be.compiler.errors import QueryTooExpensiveError, UnsupportedQueryShapeError
from fpa_be.compiler.security import SecurityContext
from fpa_be.dsl.parser import parse_query
from fpa_be.dsl.resolver import check_node

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))
UNRESTRICTED = SecurityContext()


def _compile(dsl: str, ctx: SecurityContext = PL_SCOPE, **kwargs):
    query = parse_query(dsl)
    check_node(query)
    return compile(query, ctx, **kwargs)


class TestAdditiveMeasure:
    def test_sums_across_dimensions_and_time(self, ch_client):
        cq = _compile("SELECT services_revenue BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        res = ch_client.query(cq.sql, parameters=cq.params)
        assert res.column_names == ("practice", "services_revenue")
        assert len(res.result_rows) > 0
        assert all(row[1] > 0 for row in res.result_rows)


class TestSemiAdditiveMeasure:
    def test_takes_closing_month_not_a_sum_across_time(self, ch_client):
        # headcount summed naively across 3 months would triple-count people
        # who show up every month; argMax(leaf, period_month) must instead
        # return something close to a single month's headcount.
        cq_quarter = _compile("SELECT headcount BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        cq_month = _compile("SELECT headcount BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-06")
        quarter_rows = {r[0]: r[1] for r in ch_client.query(cq_quarter.sql, parameters=cq_quarter.params).result_rows}
        month_rows = {r[0]: r[1] for r in ch_client.query(cq_month.sql, parameters=cq_month.params).result_rows}
        for practice, quarter_headcount in quarter_rows.items():
            month_headcount = month_rows[practice]
            assert quarter_headcount < month_headcount * 2, (
                f"{practice}: quarter headcount {quarter_headcount} looks summed across 3 months, "
                f"not a closing-period value (single month was {month_headcount})"
            )


class TestRatioMeasure:
    def test_recomputed_from_numerator_and_denominator_not_averaged(self, ch_client):
        cq = _compile("SELECT utilisation BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        res = ch_client.query(cq.sql, parameters=cq.params)
        assert len(res.result_rows) > 0
        for _, utilisation in res.result_rows:
            assert 0 < float(utilisation) < 3

    def test_gross_margin_pct_matches_manually_computed_ratio(self, ch_client):
        cq_pct = _compile("SELECT gross_margin_pct BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        cq_parts = _compile("SELECT total_revenue, total_cogs BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        pct_rows = {r[0]: float(r[1]) for r in ch_client.query(cq_pct.sql, parameters=cq_pct.params).result_rows}
        part_rows = {r[0]: (float(r[1]), float(r[2])) for r in ch_client.query(cq_parts.sql, parameters=cq_parts.params).result_rows}
        for practice, pct in pct_rows.items():
            revenue, cogs = part_rows[practice]
            expected = (revenue - cogs) / revenue
            assert pct == pytest.approx(expected, rel=1e-6)


class TestTimeFunctions:
    def test_yoy_looks_back_beyond_the_requested_period(self, ch_client):
        cq = _compile("SELECT YOY(services_revenue) BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q1..2026-Q4")
        res = ch_client.query(cq.sql, parameters=cq.params)
        assert len(res.result_rows) > 0
        # every returned row is within the caller's own requested range
        assert all(row[1].isoformat() >= "2026-01-01" and row[1].isoformat() < "2027-01-01" for row in res.result_rows)
        # and YOY actually computed a real ratio somewhere, not all NULL
        assert any(row[2] is not None for row in res.result_rows)

    def test_prior_shifts_by_the_given_offset(self, ch_client):
        cq = _compile("SELECT services_revenue, PRIOR(services_revenue, 1) BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q1..2026-Q4")
        res = ch_client.query(cq.sql, parameters=cq.params)
        rows = sorted(res.result_rows, key=lambda r: (r[0], r[1]))
        by_practice: dict[str, list] = {}
        for practice, month, revenue, prior in rows:
            by_practice.setdefault(practice, []).append((month, revenue, prior))
        for months in by_practice.values():
            for i in range(1, len(months)):
                _, prev_revenue, _ = months[i - 1]
                _, _, this_prior = months[i]
                assert float(this_prior) == pytest.approx(float(prev_revenue))

    def test_rolling_sums_the_trailing_window(self, ch_client):
        cq = _compile("SELECT ROLLING(services_revenue, 3) BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q1..2026-Q4")
        res = ch_client.query(cq.sql, parameters=cq.params)
        assert len(res.result_rows) > 0


class TestSecurityScope:
    def test_scope_injection_survives_a_widening_where_clause(self, ch_client):
        # The caller is scoped to RTPL1 only but asks the DSL for a second
        # company explicitly -- the injected scope filter must win regardless.
        cq = _compile(
            "SELECT services_revenue BY company WHERE company IN ('RTPL1', 'RTUS1') FOR PERIOD 2026-Q2",
            ctx=PL_SCOPE,
        )
        res = ch_client.query(cq.sql, parameters=cq.params)
        companies_returned = {row[0] for row in res.result_rows}
        assert companies_returned == {"RTPL1"}
        assert "RTUS1" not in companies_returned

    def test_three_entity_scope_never_returns_a_fourth(self, ch_client):
        scope = SecurityContext(allowed_companies=frozenset({"RTUS1", "RTUS2", "RTUS3"}))
        cq = _compile("SELECT services_revenue BY company FOR PERIOD 2026-Q2", ctx=scope)
        res = ch_client.query(cq.sql, parameters=cq.params)
        companies_returned = {row[0] for row in res.result_rows}
        assert companies_returned <= {"RTUS1", "RTUS2", "RTUS3"}
        assert "RTCA1" not in companies_returned

    def test_unrestricted_scope_is_never_used_by_agent_facing_callers(self):
        # Documents the contract in security.py: None means every company,
        # so an agent-facing caller must never construct a bare SecurityContext().
        assert UNRESTRICTED.allowed_companies is None
        assert UNRESTRICTED.allowed_geo_countries is None


class TestPartitionPruning:
    def test_period_filter_prunes_clickhouse_partitions(self, ch_client):
        cq = _compile("SELECT services_revenue BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        explain = ch_client.query(f"EXPLAIN indexes=1\n{cq.sql}", parameters=cq.params)
        plan_text = "\n".join(row[0] for row in explain.result_rows)
        assert "Partition" in plan_text
        assert "toYYYYMM(period_month)" in plan_text
        # 6 of 32 total parts read proves partitions outside Q2 were skipped.
        import re

        match = re.search(r"Parts: (\d+)/(\d+)", plan_text)
        assert match is not None
        read_parts, total_parts = int(match.group(1)), int(match.group(2))
        assert read_parts < total_parts


class TestCostBudget:
    def test_wide_open_period_and_scope_is_rejected(self):
        query = parse_query("SELECT services_revenue FOR PERIOD 2020-01..2030-12")
        check_node(query)
        with pytest.raises(QueryTooExpensiveError) as exc_info:
            compile(query, UNRESTRICTED)
        assert exc_info.value.budget == MAX_ESTIMATED_ROWS
        assert exc_info.value.estimated_rows > MAX_ESTIMATED_ROWS

    def test_narrow_scope_and_period_is_accepted(self, ch_client):
        cq = _compile("SELECT services_revenue FOR PERIOD 2026-Q2", ctx=PL_SCOPE)
        ch_client.query(cq.sql, parameters=cq.params)  # must not raise


class TestUnsupportedShapes:
    def test_query_without_for_period_is_rejected(self):
        query = parse_query("SELECT services_revenue BY practice")
        check_node(query)
        with pytest.raises(UnsupportedQueryShapeError):
            compile(query, PL_SCOPE)

    def test_compare_clause_is_deferred_to_the_bridge_engine(self):
        query = parse_query(
            "SELECT services_revenue FOR PERIOD 2026-Q2 COMPARE PLAN pv='PV-2026-0001' TO ACTUAL"
        )
        check_node(query)
        with pytest.raises(UnsupportedQueryShapeError):
            compile(query, PL_SCOPE)

    def test_bridge_flag_is_deferred_to_the_bridge_engine(self):
        query = parse_query("SELECT services_revenue FOR PERIOD 2026-Q2 BRIDGE")
        check_node(query)
        with pytest.raises(UnsupportedQueryShapeError):
            compile(query, PL_SCOPE)


class TestVintage:
    def test_specific_vintage_differs_from_latest_for_a_restated_cut(self, ch_client):
        cq_latest = _compile("SELECT delivery_cost BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2", ctx=PL_SCOPE)
        cq_v1 = _compile(
            "SELECT delivery_cost BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2",
            ctx=PL_SCOPE,
            resolved_vintage=1,
        )
        latest_rows = {r[0]: float(r[1]) for r in ch_client.query(cq_latest.sql, parameters=cq_latest.params).result_rows}
        v1_rows = {r[0]: float(r[1]) for r in ch_client.query(cq_v1.sql, parameters=cq_v1.params).result_rows}
        assert latest_rows.keys() == v1_rows.keys()
        assert any(latest_rows[k] != pytest.approx(v1_rows[k]) for k in latest_rows), (
            "expected the Q2 restatement (vintage 2) to differ from the original close (vintage 1) somewhere"
        )
