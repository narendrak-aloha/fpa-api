"""Grammar-level tests: precedence, nesting, and both entry points parse
the exact shapes the assignment's own examples use.
"""

import pytest

from fpa_be.dsl import ast_nodes as ast
from fpa_be.dsl import parse_expr, parse_query
from fpa_be.dsl.errors import FinOpsExprSyntaxError


def test_precedence_multiplication_before_addition():
    tree = parse_expr("1 + 2 * 3")
    assert tree == ast.BinOp("+", ast.NumberLit(1), ast.BinOp("*", ast.NumberLit(2), ast.NumberLit(3)))


def test_precedence_power_before_multiplication():
    tree = parse_expr("2 * 3 ^ 2")
    assert tree == ast.BinOp("*", ast.NumberLit(2), ast.BinOp("^", ast.NumberLit(3), ast.NumberLit(2)))


def test_parentheses_override_precedence():
    tree = parse_expr("(1 + 2) * 3")
    assert tree == ast.BinOp("*", ast.BinOp("+", ast.NumberLit(1), ast.NumberLit(2)), ast.NumberLit(3))


def test_left_associative_subtraction():
    tree = parse_expr("10 - 3 - 2")
    assert tree == ast.BinOp("-", ast.BinOp("-", ast.NumberLit(10), ast.NumberLit(3)), ast.NumberLit(2))


def test_unary_negation():
    tree = parse_expr("-services_revenue")
    assert tree == ast.Neg(ast.Ident("services_revenue"))


def test_nested_function_calls():
    tree = parse_expr("PRIOR(services_revenue, 12) * (1 + YOY(bookings) * attach_rate)")
    assert isinstance(tree, ast.BinOp)
    prior_call, rest = tree.left, tree.right
    assert prior_call == ast.Call("PRIOR", (ast.Ident("services_revenue"), ast.NumberLit(12)))
    assert isinstance(rest, ast.BinOp) and rest.op == "+"


def test_driver_formula_multiplication_chain():
    tree = parse_expr("heads * available_hours * utilisation * bill_rate * realisation")
    # left-associative: (((heads * available_hours) * utilisation) * bill_rate) * realisation
    assert tree.op == "*"
    assert tree.right == ast.Ident("realisation")


@pytest.mark.parametrize(
    "text",
    [
        """SELECT services_revenue, gross_margin
BY practice, geo_country
WHERE geo_country IN ('PL') AND engine = 'Services'
FOR PERIOD 2026-Q2
COMPARE PLAN pv='PV-2026-0001' TO ACTUAL
BRIDGE""",
        """SELECT subcontractor_cost
BY company
WHERE geo_region = 'EMEA'
FOR PERIOD 2026-H1""",
        """SELECT utilisation, gross_margin_pct
BY practice, grade
WHERE geo_country = 'PL' AND delivery_shore != 'Offshore'
FOR PERIOD 2026-Q2
LIMIT 50""",
        """SELECT delivery_cost
BY practice
WHERE geo_country = 'PL'
FOR PERIOD 2026-Q2
AS OF '2026-07-05T18:00:00'""",
        """SELECT YOY(services_revenue), ROLLING(bookings, 3)
BY practice
WHERE engine = 'Services'
FOR PERIOD 2026-Q1..2026-Q4
COMPARE PLAN pv='PV-2026-0001', scenario='downside' TO ACTUAL""",
    ],
)
def test_assignment_query_examples_all_parse(text):
    query = parse_query(text)
    assert isinstance(query, ast.Query)
    assert len(query.measures) >= 1


def test_query_bridge_flag_and_limit():
    q = parse_query("SELECT services_revenue BY practice LIMIT 50")
    assert q.bridge is False
    assert q.limit == 50


def test_query_bare_minimum_is_just_select():
    q = parse_query("SELECT services_revenue")
    assert q.by == ()
    assert q.where is None
    assert q.period is None


def test_query_as_of_vintage_number():
    q = parse_query("SELECT services_revenue AS OF 2")
    assert q.as_of.value == 2.0


def test_query_not_in_predicate():
    q = parse_query("SELECT services_revenue WHERE geo_country NOT IN ('PL', 'DE')")
    assert q.where.op == "NOT IN"
    assert q.where.values == (ast.StringLit("PL"), ast.StringLit("DE"))


def test_malformed_formula_raises_syntax_error():
    with pytest.raises(FinOpsExprSyntaxError):
        parse_expr("heads *")


def test_malformed_formula_unbalanced_parens():
    with pytest.raises(FinOpsExprSyntaxError):
        parse_expr("(services_revenue + total_cogs")


def test_malformed_query_missing_select():
    with pytest.raises(FinOpsExprSyntaxError):
        parse_query("services_revenue BY practice")
