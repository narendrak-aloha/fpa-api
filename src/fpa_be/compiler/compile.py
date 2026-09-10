"""The only path from a parsed+typechecked FinOpsExpr query to SQL:

    DSL -> parse -> typecheck -> authorize -> scope-inject -> parameterize -> ClickHouse

compile() takes an already-parsed, already-checked ast.Query (see
dsl.parser.parse_query / dsl.resolver.check_node) and turns it into a
parameterized ClickHouse statement. No eval/exec/compile, no string-built
SQL from user-supplied values -- only frozen registry names (measure/
account/dimension identifiers) are ever inlined as raw SQL; every literal
value the caller supplied (WHERE comparisons, security scope) goes out as
a bound {name:Type} parameter.

Two-level aggregation is the load-bearing trick for getting additive,
semi-additive and ratio measures all correct out of one query shape:

  1. "monthly" CTE -- GROUP BY (by_dims + period_month), one row per
     dimension-combination per month. Every leaf (AccountAmountMeasure /
     DistinctCountMeasure) is summed/counted here with sumIf/uniqExactIf,
     filtered to its own account codes.
  2. "rollup" CTE -- GROUP BY the dims actually requested (period_month
     only if the caller asked for it, or if a time-intelligence function
     forces a per-month grain -- see _needs_monthly_grain). Additive
     leaves are sum()'d across whatever monthly rows collapsed into this
     group; semi-additive leaves take argMax(leaf, period_month) instead,
     i.e. the closing month's value, unless period_month is already the
     grain (in which case sum() over one row is a no-op passthrough).
  3. Final SELECT -- builds each requested measure's value expression by
     walking ComputedAdditiveMeasure/RatioMeasure trees down to agg_<leaf>
     columns (_value_expr), and wraps it in a ClickHouse window function
     for the eight time-intelligence operators.
"""

import itertools
from dataclasses import dataclass

from fpa_be.compiler import period
from fpa_be.compiler.errors import QueryTooExpensiveError, UnsupportedQueryShapeError
from fpa_be.compiler.fact_source import actual_fact_source
from fpa_be.compiler.security import SecurityContext
from fpa_be.dsl import ast_nodes as ast
from fpa_be.dsl.resolver import check_node, resolve_metric_name
from fpa_be.registry.measures import (
    MEASURES,
    AccountAmountMeasure,
    ComputedAdditiveMeasure,
    DistinctCountMeasure,
    Measure,
    MeasureKind,
    RatioMeasure,
)
from fpa_be.registry.reference import COMPANIES

# Pure heuristic, not a real EXPLAIN-based estimator: the seed carries 24
# months of history across 20 companies at ~3,200 fact rows/company/month
# (see docs/DATA_MODEL.md). Good enough to reject a caller trying to scan
# every company for every seeded month in one query.
_ROWS_PER_COMPANY_MONTH = 3_200
MAX_ESTIMATED_ROWS = 2_000_000

_TIME_FUNCS_WITH_OFFSET = frozenset({"PRIOR", "LEAD", "CAGR", "ROLLING"})
_TIME_FUNCS = frozenset({"PRIOR", "LEAD", "YOY", "CAGR", "YTD", "QTD", "MTD", "ROLLING"})


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: dict[str, object]
    vintage: int | None  # None means "latest", echoed back so callers can surface it


@dataclass(frozen=True)
class _QueryMeasure:
    alias: str
    measure: Measure
    time_func: str | None = None
    time_arg: int | None = None


class _ParamPool:
    def __init__(self) -> None:
        self._params: dict[str, object] = {}
        self._counter = itertools.count()

    def add(self, value: object, ch_type: str) -> str:
        name = f"p{next(self._counter)}"
        self._params[name] = value
        return f"{{{name}:{ch_type}}}"

    @property
    def params(self) -> dict[str, object]:
        return self._params


def compile(
    query: ast.Query,
    security_context: SecurityContext,
    resolved_vintage: int | None = None,
) -> CompiledQuery:
    check_node(query)

    if query.period is None:
        raise UnsupportedQueryShapeError("FOR PERIOD is required on every query -- every result must be bounded")
    if query.compare is not None or query.bridge:
        raise UnsupportedQueryShapeError(
            "COMPARE PLAN .. TO ACTUAL is only supported together with BRIDGE, which compiles via compile_bridge()"
        )

    start, end = period.range_bounds(query.period)
    measure_specs = [_compile_query_measure(m) for m in query.measures]

    leaves: dict[str, Measure] = {}
    for spec in measure_specs:
        _collect_leaves(spec.measure, leaves)

    needs_monthly_grain = any(spec.time_func is not None for spec in measure_specs)
    by_dims = list(dict.fromkeys(query.by))
    rollup_dims = list(by_dims)
    if needs_monthly_grain and "period_month" not in rollup_dims:
        rollup_dims.append("period_month")

    # Time functions look backwards past the caller's own FOR PERIOD (e.g.
    # YOY needs the same month a year earlier) -- fetch that much extra
    # history into monthly/rollup, then trim back down to what was asked.
    lookback_months = max((_lookback_months(spec) for spec in measure_specs), default=0)
    fetch_start = period.add_months(start, -lookback_months) if lookback_months else start
    _check_cost_budget(fetch_start, end, security_context)

    params = _ParamPool()
    monthly_sql = _build_monthly_cte(leaves, by_dims, query.where, security_context, fetch_start, end, resolved_vintage, params)
    rollup_sql = _build_rollup_cte(leaves, by_dims, rollup_dims)
    select_sql = _build_final_select(measure_specs, rollup_dims)

    order_by = ", ".join(rollup_dims) if rollup_dims else None
    sql = f"WITH monthly AS (\n{monthly_sql}\n),\nrollup AS (\n{rollup_sql}\n)\nSELECT {select_sql}\nFROM rollup"
    if lookback_months and "period_month" in rollup_dims:
        # ClickHouse has no QUALIFY, so nest the window-function SELECT and
        # drop the lookback rows outside it -- what QUALIFY desugars to.
        trim = (
            f"period_month >= {params.add(start.isoformat(), 'Date')} "
            f"AND period_month < {params.add(end.isoformat(), 'Date')}"
        )
        sql = f"SELECT * FROM (\n{sql}\n)\nWHERE {trim}"
    if order_by:
        sql += f"\nORDER BY {order_by}"
    if query.limit is not None:
        sql += f"\nLIMIT {int(query.limit)}"

    return CompiledQuery(sql=sql, params=params.params, vintage=resolved_vintage)


def _lookback_months(spec: _QueryMeasure) -> int:
    if spec.time_func in ("YOY",):
        return 12
    if spec.time_func in ("PRIOR", "CAGR", "ROLLING"):
        return spec.time_arg or 0
    return 0


# ---- cost budget -----------------------------------------------------------


def _check_cost_budget(start, end, security_context: SecurityContext) -> None:
    months = max(period.month_count(start, end), 1)
    n_companies = len(security_context.allowed_companies) if security_context.allowed_companies is not None else len(COMPANIES)
    estimated_rows = months * n_companies * _ROWS_PER_COMPANY_MONTH
    if estimated_rows > MAX_ESTIMATED_ROWS:
        raise QueryTooExpensiveError(estimated_rows, MAX_ESTIMATED_ROWS)


# ---- query-level measure parsing -------------------------------------------


def _compile_query_measure(node: object) -> _QueryMeasure:
    if isinstance(node, ast.Ident):
        measure = resolve_metric_name(node.name)
        if measure is None:
            raise UnsupportedQueryShapeError(f"{node.name!r} is a plan_driver, not a queryable measure")
        return _QueryMeasure(alias=node.name, measure=measure)

    if isinstance(node, ast.Call) and node.func in _TIME_FUNCS:
        if not node.args or not isinstance(node.args[0], ast.Ident):
            raise UnsupportedQueryShapeError(f"{node.func}(...) must wrap a single bare measure name")
        measure = resolve_metric_name(node.args[0].name)
        if measure is None:
            raise UnsupportedQueryShapeError(f"{node.args[0].name!r} is a plan_driver, not a queryable measure")
        time_arg = None
        if node.func in _TIME_FUNCS_WITH_OFFSET:
            if len(node.args) < 2 or not isinstance(node.args[1], ast.NumberLit):
                raise UnsupportedQueryShapeError(f"{node.func}(...) requires a numeric second argument")
            time_arg = int(node.args[1].value)
        alias = f"{node.func.lower()}_{node.args[0].name}" + (f"_{time_arg}" if time_arg is not None else "")
        return _QueryMeasure(alias=alias, measure=measure, time_func=node.func, time_arg=time_arg)

    raise UnsupportedQueryShapeError(
        f"only a bare measure name or a single time-function call over one is supported at query top level, got {node!r}"
    )


# ---- leaf collection --------------------------------------------------------


def _collect_leaves(measure: Measure, acc: dict[str, Measure]) -> None:
    if isinstance(measure, (AccountAmountMeasure, DistinctCountMeasure)):
        acc[measure.name] = measure
    elif isinstance(measure, ComputedAdditiveMeasure):
        for _, term_name in measure.terms:
            _collect_leaves(MEASURES[term_name], acc)
    elif isinstance(measure, RatioMeasure):
        _collect_leaves(MEASURES[measure.numerator], acc)
        _collect_leaves(MEASURES[measure.denominator], acc)
    else:
        raise TypeError(f"unhandled measure type: {measure!r}")


# ---- monthly CTE -------------------------------------------------------------


def _build_monthly_cte(
    leaves: dict[str, Measure],
    by_dims: list[str],
    where: object,
    security_context: SecurityContext,
    start,
    end,
    resolved_vintage: int | None,
    params: _ParamPool,
) -> str:
    group_cols = list(dict.fromkeys(by_dims + ["period_month"]))

    leaf_exprs = []
    for name, m in leaves.items():
        accounts_sql = ", ".join(f"'{a}'" for a in m.accounts)
        if isinstance(m, AccountAmountMeasure):
            leaf_exprs.append(f"sumIf({m.column}, account IN ({accounts_sql})) AS leaf_{name}")
        else:
            leaf_exprs.append(f"uniqExactIf({m.distinct_dim}, account IN ({accounts_sql})) AS leaf_{name}")

    select_cols = group_cols + leaf_exprs
    fact_source = actual_fact_source(resolved_vintage)

    where_parts = [
        f"period_month >= {params.add(start.isoformat(), 'Date')}",
        f"period_month < {params.add(end.isoformat(), 'Date')}",
        *_scope_predicates(security_context, params),
    ]
    if where is not None:
        where_parts.append(_compile_predicate(where, params))

    return (
        f"SELECT {', '.join(select_cols)}\n"
        f"FROM {fact_source}\n"
        f"WHERE {' AND '.join(where_parts)}\n"
        f"GROUP BY {', '.join(group_cols)}"
    )


def _scope_predicates(security_context: SecurityContext, params: _ParamPool) -> list[str]:
    parts = []
    if security_context.allowed_companies is not None:
        parts.append(f"company IN {params.add(sorted(security_context.allowed_companies), 'Array(String)')}")
    if security_context.allowed_geo_countries is not None:
        parts.append(f"geo_country IN {params.add(sorted(security_context.allowed_geo_countries), 'Array(String)')}")
    return parts


# ---- WHERE predicate compilation --------------------------------------------


def _compile_predicate(node: object, params: _ParamPool) -> str:
    if isinstance(node, ast.BoolPred):
        left = _compile_predicate(node.left, params)
        right = _compile_predicate(node.right, params)
        return f"({left} {node.op} {right})"
    if isinstance(node, ast.Comparison):
        if node.op == "IN":
            placeholder = params.add([_literal_value(v) for v in node.values], "Array(String)")
            return f"{node.dim} IN {placeholder}"
        if node.op == "NOT IN":
            placeholder = params.add([_literal_value(v) for v in node.values], "Array(String)")
            return f"{node.dim} NOT IN {placeholder}"
        value = _literal_value(node.values[0])
        ch_type = "Float64" if isinstance(value, float) else "String"
        placeholder = params.add(value, ch_type)
        return f"{node.dim} {node.op} {placeholder}"
    raise TypeError(f"unhandled predicate node: {node!r}")


def _literal_value(node: object) -> object:
    if isinstance(node, ast.StringLit):
        return node.value
    if isinstance(node, ast.NumberLit):
        return node.value
    raise TypeError(f"unhandled literal node: {node!r}")


# ---- rollup CTE --------------------------------------------------------------


def _build_rollup_cte(leaves: dict[str, Measure], by_dims: list[str], rollup_dims: list[str]) -> str:
    period_in_grain = "period_month" in rollup_dims

    agg_exprs = []
    for name, m in leaves.items():
        if m.kind == MeasureKind.SEMI_ADDITIVE:
            agg_exprs.append(f"sum(leaf_{name}) AS agg_{name}" if period_in_grain else f"argMax(leaf_{name}, period_month) AS agg_{name}")
        else:  # ADDITIVE leaf
            agg_exprs.append(f"sum(leaf_{name}) AS agg_{name}")

    select_cols = rollup_dims + agg_exprs
    group_clause = f"GROUP BY {', '.join(rollup_dims)}" if rollup_dims else ""
    return f"SELECT {', '.join(select_cols)}\nFROM monthly\n{group_clause}".strip()


# ---- final SELECT ------------------------------------------------------------


def _value_expr(measure: Measure) -> str:
    if isinstance(measure, (AccountAmountMeasure, DistinctCountMeasure)):
        return f"agg_{measure.name}"
    if isinstance(measure, ComputedAdditiveMeasure):
        parts = []
        for i, (sign, term_name) in enumerate(measure.terms):
            term_expr = _value_expr(MEASURES[term_name])
            parts.append(term_expr if i == 0 and sign == "+" else f"{sign} {term_expr}")
        return "(" + " ".join(parts) + ")"
    if isinstance(measure, RatioMeasure):
        num = _value_expr(MEASURES[measure.numerator])
        den = _value_expr(MEASURES[measure.denominator])
        # toFloat64: ClickHouse Decimal / Decimal keeps a narrow inferred
        # scale (e.g. 2 places), which silently truncates a genuinely
        # fractional ratio like gross_margin_pct.
        return f"(toFloat64({num}) / nullIf(toFloat64({den}), 0))"
    raise TypeError(f"unhandled measure type: {measure!r}")


def _build_final_select(measure_specs: list[_QueryMeasure], rollup_dims: list[str]) -> str:
    partition_dims = [d for d in rollup_dims if d != "period_month"]

    cols = list(rollup_dims)
    for spec in measure_specs:
        base_expr = _value_expr(spec.measure)
        if spec.time_func is None:
            cols.append(f"{base_expr} AS {spec.alias}")
            continue
        cols.append(f"{_time_function_expr(spec, base_expr, partition_dims)} AS {spec.alias}")
    return ", ".join(cols)


def _window_over(partition_dims: list[str], rows_clause: str | None = None) -> str:
    partition = f"PARTITION BY {', '.join(partition_dims)} " if partition_dims else ""
    rows = f" {rows_clause}" if rows_clause else ""
    return f"OVER ({partition}ORDER BY period_month{rows})"


def _time_function_expr(spec: _QueryMeasure, base_expr: str, partition_dims: list[str]) -> str:
    func, n = spec.time_func, spec.time_arg
    over = _window_over(partition_dims)

    if func == "PRIOR":
        return f"lagInFrame({base_expr}, {n}) {over}"
    if func == "LEAD":
        return f"leadInFrame({base_expr}, {n}) {over}"
    if func == "YOY":
        prior = f"lagInFrame({base_expr}, 12) {over}"
        return f"(({base_expr} - {prior}) / nullIf({prior}, 0))"
    if func == "CAGR":
        prior = f"lagInFrame({base_expr}, {n}) {over}"
        return f"(pow({base_expr} / nullIf({prior}, 0), 12.0 / {n}) - 1)"
    if func == "YTD":
        return f"sum({base_expr}) {_window_over(partition_dims, 'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW')}"
    if func == "QTD":
        return f"sum({base_expr}) {_window_over([*partition_dims, 'toStartOfQuarter(period_month)'], 'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW')}"
    if func == "MTD":
        # Monthly is the finest grain this cube carries, so month-to-date is
        # simply the month's own value -- no window needed.
        return base_expr
    if func == "ROLLING":
        return f"sum({base_expr}) {_window_over(partition_dims, f'ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW')}"
    raise UnsupportedQueryShapeError(f"unhandled time function: {func}")


# ---- BRIDGE: the matched plan/actual population ------------------------------
#
# The bridge needs row-level (plan row, actual row) pairs -- quantity, price
# and FX separately -- not the pre-aggregated measures compile() returns, so
# it has its own statement shape. It is still emitted here, under the same
# contract: registry-resolved names only, scope injected, every literal bound.

_BRIDGE_ROLLUP_DIMS = frozenset({"company", "account", "practice", "grade"})


@dataclass(frozen=True)
class CompiledBridge:
    compiled: CompiledQuery
    measure: str
    kind: str  # "revenue" | "cost" -- which leg set the decomposition uses
    by: tuple[str, ...]


def compile_bridge(
    query: ast.Query,
    security_context: SecurityContext,
    resolved_vintage: int | None = None,
) -> CompiledBridge:
    check_node(query)

    if query.period is None:
        raise UnsupportedQueryShapeError("FOR PERIOD is required on every query -- every result must be bounded")
    if not query.bridge or query.compare is None:
        raise UnsupportedQueryShapeError(
            "BRIDGE needs COMPARE PLAN pv='...' TO ACTUAL: it decomposes the gap between one plan and the ledger"
        )
    if query.limit is not None:
        raise UnsupportedQueryShapeError("LIMIT does not apply to BRIDGE; narrow it with WHERE or BY instead")
    if len(query.measures) != 1 or not isinstance(query.measures[0], ast.Ident):
        raise UnsupportedQueryShapeError("BRIDGE decomposes exactly one bare measure, e.g. SELECT services_revenue ...")

    name = query.measures[0].name
    measure = resolve_metric_name(name)
    if not isinstance(measure, AccountAmountMeasure) or measure.column != "amount_functional":
        raise UnsupportedQueryShapeError(
            f"{name!r} cannot be bridged: BRIDGE splits an additive account-amount measure into quantity, price and FX legs"
        )
    kind = _bridge_kind(measure)

    by = tuple(dict.fromkeys(query.by))
    unsupported = [d for d in by if d not in _BRIDGE_ROLLUP_DIMS]
    if unsupported:
        raise UnsupportedQueryShapeError(
            f"BRIDGE rolls up BY {sorted(_BRIDGE_ROLLUP_DIMS)} only; got {unsupported}"
        )

    start, end = period.range_bounds(query.period)
    _check_cost_budget(start, end, security_context)
    compiled = compile_matched_rows(
        security_context,
        start,
        end,
        resolved_vintage,
        plan_version=query.compare.plan_version,
        scenario_id=query.compare.scenario or "base",
        accounts=measure.accounts,
        where=query.where,
    )
    return CompiledBridge(compiled=compiled, measure=name, kind=kind, by=by)


def _bridge_kind(measure: AccountAmountMeasure) -> str:
    if all(a.startswith("4") for a in measure.accounts):
        return "revenue"
    if all(a.startswith("5") for a in measure.accounts):
        return "cost"
    raise UnsupportedQueryShapeError(
        f"{measure.name!r} mixes revenue and cost accounts; bridge each side on its own measure"
    )


def compile_matched_rows(
    security_context: SecurityContext,
    start,
    end,
    resolved_vintage: int | None,
    plan_version: str,
    scenario_id: str,
    accounts: tuple[str, ...] | None = None,
    where: object = None,
) -> CompiledQuery:
    """Plan (`fact_plan_line`) joined to actual (vintage-aware) on the full
    matched key `(company, period_month, account, dim_signature_hash)`, over
    [start, end), with plan and actual FX looked up to USD. Only keys present
    on both sides survive -- a one-sided line has no gap to decompose."""
    params = _ParamPool()
    plan_version_p = params.add(plan_version, "String")
    scenario_p = params.add(scenario_id, "String")
    plan_filters = _matched_side_filters(security_context, start, end, accounts, where, params)
    actual_filters = _matched_side_filters(security_context, start, end, accounts, where, params)

    sql = f"""
SELECT
    p.company AS company,
    p.period_month AS period_month,
    p.account AS account,
    p.dim_signature_hash AS dim_signature_hash,
    p.practice AS practice,
    p.grade AS grade,
    p.quantity AS plan_qty,
    p.unit_price AS plan_price,
    fx_plan.rate AS plan_fx,
    a.quantity AS actual_qty,
    a.unit_price AS actual_price,
    fx_actual.rate AS actual_fx
FROM
(
    SELECT company, period_month, account, dim_signature_hash, practice, grade,
           quantity, unit_price, functional_currency
    FROM fact_plan_line FINAL
    WHERE plan_version = {plan_version_p} AND scenario_id = {scenario_p} AND {plan_filters}
) AS p
INNER JOIN
(
    SELECT company, period_month, account, dim_signature_hash, quantity, unit_price, functional_currency
    FROM {actual_fact_source(resolved_vintage)}
    WHERE {actual_filters}
) AS a
ON p.company = a.company
   AND p.period_month = a.period_month
   AND p.account = a.account
   AND p.dim_signature_hash = a.dim_signature_hash
INNER JOIN dim_fx_plan AS fx_plan
    ON fx_plan.plan_version = {plan_version_p}
       AND fx_plan.period_month = p.period_month
       AND fx_plan.from_currency = p.functional_currency
       AND fx_plan.to_currency = 'USD'
INNER JOIN dim_fx_actual AS fx_actual
    ON fx_actual.period_month = a.period_month
       AND fx_actual.from_currency = a.functional_currency
       AND fx_actual.to_currency = 'USD'
"""
    return CompiledQuery(sql=sql, params=params.params, vintage=resolved_vintage)


def _matched_side_filters(security_context, start, end, accounts, where, params: _ParamPool) -> str:
    parts = [
        f"period_month >= {params.add(start.isoformat(), 'Date')}",
        f"period_month < {params.add(end.isoformat(), 'Date')}",
        *_scope_predicates(security_context, params),
    ]
    if accounts is not None:
        parts.append(f"account IN {params.add(list(accounts), 'Array(String)')}")
    if where is not None:
        parts.append(_compile_predicate(where, params))
    return " AND ".join(parts)


# ---- drill-through: the cube rows behind a bridge line ------------------------


def compile_drill_through(
    signature_hashes: list[bytes],
    resolved_vintage: int | None,
    security_context: SecurityContext,
    row_limit: int,
) -> CompiledQuery:
    params = _ParamPool()
    where_parts = [
        f"dim_signature_hash IN {params.add(signature_hashes, 'Array(String)')}",
        *_scope_predicates(security_context, params),
    ]
    sql = (
        "SELECT company, period_month, account, dim_signature_hash, quantity, amount_functional\n"
        f"FROM {actual_fact_source(resolved_vintage)}\n"
        f"WHERE {' AND '.join(where_parts)}\n"
        "ORDER BY company, period_month, account\n"
        f"LIMIT {params.add(row_limit, 'UInt32')}"
    )
    return CompiledQuery(sql=sql, params=params.params, vintage=resolved_vintage)
