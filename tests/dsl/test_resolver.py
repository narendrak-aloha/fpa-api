"""Resolver tests: every name in an AST must resolve against the frozen
registry, and every measure's declared kind decides what it can legally be
wrapped in.
"""

import pytest

from fpa_be.dsl import ast_nodes as ast
from fpa_be.dsl import check_node, parse_expr, parse_query, topological_driver_order
from fpa_be.dsl.errors import (
    CyclicDriverError,
    IllegalAggregationError,
    UnknownDimensionError,
    UnknownMetricError,
)

TIME_FUNCS = ["PRIOR", "LEAD", "YOY", "CAGR", "YTD", "QTD", "MTD", "ROLLING"]


@pytest.mark.parametrize("func", TIME_FUNCS)
def test_every_time_operator_parses_and_resolves(func):
    tree = parse_expr(f"{func}(services_revenue, 3)" if func in ("PRIOR", "LEAD", "CAGR", "ROLLING") else f"{func}(services_revenue)")
    check_node(tree)
    assert isinstance(tree, ast.Call)
    assert tree.func == func


@pytest.mark.parametrize(
    "metric",
    ["services_revenue", "total_revenue", "total_cogs", "total_opex", "delivery_cost", "subcontractor_cost"],
)
def test_additive_measures_are_legal_inside_sum(metric):
    check_node(parse_expr(f"SUM({metric})"))


@pytest.mark.parametrize("metric", ["headcount"])
def test_semi_additive_measures_are_legal_inside_sum(metric):
    check_node(parse_expr(f"SUM({metric})"))


@pytest.mark.parametrize("metric", ["utilisation", "realisation", "gross_margin_pct"])
def test_ratio_measures_are_illegal_inside_sum(metric):
    with pytest.raises(IllegalAggregationError):
        check_node(parse_expr(f"SUM({metric})"))


@pytest.mark.parametrize("metric", ["utilisation", "realisation", "gross_margin_pct"])
def test_ratio_measures_are_illegal_inside_avg(metric):
    with pytest.raises(IllegalAggregationError):
        check_node(parse_expr(f"AVG({metric})"))


def test_unknown_metric_is_rejected():
    with pytest.raises(UnknownMetricError):
        check_node(parse_expr("definitely_not_a_metric"))


def test_unknown_metric_nested_in_call_is_rejected():
    with pytest.raises(UnknownMetricError):
        check_node(parse_expr("PRIOR(not_a_real_metric, 12)"))


def test_unknown_dimension_in_query_by_is_rejected():
    with pytest.raises(UnknownDimensionError):
        check_node(parse_query("SELECT services_revenue BY not_a_real_dimension"))


def test_unknown_dimension_in_where_is_rejected():
    with pytest.raises(UnknownDimensionError):
        check_node(parse_query("SELECT services_revenue WHERE not_a_real_dimension = 'x'"))


def test_known_separate_axis_dimension_resolves():
    # company/account/period_month aren't in the 19-dim compound key but are
    # still legal BY/WHERE targets (see registry/dimensions.py SEPARATE_AXES).
    check_node(parse_query("SELECT services_revenue BY company"))


def test_driver_name_resolves_when_not_a_known_metric():
    tree = parse_expr("attach_rate * 2")
    check_node(tree, driver_names=frozenset({"attach_rate"}))


def test_unlisted_driver_name_still_rejected():
    with pytest.raises(UnknownMetricError):
        check_node(parse_expr("attach_rate * 2"))


def test_gross_margin_terms_are_exactly_revenue_minus_cogs():
    from fpa_be.registry.measures import MEASURES

    assert MEASURES["gross_margin"].terms == (("+", "total_revenue"), ("-", "total_cogs"))


class TestTopologicalDriverOrder:
    def test_simple_chain_resolves_in_dependency_order(self):
        formulas = {
            "a": parse_expr("1"),
            "b": parse_expr("a * 2"),
            "c": parse_expr("b * 2"),
        }
        order = topological_driver_order(formulas)
        assert order.index("a") < order.index("b") < order.index("c")

    def test_independent_drivers_have_no_forced_order(self):
        formulas = {"a": parse_expr("1"), "b": parse_expr("2")}
        order = topological_driver_order(formulas)
        assert set(order) == {"a", "b"}

    def test_direct_cycle_is_rejected(self):
        formulas = {"a": parse_expr("b * 2"), "b": parse_expr("a * 2")}
        with pytest.raises(CyclicDriverError):
            topological_driver_order(formulas)

    def test_self_reference_is_rejected(self):
        formulas = {"a": parse_expr("a * 2")}
        with pytest.raises(CyclicDriverError):
            topological_driver_order(formulas)

    def test_longer_cycle_is_rejected(self):
        formulas = {
            "a": parse_expr("b * 2"),
            "b": parse_expr("c * 2"),
            "c": parse_expr("a * 2"),
        }
        with pytest.raises(CyclicDriverError):
            topological_driver_order(formulas)
