"""eval_expr: arithmetic evaluation of a parsed driver formula against a
concrete bindings dict -- no eval/exec/compile anywhere."""

import pytest

from fpa_be.dsl import parse_expr
from fpa_be.dsl.eval import UnboundNameError, eval_expr


class TestEvalExpr:
    def test_number_literal(self):
        assert eval_expr(parse_expr("42"), {}) == 42

    def test_bound_identifier(self):
        assert eval_expr(parse_expr("bill_rate"), {"bill_rate": 150.0}) == 150.0

    def test_unbound_identifier_raises(self):
        with pytest.raises(UnboundNameError):
            eval_expr(parse_expr("bill_rate"), {})

    def test_the_assignments_own_driver_formula_example(self):
        bindings = {
            "heads": 10.0,
            "available_hours": 160.0,
            "utilisation": 0.75,
            "bill_rate": 150.0,
            "realisation": 0.9,
        }
        result = eval_expr(parse_expr("heads * available_hours * utilisation * bill_rate * realisation"), bindings)
        assert result == pytest.approx(10.0 * 160.0 * 0.75 * 150.0 * 0.9)

    def test_negation(self):
        assert eval_expr(parse_expr("-bill_rate"), {"bill_rate": 100.0}) == -100.0

    def test_addition_and_subtraction(self):
        assert eval_expr(parse_expr("10 + 5 - 3"), {}) == 12

    def test_division(self):
        assert eval_expr(parse_expr("100 / 4"), {}) == 25.0

    def test_precedence(self):
        assert eval_expr(parse_expr("2 + 3 * 4"), {}) == 14
