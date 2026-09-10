class CompilerError(Exception):
    """Base class for compile()-time failures -- distinct from dsl.errors,
    which are grammar/resolver-level."""


class ScopeViolationError(CompilerError):
    """The DSL asked for rows outside the caller's security scope. Row scope
    is injected server-side regardless of what the query said; this is
    raised only when the query's own WHERE narrows to a value the caller's
    scope can never satisfy (an always-false filter is compiled instead
    whenever possible, but a directly self-contradictory ask is rejected
    outright so the caller gets a clear signal rather than a silent empty
    result)."""


class QueryTooExpensiveError(CompilerError):
    def __init__(self, estimated_rows: int, budget: int):
        super().__init__(
            f"estimated {estimated_rows:,} rows scanned exceeds the {budget:,}-row query budget; "
            "narrow the period range, BY dimensions, or WHERE filter"
        )
        self.estimated_rows = estimated_rows
        self.budget = budget


class UnsupportedQueryShapeError(CompilerError):
    """The AST is well-typed per the resolver but this compiler doesn't
    (yet) know how to turn this particular shape into SQL."""
