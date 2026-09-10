"""BRIDGE and drill-through go through the compiler under the same contract
as every other query -- registry-resolved names, the caller's scope
injected, every literal bound -- and the bridge ties at every rollup node.
"""

import pytest

from fpa_be.compiler.compile import compile, compile_bridge, compile_drill_through
from fpa_be.compiler.errors import UnsupportedQueryShapeError
from fpa_be.compiler.security import SecurityContext
from fpa_be.cube.client import CubeClient
from fpa_be.dsl.parser import parse_query
from fpa_be.dsl.resolver import check_node

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))
UNRESTRICTED = SecurityContext()
COMPARE = "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE"
PL_Q2_BRIDGE = f"SELECT services_revenue BY practice WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 {COMPARE}"


def _query(dsl: str):
    query = parse_query(dsl)
    check_node(query)
    return query


class TestCompilerContract:
    def test_literals_and_scope_are_bound_never_inlined(self):
        bridge = compile_bridge(_query(PL_Q2_BRIDGE), PL_SCOPE)
        for literal in ("'PL'", "PV-2026-0001", "RTPL1"):
            assert literal not in bridge.compiled.sql
        params = list(bridge.compiled.params.values())
        assert "PV-2026-0001" in params
        assert "PL" in params
        assert ["RTPL1"] in params
        assert (bridge.kind, bridge.by) == ("revenue", ("practice",))

    def test_a_cost_measure_uses_the_cost_legs(self):
        bridge = compile_bridge(_query(f"SELECT total_cogs FOR PERIOD 2026-Q2 {COMPARE}"), PL_SCOPE)
        assert bridge.kind == "cost"

    @pytest.mark.parametrize(
        ("dsl", "reason"),
        [
            ("SELECT services_revenue FOR PERIOD 2026-Q2 BRIDGE", "COMPARE PLAN"),
            (f"SELECT utilisation FOR PERIOD 2026-Q2 {COMPARE}", "cannot be bridged"),
            (f"SELECT services_revenue BY geo_region FOR PERIOD 2026-Q2 {COMPARE}", "rolls up BY"),
            (f"SELECT services_revenue FOR PERIOD 2026-Q2 {COMPARE} LIMIT 5", "LIMIT"),
        ],
    )
    def test_unsupported_shapes_are_refused_with_a_reason(self, dsl, reason):
        with pytest.raises(UnsupportedQueryShapeError, match=reason):
            compile_bridge(_query(dsl), PL_SCOPE)

    def test_compile_still_refuses_compare_without_bridge(self):
        query = _query("SELECT services_revenue FOR PERIOD 2026-Q2 COMPARE PLAN pv='PV-2026-0001' TO ACTUAL")
        with pytest.raises(UnsupportedQueryShapeError, match="only supported together with BRIDGE"):
            compile(query, PL_SCOPE)

    def test_drill_through_binds_the_hashes_and_the_scope(self):
        compiled = compile_drill_through([b"\x00" * 16], None, PL_SCOPE, 10)
        assert "RTPL1" not in compiled.sql
        assert ["RTPL1"] in compiled.params.values()
        assert [b"\x00" * 16] in compiled.params.values()


class TestLiveBridge:
    def test_ties_at_the_root_and_at_every_practice(self, ch_client):
        result = CubeClient(client=ch_client).run(_query(PL_Q2_BRIDGE), PL_SCOPE)
        col = {name: i for i, name in enumerate(result.columns)}
        assert len(result.rows) > 1
        for row in result.rows:
            assert abs(row[col["residual"]]) < row[col["tol"]], f"breaks at practice={row[col['practice']]}"

        root = result.rows[0]
        assert root[col["practice"]] == "(all)"
        for leg in ("Volume", "Mix(practice)", "Mix(grade)", "Price", "FX"):
            assert abs(root[col[leg]]) > 1.00, f"leg {leg} is suspiciously zero"

    def test_scope_is_injected_whatever_the_dsl_asks_for(self, ch_client):
        germany = _query(f"SELECT services_revenue WHERE geo_country = 'DE' FOR PERIOD 2026-Q2 {COMPARE}")
        assert CubeClient(client=ch_client).run(germany, PL_SCOPE).rows == []
        assert CubeClient(client=ch_client).run(germany, UNRESTRICTED).rows != []

    def test_as_of_bridges_the_named_vintage(self, ch_client):
        cube = CubeClient(client=ch_client)
        base = "SELECT services_revenue WHERE geo_country = 'PL' FOR PERIOD 2026-Q2"
        july = cube.run(_query(f"{base} AS OF '2026-07-05T18:00:00' {COMPARE}"), PL_SCOPE)
        latest = cube.run(_query(f"{base} {COMPARE}"), PL_SCOPE)
        assert (july.vintage, latest.vintage) == (1, None)
        gap = july.columns.index("gap")
        assert july.rows[0][gap] != pytest.approx(latest.rows[0][gap])
