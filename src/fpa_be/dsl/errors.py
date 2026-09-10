class FinOpsExprError(Exception):
    """Base class for every FinOpsExpr failure -- grammar, resolver, or compiler."""


class FinOpsExprSyntaxError(FinOpsExprError):
    """Malformed formula/query text that doesn't parse at all."""


class UnknownMetricError(FinOpsExprError):
    def __init__(self, name: str):
        super().__init__(f"unknown metric: {name!r}")
        self.name = name


class UnknownDimensionError(FinOpsExprError):
    def __init__(self, name: str):
        super().__init__(f"unknown dimension: {name!r}")
        self.name = name


class IllegalAggregationError(FinOpsExprError):
    """A ratio measure was wrapped in SUM/AVG, or a semi-additive measure was
    summed across time -- the registry says that measure's type forbids it.
    """


class CyclicDriverError(FinOpsExprError):
    def __init__(self, cycle: tuple[str, ...]):
        super().__init__(f"cyclic driver dependency: {' -> '.join(cycle)}")
        self.cycle = cycle
