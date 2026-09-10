"""Read-only ClickHouse wrapper -- the only thing in the codebase that ever
executes compiler-emitted SQL against the cube for a caller. Every result
carries the vintage it actually read, all the way out to the caller (and
eventually the UI): a query answer without its vintage is not trustworthy.
"""

import os
from dataclasses import dataclass

import clickhouse_connect

from fpa_be.bridge.decompose import cost_bridge, revenue_bridge
from fpa_be.bridge.matched_rows import run_matched_rows
from fpa_be.compiler.compile import CompiledQuery, compile, compile_bridge
from fpa_be.compiler.security import SecurityContext
from fpa_be.cube.vintage import resolve_vintage
from fpa_be.dsl import ast_nodes as ast

_ROLLUP_ROOT_LABEL = "(all)"


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: list[tuple]
    vintage: int | None  # None means "latest"
    compiled: CompiledQuery


class CubeClient:
    """Wraps a clickhouse-connect client. Pass an existing client in tests;
    the default constructor reads connection settings from the environment
    the way every other service in this repo does."""

    def __init__(self, client=None):
        self._client = client or clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", "fpa"),
            database=os.environ.get("CLICKHOUSE_DATABASE", "fpa_cube"),
        )

    def resolve_vintage(self, as_of: ast.AsOf | None) -> int | None:
        return resolve_vintage(as_of, self._client)

    def execute(self, compiled: CompiledQuery):
        """Runs an already-compiled statement (e.g. compile_drill_through's)
        -- the SQL still comes only from the compiler."""
        return self._client.query(compiled.sql, parameters=compiled.params)

    def close(self) -> None:
        self._client.close()

    def run(self, query: ast.Query, security_context: SecurityContext) -> QueryResult:
        """The only path from a parsed+typechecked query to a cube answer:
        resolve AS OF -> compile (parameterize, scope-inject, cost-budget) ->
        execute. No caller of this method ever sees raw SQL."""
        resolved_vintage = self.resolve_vintage(query.as_of)
        if query.bridge:
            return self._run_bridge(query, security_context, resolved_vintage)
        compiled = compile(query, security_context, resolved_vintage=resolved_vintage)
        result = self._client.query(compiled.sql, parameters=compiled.params)
        return QueryResult(
            columns=result.column_names,
            rows=result.result_rows,
            vintage=resolved_vintage,
            compiled=compiled,
        )

    def _run_bridge(
        self, query: ast.Query, security_context: SecurityContext, resolved_vintage: int | None
    ) -> QueryResult:
        """One row per rollup node -- the root, then one per BY group -- each
        carrying its own legs, residual and tolerance, so a reader can check
        the bridge ties at every level rather than only at the top."""
        bridge = compile_bridge(query, security_context, resolved_vintage=resolved_vintage)
        matched = run_matched_rows(self._client, bridge.compiled)
        decompose = revenue_bridge if bridge.kind == "revenue" else cost_bridge

        nodes: list[tuple[tuple, list]] = [((_ROLLUP_ROOT_LABEL,) * len(bridge.by), matched)] if matched else []
        if bridge.by and matched:
            groups: dict[tuple, list] = {}
            for row in matched:
                groups.setdefault(tuple(getattr(row, d) for d in bridge.by), []).append(row)
            nodes.extend(sorted(groups.items()))

        rows = []
        leg_names: tuple[str, ...] = ()
        for key, members in nodes:
            result = decompose(members)
            leg_names = tuple(leg.name for leg in result.legs)
            rows.append(
                (
                    *key,
                    result.plan_total,
                    result.reported_actual_total,
                    result.reported_actual_total - result.plan_total,
                    *(leg.amount for leg in result.legs),
                    result.residual,
                    max(1.00, 0.01 * len(members)),
                    len(members),
                )
            )
        columns = (
            *bridge.by,
            "plan_total",
            "reported_actual_total",
            "gap",
            *leg_names,
            "residual",
            "tol",
            "matched_lines",
        )
        return QueryResult(columns=columns, rows=rows, vintage=resolved_vintage, compiled=bridge.compiled)
