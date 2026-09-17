from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ast import Comparison, Literal, Measure, Period, Query, TimeFunction
from .errors import DSLValidationError
from .parser import parse_query
from .schema import Schema


@dataclass(frozen=True)
class SecurityContext:
    """Authorisation and resource limits supplied by the authenticated caller."""

    allowed_companies: frozenset[str] | None = None
    max_estimated_rows: int = 1_000_000

    def __post_init__(self) -> None:
        if self.max_estimated_rows < 0:
            raise ValueError("max_estimated_rows must be non-negative")
        if self.allowed_companies is not None and not all(
            isinstance(company, str) and company for company in self.allowed_companies
        ):
            raise ValueError("allowed_companies must contain non-empty strings")


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: dict[str, Any]
    estimated_rows: int = 0
    vintage: str = "current"


class Compiler:
    def __init__(self, schema: Schema | None = None, security_context: SecurityContext | None = None):
        self.schema = schema or Schema()
        self.security_context = security_context or SecurityContext()
        self.params: dict[str, Any] = {}
        self.param_index = 0

    def compile(self, query: Query) -> CompiledQuery:
        # Validate before producing SQL; callers should never receive a
        # partially trusted query representation.
        self.validate_query(query)
        if query.bridge:
            # Bridge queries compare plan and actual rows at their matched
            # physical grain, so they use a dedicated compilation path.
            return self.compile_bridge(query)

        source = "fact_plan_line" if query.plan else "fact_gl_actual"
        alias = "p" if query.plan else "a"
        # FINAL collapses ReplacingMergeTree rows for current actual reads.
        source_sql = f"fpa_cube.{source} AS {alias}" if query.plan else f"fpa_cube.{source} AS {alias} FINAL"
        select_parts = [
            f"{self.measure_sql(m.name, alias)} AS {m.alias or self.default_alias(m.name)}"
            for m in query.measures
        ]
        group_parts = list(query.dimensions)
        sql = f"SELECT {', '.join(group_parts + select_parts)} FROM {source_sql}"
        where_parts, having_parts = self.where_sql(query, alias)
        if where_parts:
            sql += " WHERE " + self.combine_predicates(where_parts, query)
        if group_parts:
            sql += " GROUP BY " + ", ".join(group_parts)
        if having_parts:
            sql += " HAVING " + self.combine_predicates(having_parts, query)
        if query.limit is not None:
            sql += f" LIMIT {query.limit}"
        # Apply the caller's cost budget before returning executable SQL.
        estimated = self.estimate_rows(query)
        self.enforce_budget(estimated)
        return CompiledQuery(sql, self.params, estimated, query.as_of or "current")

    def validate_query(self, query: Query) -> None:
        # Keep semantic validation separate from SQL generation so invalid
        # names and combinations fail before a database call is possible.
        if query.plan and query.plan.scenario not in self.schema.scenarios:
            raise DSLValidationError(f"unknown scenario: {query.plan.scenario}")
        for dimension in query.dimensions:
            self.schema.require_dimension(dimension)
        for comparison in query.predicates:
            self.validate_comparison(comparison)
        for measure in query.measures:
            self.validate_measure(measure)
            if isinstance(measure.name, str) and self.schema.metrics[measure.name].get("kind") == "semi_additive":
                if query.period and query.period.start != query.period.end:
                    raise DSLValidationError(
                        f"semi-additive measure requires a closing period: {measure.name}"
                    )
        if query.bridge and query.plan is None:
            raise DSLValidationError("BRIDGE requires a plan comparison")
        if query.as_of and query.plan:
            raise DSLValidationError("AS OF is only valid for actual-ledger queries")
        if query.limit is not None and query.limit < 0:
            raise DSLValidationError("LIMIT must be non-negative")
        if query.period and self.period_start(query.period.start) >= self.period_end(query.period.end):
            raise DSLValidationError("period range must end after it starts")
        for measure in query.measures:
            if measure.alias is not None and not self.is_safe_identifier(measure.alias):
                raise DSLValidationError(f"invalid measure alias: {measure.alias}")

    def validate_measure(self, measure: Measure) -> None:
        # Time functions validate their underlying metric and offset as one
        # unit, so nested expressions cannot bypass metric checks.
        for name in self.measure_names(measure.name):
            self.schema.require_metric(name)
        if isinstance(measure.name, TimeFunction) and measure.name.name in {"PRIOR", "LEAD", "ROLLING"}:
            if measure.name.offset is not None and measure.name.offset <= 0:
                raise DSLValidationError(f"{measure.name.name} offset must be positive")

    def measure_names(self, value: str | TimeFunction) -> set[str]:
        # Flatten nested time functions to the base metric names required by
        # the schema contract.
        return self.measure_names(value.metric) if isinstance(value, TimeFunction) else {value}

    def validate_comparison(self, comparison: Comparison) -> None:
        # Predicates may target dimensions or measures, but membership must
        # contain at least one literal to produce valid SQL.
        if comparison.field not in self.schema.dimensions and comparison.field not in self.schema.metric_names:
            raise DSLValidationError(f"unknown predicate field: {comparison.field}")
        if comparison.field in self.schema.metric_names:
            self.schema.require_metric(comparison.field)
        if comparison.operator in {"IN", "NOT IN"} and not comparison.values:
            raise DSLValidationError("membership predicate cannot be empty")

    def where_sql(self, query: Query, alias: str) -> tuple[list[str], list[str]]:
        where: list[str] = []
        having: list[str] = []
        for comparison in query.predicates:
            (having if comparison.field in self.schema.metric_names else where).append(
                self.comparison_sql(comparison, alias)
            )
        if query.period:
            where.append(self.period_sql(query.period, alias))
        if query.as_of and not query.plan:
            # Resolve the latest close known at the requested timestamp;
            # historical reads must not silently use today's latest vintage.
            placeholder = self.bind(query.as_of, "DateTime")
            where.append(
                f"{alias}._version = (SELECT argMax(vintage, closed_at) "
                f"FROM fpa_cube.dim_ledger_vintage WHERE closed_at <= {placeholder})"
            )
        if self.security_context.allowed_companies is not None:
            # Scope is injected from authenticated context, never trusted from
            # model-generated DSL text.
            if not self.security_context.allowed_companies:
                where.append("0 = 1")
            else:
                values = tuple(Literal(c, True) for c in sorted(self.security_context.allowed_companies))
                where.append(self.comparison_sql(Comparison("company", "IN", values), alias))
        if query.plan:
            where.append(f"{alias}.plan_version = {self.bind(query.plan.version, 'String')}")
            where.append(f"{alias}.scenario_id = {self.bind(query.plan.scenario, 'String')}")
        return where, having

    def combine_predicates(self, predicates: list[str], query: Query) -> str:
        # Preserve the user's AND/OR connectors while parenthesizing each
        # combination so mixed predicates have deterministic precedence.
        result = predicates[0]
        for index, predicate in enumerate(predicates[1:]):
            connector = query.predicate_connectors[index] if index < len(query.predicate_connectors) else "AND"
            result = f"({result} {connector} {predicate})"
        return result

    def comparison_sql(self, comparison: Comparison, alias: str) -> str:
        # Measures become expressions in HAVING; dimensions remain qualified
        # columns in WHERE.
        field = f"{alias}.{comparison.field}"
        if comparison.field in self.schema.metric_names:
            field = self.measure_sql(comparison.field, alias)
        if comparison.operator in {"IN", "NOT IN"}:
            values = ", ".join(self.literal_sql(v) for v in comparison.values)
            return f"{field} {comparison.operator} ({values})"
        return f"{field} {comparison.operator} {self.literal_sql(comparison.values[0])}"

    def period_sql(self, period: Period, alias: str) -> str:
        # Use >= start and < end for every period, including ranges.
        start = self.period_start(period.start)
        end = self.period_end(period.end)
        return (
            f"{alias}.period_month >= toDate({self.bind(start, 'String')}) "
            f"AND {alias}.period_month < toDate({self.bind(end, 'String')})"
        )

    @staticmethod
    def period_start(period: str) -> str:
        # Normalize year, quarter, half-year, and month tokens to month starts.
        year = int(period[:4])
        if len(period) == 4:
            return f"{year:04d}-01-01"
        suffix = period[5:]
        month = ((int(suffix[1]) - 1) * 3 + 1 if suffix.startswith("Q")
                 else (int(suffix[1]) - 1) * 6 + 1 if suffix.startswith("H")
                 else int(suffix))
        return f"{year:04d}-{month:02d}-01"

    @classmethod
    def period_end(cls, period: str) -> str:
        # Return the first day after the requested period for half-open SQL.
        year = int(period[:4])
        if len(period) == 4:
            return f"{year + 1:04d}-01-01"
        suffix = period[5:]
        month = (int(suffix[1]) * 3 + 1 if suffix.startswith("Q")
                 else int(suffix[1]) * 6 + 1 if suffix.startswith("H")
                 else int(suffix) + 1)
        return f"{year + 1:04d}-01-01" if month == 13 else f"{year:04d}-{month:02d}-01"

    def measure_sql(self, value: str | TimeFunction, alias: str) -> str:
        if isinstance(value, TimeFunction):
            inner = self.measure_sql(value.metric, alias)
            window = "ORDER BY period_month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
            if value.name in {"PRIOR", "LEAD"}:
                offset = int(value.offset or 1)
                function = "lagInFrame" if value.name == "PRIOR" else "leadInFrame"
                return f"{function}({inner}, {offset}) OVER ({window})"
            if value.name == "YOY":
                # Ratios are recomputed from lagged values rather than summed
                # as independent percentages.
                return f"{inner} / nullIf(lagInFrame({inner}, 12) OVER ({window}), 0) - 1"
            if value.name == "ROLLING":
                offset = int(value.offset or 3)
                return f"sum({inner}) OVER (ORDER BY period_month ROWS {offset - 1} PRECEDING)"
            return inner
        return self.schema.metrics[value]["sql_expression"].replace("{a}", alias)

    def compile_bridge(self, query: Query) -> CompiledQuery:
        assert query.plan is not None
        dimensions = list(query.dimensions) or ["company"]
        group_sql = ", ".join(f"a.{d}" for d in dimensions)
        # Plan and actual only match on this complete four-column grain.
        join_keys = " AND ".join(
            f"a.{key} = p.{key}" for key in ("company", "period_month", "account", "dim_signature_hash")
        )
        where = [
            f"p.plan_version = {self.bind(query.plan.version, 'String')}",
            f"p.scenario_id = {self.bind(query.plan.scenario, 'String')}",
        ]
        if query.period:
            where.append(self.period_sql(query.period, "a"))
        if self.security_context.allowed_companies is not None:
            if not self.security_context.allowed_companies:
                where.append("0 = 1")
            else:
                values = tuple(Literal(c, True) for c in sorted(self.security_context.allowed_companies))
                where.append(self.comparison_sql(Comparison("company", "IN", values), "a"))
        if query.as_of:
            placeholder = self.bind(query.as_of, "DateTime")
            where.append(
                f"a._version = (SELECT argMax(vintage, closed_at) FROM fpa_cube.dim_ledger_vintage "
                f"WHERE closed_at <= {placeholder})"
            )
        for comparison in query.predicates:
            if comparison.field in self.schema.dimensions:
                where.append(self.comparison_sql(comparison, "a"))
        # The compiler emits price/volume and a residual foundation; the
        # deterministic bridge module adds mix and FX at report grain.
        sql = (
            "WITH matched AS (SELECT " + group_sql + ", "
            "sum(a.amount_functional) AS actual_amount, sum(p.amount_functional) AS plan_amount, "
            "sum(a.quantity) AS actual_quantity, sum(p.quantity) AS plan_quantity, "
            "sum(a.quantity * (a.unit_price - p.unit_price)) AS price_variance, "
            "sum((a.quantity - p.quantity) * p.unit_price) AS volume_variance "
            "FROM fpa_cube.fact_gl_actual AS a FINAL "
            "INNER JOIN fpa_cube.fact_plan_line AS p ON " + join_keys + " "
            "WHERE " + " AND ".join(where) + " GROUP BY " + group_sql + ") "
            "SELECT *, actual_amount - plan_amount AS group_gap, "
            "group_gap - (price_variance + volume_variance) AS residual FROM matched"
        )
        estimated = self.estimate_rows(query)
        self.enforce_budget(estimated)
        return CompiledQuery(sql, self.params, estimated, query.as_of or "current")

    def estimate_rows(self, query: Query) -> int:
        # This is a conservative guardrail used before execution, not a
        # replacement for ClickHouse's physical query planner.
        base = 231_390 if query.plan else 1_034_766
        if query.period:
            base //= 4 if "Q" in query.period.start else 2 if "H" in query.period.start else 1
        if query.dimensions:
            base //= max(1, 2 ** min(len(query.dimensions), 5))
        if query.limit is not None:
            base = min(base, query.limit)
        return max(1, base)

    def enforce_budget(self, estimated: int) -> None:
        if estimated > self.security_context.max_estimated_rows:
            raise DSLValidationError(
                f"query exceeds estimated row budget: {estimated} > {self.security_context.max_estimated_rows}"
            )

    @staticmethod
    def default_alias(value: str | TimeFunction) -> str:
        return value if isinstance(value, str) else value.name.lower()

    def literal_sql(self, literal: Literal) -> str:
        # Keep literal values in the parameter map so they cannot alter SQL
        # structure or identifier resolution.
        return self.bind(literal.value, "String" if literal.is_string else "Decimal64(6)")

    @staticmethod
    def is_safe_identifier(value: str) -> bool:
        """Allow only parser-compatible aliases before placing them in SQL text."""
        return bool(value) and value[0].isalpha() and all(
            character.isalnum() or character == "_" for character in value
        )

    def bind(self, value: Any, type_name: str) -> str:
        # Generate stable named placeholders and retain their typed values for
        # the ClickHouse client.
        name = f"p{self.param_index}"
        self.param_index += 1
        self.params[name] = value
        return "{" + name + ":" + type_name + "}"


def compile_query(
    source: str,
    schema: Schema | None = None,
    security_context: SecurityContext | None = None,
) -> CompiledQuery:
    return Compiler(schema, security_context).compile(parse_query(source))
