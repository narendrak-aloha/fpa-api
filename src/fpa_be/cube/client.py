"""Read-only ClickHouse wrapper -- the only thing in the codebase that ever
executes compiler-emitted SQL against the cube. Every result carries the
vintage it actually read, all the way out to the caller (and eventually the
UI): a query answer without its vintage is not trustworthy.
"""

import os
from dataclasses import dataclass

import clickhouse_connect

from fpa_be.compiler.compile import CompiledQuery, compile
from fpa_be.compiler.security import SecurityContext
from fpa_be.cube.vintage import resolve_vintage
from fpa_be.dsl import ast_nodes as ast


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

    def run(self, query: ast.Query, security_context: SecurityContext) -> QueryResult:
        """The only path from a parsed+typechecked query to a cube answer:
        resolve AS OF -> compile (parameterize, scope-inject, cost-budget) ->
        execute. No caller of this method ever sees raw SQL."""
        resolved_vintage = self.resolve_vintage(query.as_of)
        compiled = compile(query, security_context, resolved_vintage=resolved_vintage)
        result = self._client.query(compiled.sql, parameters=compiled.params)
        return QueryResult(
            columns=result.column_names,
            rows=result.result_rows,
            vintage=resolved_vintage,
            compiled=compiled,
        )
