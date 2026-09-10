"""Parses FinOpsExpr text into the AST in ast_nodes.py. No eval/exec/compile
anywhere in this module or anywhere downstream -- the grammar is walked by
a Lark Transformer, nothing here ever executes user-supplied text as code.
"""

from pathlib import Path

from lark import Lark, Transformer, UnexpectedInput

from fpa_be.dsl import ast_nodes as ast
from fpa_be.dsl.errors import FinOpsExprSyntaxError

_GRAMMAR_PATH = Path(__file__).parent / "grammar.lark"
_GRAMMAR = _GRAMMAR_PATH.read_text()

_expr_parser = Lark(_GRAMMAR, parser="lalr", start="expr")
_query_parser = Lark(_GRAMMAR, parser="lalr", start="query")


class _AstBuilder(Transformer):
    # ---- arithmetic ----------------------------------------------------
    def add(self, c):
        return ast.BinOp("+", c[0], c[1])

    def sub(self, c):
        return ast.BinOp("-", c[0], c[1])

    def mul(self, c):
        return ast.BinOp("*", c[0], c[1])

    def div(self, c):
        return ast.BinOp("/", c[0], c[1])

    def pow(self, c):
        return ast.BinOp("^", c[0], c[1])

    def neg(self, c):
        return ast.Neg(c[0])

    def number(self, c):
        return ast.NumberLit(float(c[0]))

    def string(self, c):
        return ast.StringLit(str(c[0])[1:-1])

    def ident(self, c):
        return ast.Ident(str(c[0]))

    def call(self, c):
        func = str(c[0])
        args = c[1] if len(c) > 1 and c[1] is not None else ()
        return ast.Call(func, tuple(args))

    def call_args(self, c):
        return list(c)

    # ---- predicates ------------------------------------------------------
    def comparison_binary(self, c):
        return ast.Comparison(str(c[0]), str(c[1]), (c[2],))

    def comparison_in(self, c):
        return ast.Comparison(str(c[0]), "IN", tuple(c[1]))

    def comparison_not_in(self, c):
        return ast.Comparison(str(c[0]), "NOT IN", tuple(c[1]))

    def literal_list(self, c):
        return list(c)

    def pred_and(self, c):
        return ast.BoolPred("AND", c[0], c[1])

    def pred_or(self, c):
        return ast.BoolPred("OR", c[0], c[1])

    # ---- query -------------------------------------------------------------
    def measure_list(self, c):
        return list(c)

    def by_clause(self, c):
        return tuple(str(tok) for tok in c)

    def where_clause(self, c):
        return c[0]

    def period_range(self, c):
        start = str(c[0])
        end = str(c[1]) if len(c) > 1 else None
        return ast.PeriodRange(start, end)

    def period_clause(self, c):
        return c[0]

    def asof_clause(self, c):
        token = c[0]
        if token.type == "STRING":
            return ast.AsOf(str(token)[1:-1])
        return ast.AsOf(float(token))

    def plan_ref(self, c):
        return str(c[0])[1:-1]

    def scenario_ref(self, c):
        return str(c[0])[1:-1]

    def compare_clause(self, c):
        plan_version = c[0]
        scenario = c[1] if len(c) > 1 else None
        return ast.Compare(plan_version, scenario)

    def bridge_clause(self, c):
        return True

    def limit_clause(self, c):
        return int(c[0])

    def query(self, c):
        clauses = {
            "measures": tuple(c[0]),
            "by": (),
            "where": None,
            "period": None,
            "as_of": None,
            "compare": None,
            "bridge": False,
            "limit": None,
        }
        for item in c[1:]:
            if item is None:
                continue
            if isinstance(item, tuple) and item and isinstance(item[0], str):
                # by_clause result: tuple[str, ...] -- the only optional
                # clause that produces a tuple of plain strings.
                clauses["by"] = item
            elif isinstance(item, ast.PeriodRange):
                clauses["period"] = item
            elif isinstance(item, ast.AsOf):
                clauses["as_of"] = item
            elif isinstance(item, ast.Compare):
                clauses["compare"] = item
            elif item is True:
                clauses["bridge"] = True
            elif isinstance(item, int):
                clauses["limit"] = item
            else:
                clauses["where"] = item
        return ast.Query(**clauses)


_builder = _AstBuilder()


def parse_expr(text: str):
    try:
        tree = _expr_parser.parse(text)
    except UnexpectedInput as exc:
        raise FinOpsExprSyntaxError(f"malformed FinOpsExpr formula: {exc}") from exc
    return _builder.transform(tree)


def parse_query(text: str) -> ast.Query:
    try:
        tree = _query_parser.parse(text)
    except UnexpectedInput as exc:
        raise FinOpsExprSyntaxError(f"malformed FinOpsExpr query: {exc}") from exc
    result = _builder.transform(tree)
    return result
