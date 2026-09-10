"""The exactly-four agent tools, end to end against live ClickHouse/Postgres:
`list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver` --
plus the masking gate, which is the tool hook every agent carries around
them rather than something inside a tool body.
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
from fpa_be.masking.gate import disclosure_tool_hook
from fpa_be.registry.dimensions import DIM_COLUMNS, SEPARATE_AXES
from tests.conftest import SUPERUSER_DSN

from .conftest import make_run_context

PL_Q2_BRIDGE = (
    "SELECT services_revenue BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 "
    "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE"
)


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
    async def test_customer_dimension_is_masked_and_disclosure_logged_by_the_tool_hook(self, run_context):
        dsl = "SELECT services_revenue BY customer WHERE geo_country = 'PL' FOR PERIOD 2026-Q2"
        result = json.loads(
            await disclosure_tool_hook(
                "run_finops_query",
                run_finops_query.entrypoint,
                {"dsl": dsl, "run_context": run_context},
                run_context=run_context,
            )
        )
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

    @pytest.mark.asyncio
    async def test_a_bridge_query_returns_legs_that_tie_at_every_node(self, run_context):
        result = json.loads(await run_finops_query.entrypoint(dsl=PL_Q2_BRIDGE, run_context=run_context))
        assert result["vintage"] is None
        col = {name: i for i, name in enumerate(result["columns"])}
        assert {"Volume", "Mix(practice)", "Mix(grade)", "Price", "FX", "residual", "tol"} <= col.keys()
        assert len(result["rows"]) > 1
        for row in result["rows"]:
            assert abs(row[col["residual"]]) < row[col["tol"]]

    @pytest.mark.asyncio
    async def test_an_unknown_vintage_is_a_structured_error(self, run_context):
        result = json.loads(
            await run_finops_query.entrypoint(
                dsl="SELECT services_revenue FOR PERIOD 2026-Q2 AS OF 99", run_context=run_context
            )
        )
        assert set(result) == {"error"}


async def _delete_draft_proposals(name: str) -> None:
    conn = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await conn.execute("DELETE FROM plan_driver_proposal WHERE name = $1", name)
    finally:
        await conn.close()


class TestProposeDriver:
    def test_still_requires_human_confirmation(self):
        assert propose_driver.requires_confirmation is True

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
    async def test_records_a_draft_and_never_touches_the_live_driver(self, run_context):
        try:
            result = json.loads(
                await propose_driver.entrypoint(
                    name="pytest_bill_rate",
                    formula="150 * utilisation",
                    effective_date="2026-01-01",
                    run_context=run_context,
                )
            )
            assert result["state"] == "Draft"

            conn = await asyncpg.connect(app_dsn())
            try:
                proposal = await conn.fetchrow(
                    "SELECT proposed_by, state FROM plan_driver_proposal WHERE id = $1", result["proposal_id"]
                )
                live = await conn.fetchval("SELECT count(*) FROM plan_driver WHERE name = 'pytest_bill_rate'")
            finally:
                await conn.close()
            assert tuple(proposal) == ("planner@example.com", "Draft")
            assert live == 0
        finally:
            await _delete_draft_proposals("pytest_bill_rate")

    @pytest.mark.asyncio
    async def test_a_run_without_an_authenticated_caller_cannot_propose(self):
        result = json.loads(
            await propose_driver.entrypoint(
                name="pytest_orphan", formula="1", effective_date="2026-01-01", run_context=make_run_context(user_id=None)
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_a_malformed_effective_date_is_a_structured_error(self, run_context):
        result = json.loads(
            await propose_driver.entrypoint(name="pytest_x", formula="1", effective_date="soon", run_context=run_context)
        )
        assert "error" in result
