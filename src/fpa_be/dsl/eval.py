"""Evaluates a parsed FinOpsExpr driver formula (the `expr` grammar entry,
e.g. `heads * available_hours * utilisation * bill_rate * realisation`)
against a concrete set of numeric bindings -- one driver value or measure
reading per name.

This is arithmetic evaluation of an already-parsed, already-typechecked AST,
not a second parser and not `eval`/`exec`/`compile`: the only operations are
the four the grammar itself defines (+, -, *, /) plus unary negation, walked
by hand.
"""

from fpa_be.dsl import ast_nodes as ast


class UnboundNameError(KeyError):
    """A name in the formula has no value in the bindings passed to eval_expr."""


def eval_expr(node: object, bindings: dict[str, float]) -> float:
    if isinstance(node, ast.NumberLit):
        return node.value
    if isinstance(node, ast.Ident):
        if node.name not in bindings:
            raise UnboundNameError(node.name)
        return bindings[node.name]
    if isinstance(node, ast.Neg):
        return -eval_expr(node.operand, bindings)
    if isinstance(node, ast.BinOp):
        left = eval_expr(node.left, bindings)
        right = eval_expr(node.right, bindings)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        if node.op == "/":
            return left / right
        if node.op == "^":
            return left**right
        raise ValueError(f"unsupported operator in driver formula: {node.op!r}")
    raise TypeError(f"cannot evaluate FinOpsExpr node as a driver formula: {node!r}")
