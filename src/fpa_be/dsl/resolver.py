"""Typechecks a parsed FinOpsExpr AST against the frozen measure/dimension
registry. This is the layer that turns "some AST" into "an AST the
compiler is allowed to touch": every metric/dimension name must resolve,
and every measure's declared type must permit the aggregation it's
wrapped in -- SUM(utilisation) is a type error here, not a runtime one.
"""

from fpa_be.dsl import ast_nodes as ast
from fpa_be.dsl.errors import (
    CyclicDriverError,
    IllegalAggregationError,
    UnknownDimensionError,
    UnknownMetricError,
)
from fpa_be.registry.dimensions import DIM_COLUMNS, SEPARATE_AXES
from fpa_be.registry.measures import MEASURES, Measure, MeasureKind

_AGGREGATING_FUNCS = frozenset({"SUM", "AVG"})
# company/account/period_month are indexed separately on the fact tables
# (not part of the 19-column compound key) but are still legal BY/WHERE
# targets in the DSL.
_ALL_DIMENSION_NAMES = frozenset(DIM_COLUMNS) | frozenset(SEPARATE_AXES)


def resolve_metric_name(name: str, driver_names: frozenset[str] = frozenset()) -> Measure | None:
    """The Measure `name` resolves to, or None if it's a plan_driver name
    instead -- drivers aren't in the static registry, they live in
    Postgres' plan_driver table (Phase 2)."""
    if name in MEASURES:
        return MEASURES[name]
    if name in driver_names:
        return None
    raise UnknownMetricError(name)


def resolve_dimension_name(name: str) -> str:
    if name not in _ALL_DIMENSION_NAMES:
        raise UnknownDimensionError(name)
    return name


def check_node(node: object, driver_names: frozenset[str] = frozenset()) -> None:
    """Walks an AST node, raising on the first unknown name or illegal
    aggregation. Call this on every expr/query before it reaches the
    compiler (Phase 4)."""
    if isinstance(node, (ast.NumberLit, ast.StringLit)):
        return
    if isinstance(node, ast.Ident):
        resolve_metric_name(node.name, driver_names)
        return
    if isinstance(node, ast.BinOp):
        check_node(node.left, driver_names)
        check_node(node.right, driver_names)
        return
    if isinstance(node, ast.Neg):
        check_node(node.operand, driver_names)
        return
    if isinstance(node, ast.Call):
        if node.func in _AGGREGATING_FUNCS:
            _check_aggregation_legality(node, driver_names)
        for arg in node.args:
            check_node(arg, driver_names)
        return
    if isinstance(node, ast.Comparison):
        resolve_dimension_name(node.dim)
        return
    if isinstance(node, ast.BoolPred):
        check_node(node.left, driver_names)
        check_node(node.right, driver_names)
        return
    if isinstance(node, ast.Query):
        for measure in node.measures:
            check_node(measure, driver_names)
        for dim in node.by:
            resolve_dimension_name(dim)
        if node.where is not None:
            check_node(node.where, driver_names)
        return
    raise TypeError(f"unhandled FinOpsExpr AST node: {node!r}")


def _check_aggregation_legality(call: ast.Call, driver_names: frozenset[str]) -> None:
    for arg in call.args:
        if not isinstance(arg, ast.Ident):
            continue
        measure = resolve_metric_name(arg.name, driver_names)
        if measure is not None and measure.kind == MeasureKind.RATIO:
            raise IllegalAggregationError(
                f"{call.func}({arg.name}) is illegal: {arg.name!r} is a ratio measure, "
                "never summed/averaged across groups -- it is recomputed from its own "
                "numerator and denominator at whatever grain is requested"
            )


def topological_driver_order(driver_formulas: dict[str, object]) -> list[str]:
    """Kahn's algorithm over the driver-reference graph implied by each
    driver's own formula (calc_order_dag). Raises CyclicDriverError if the
    referenced drivers don't form a DAG."""
    driver_names = set(driver_formulas)
    deps = {name: _referenced_driver_names(node, driver_names) for name, node in driver_formulas.items()}

    in_degree = {name: len(refs) for name, refs in deps.items()}
    remaining = {name: set(refs) for name, refs in deps.items()}
    ready = [name for name, degree in in_degree.items() if degree == 0]
    order: list[str] = []

    while ready:
        name = ready.pop()
        order.append(name)
        for other, refs in remaining.items():
            if name in refs:
                refs.discard(name)
                in_degree[other] -= 1
                if in_degree[other] == 0 and other not in order and other not in ready:
                    ready.append(other)

    if len(order) != len(driver_formulas):
        cyclic = tuple(sorted(driver_names - set(order)))
        raise CyclicDriverError(cyclic)
    return order


def _referenced_driver_names(node: object, driver_names: set[str]) -> set[str]:
    found: set[str] = set()

    def walk(n: object) -> None:
        if isinstance(n, ast.Ident):
            if n.name in driver_names:
                found.add(n.name)
        elif isinstance(n, ast.BinOp):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, ast.Neg):
            walk(n.operand)
        elif isinstance(n, ast.Call):
            for arg in n.args:
                walk(arg)

    walk(node)
    return found
