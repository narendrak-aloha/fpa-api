"""CubeClient end-to-end: parse -> typecheck -> resolve AS OF -> compile ->
execute -> vintage-tagged result, against the live seeded ClickHouse.
"""

from fpa_be.compiler.security import SecurityContext
from fpa_be.dsl.parser import parse_query
from fpa_be.dsl.resolver import check_node

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))


def _run(cube, dsl, ctx=PL_SCOPE):
    query = parse_query(dsl)
    check_node(query)
    return cube.run(query, ctx)


class TestVintageAwareExecution:
    def test_poland_q2_delivery_cost_differs_between_vintages_and_carries_its_vintage(self, cube):
        as_of_july = _run(cube, "SELECT delivery_cost WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 AS OF '2026-07-05T18:00:00'")
        as_of_august = _run(cube, "SELECT delivery_cost WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 AS OF '2026-08-12T09:30:00'")

        assert as_of_july.vintage == 1
        assert as_of_august.vintage == 2
        assert as_of_july.rows != as_of_august.rows

    def test_default_query_reads_latest_vintage(self, cube):
        result = _run(cube, "SELECT delivery_cost WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        assert result.vintage is None
        assert len(result.rows) > 0

    def test_result_columns_match_the_select_list(self, cube):
        result = _run(cube, "SELECT services_revenue BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2")
        assert result.columns == ("practice", "services_revenue")
