"""The exactly-four agent tools, end to end against live ClickHouse/Postgres:
`list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver`.
"""

import json

import asyncpg
import pytest

from fpa_be.agents.tools import (
    ALL_TOOLS,
    NoSecurityContextError,
    list_dimensions,
    list_metrics,
    propose_driver,
    run_finops_query,
)
from fpa_be.db import app_dsn
from fpa_be.registry.dimensions import DIM_COLUMNS, SEPARATE_AXES

from .conftest import make_run_context


class TestExactlyFourTools:
    def test_no_run_sql_tool_exists(self):
        names = {t.name for t in ALL_TOOLS}
        assert names == {"list_metrics", "list_dimensions", "run_finops_query", "propose_driver"}


class TestSecurityContextRequired:
    @pytest.mark.asyncio
    async def test_run_finops_query_without_security_context_raises(self):
        rc = make_run_context()
        rc.dependencies = {}
        with pytest.raises(NoSecurityContextError):
            await run_finops_query.entrypoint(dsl="SELECT services_revenue", run_context=rc)


class TestListMetrics:
    def test_returns_public_measures(self, run_context):
        result = json.loads(list_metrics.entrypoint(run_context=run_context))
        assert len(result) > 0
        assert {"name", "description", "kind"} <= result[0].keys()


class TestListDimensions:
    def test_returns_registry_dimensions(self, run_context):
        result = json.loads(list_dimensions.entrypoint(run_context=run_context))
        assert set(result["dimensions"]) == set(DIM_COLUMNS)
        assert set(result["separate_axes"]) == set(SEPARATE_AXES)


class TestRunFinopsQuery:
    @pytest.mark.asyncio
    async def test_valid_query_against_live_cube(self, run_context):
        dsl = "SELECT services_revenue WHERE geo_country = 'PL' FOR PERIOD 2026-Q2"
        result = json.loads(await run_finops_query.entrypoint(dsl=dsl, run_context=run_context))
        assert result["dsl"] == dsl
        assert len(result["rows"]) > 0

    @pytest.mark.asyncio
    async def test_malformed_dsl_returns_error_not_exception(self, run_context):
        result = json.loads(await run_finops_query.entrypoint(dsl="SELECT not_a_real_metric", run_context=run_context))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_scope_widening_is_rejected_server_side(self, run_context):
        """A caller scoped to Poland (RTPL1) asking for Germany gets a
        compiled query narrowed to their scope regardless of the WHERE they
        wrote -- never another entity's real rows."""
        result = json.loads(
            await run_finops_query.entrypoint(
                dsl="SELECT services_revenue WHERE geo_country = 'DE' FOR PERIOD 2026-Q2", run_context=run_context
            )
        )
        if "error" not in result:
            assert all(float(row[-1]) == 0 for row in result["rows"])

    @pytest.mark.asyncio
    async def test_customer_dimension_is_masked_and_disclosure_logged(self, run_context):
        dsl = "SELECT services_revenue BY customer WHERE geo_country = 'PL' FOR PERIOD 2026-Q2"
        result = json.loads(await run_finops_query.entrypoint(dsl=dsl, run_context=run_context))
        assert "error" not in result
        customer_idx = result["columns"].index("customer")
        assert all(str(row[customer_idx]).startswith("[customer_identity:") for row in result["rows"])

        conn = await asyncpg.connect(app_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT classes, methods FROM llm_disclosure_log WHERE tool_name = 'run_finops_query' "
                "ORDER BY id DESC LIMIT 1"
            )
        finally:
            await conn.close()
        assert "customer_identity" in row["classes"]
        assert "tokenize" in row["methods"]


class TestProposeDriver:
    @pytest.mark.asyncio
    async def test_malformed_formula_returns_error_not_exception(self, run_context):
        result = json.loads(
            await propose_driver.entrypoint(
                name="broken_driver",
                formula="heads * (",
                effective_date="2026-01-01",
                run_context=run_context,
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_insert_then_update_against_live_postgres(self, run_context):
        try:
            first = json.loads(
                await propose_driver.entrypoint(
                    name="pytest_bill_rate",
                    formula="150 * utilisation",
                    effective_date="2026-01-01",
                    run_context=run_context,
                )
            )
            assert first["proposed_driver"] == "pytest_bill_rate"
            assert first["effective_date"] == "2026-01-01"

            second = json.loads(
                await propose_driver.entrypoint(
                    name="pytest_bill_rate",
                    formula="160 * utilisation",
                    effective_date="2026-02-01",
                    run_context=run_context,
                )
            )
            assert second["formula"] == "160 * utilisation"
            assert second["effective_date"] == "2026-02-01"
        finally:
            import asyncpg

            from fpa_be.db import app_dsn

            conn = await asyncpg.connect(app_dsn())
            try:
                await conn.execute("DELETE FROM plan_driver WHERE name = 'pytest_bill_rate'")
            finally:
                await conn.close()

    @pytest.mark.asyncio
    async def test_rate_value_cannot_be_updated_by_fpa_app_role(self, run_context):
        """rate_value is fpa_controller-only at the DB grant level (Phase 2);
        this tool's INSERT ... ON CONFLICT DO UPDATE deliberately omits it so
        that column-level REVOKE, not app logic, is what blocks the write."""
        import asyncpg

        from fpa_be.db import app_dsn

        try:
            await propose_driver.entrypoint(
                name="pytest_rate_driver",
                formula="1.0",
                effective_date="2026-01-01",
                run_context=run_context,
                is_rate_driver=True,
                rate_value=42.0,
            )
            await propose_driver.entrypoint(
                name="pytest_rate_driver",
                formula="1.0",
                effective_date="2026-01-01",
                run_context=run_context,
                is_rate_driver=True,
                rate_value=99.0,
            )
            conn = await asyncpg.connect(app_dsn())
            try:
                row = await conn.fetchrow("SELECT rate_value FROM plan_driver WHERE name = 'pytest_rate_driver'")
            finally:
                await conn.close()
            assert float(row["rate_value"]) == 42.0
        finally:
            conn = await asyncpg.connect(app_dsn())
            try:
                await conn.execute("DELETE FROM plan_driver WHERE name = 'pytest_rate_driver'")
            finally:
                await conn.close()
