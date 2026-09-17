"""Safe Agno-facing tools backed by the existing deterministic compiler."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fpa_project.dsl.compiler import SecurityContext, compile_query
from fpa_project.dsl.errors import DSLValidationError, ParseError
from fpa_project.dsl.formula import parse_formula, referenced_names, validate_formula
from fpa_project.dsl.parser import parse_query
from fpa_project.dsl.schema import Schema

from .hooks import MaskingGateHook
from .logging_utils import ExternalAuditLogger, configure_terminal_logging, log_event
from .masking import mask_for_llm
from .models import DriverProposal, QueryToolResult, ToolError, UserScope


def clickhouse_executor(client: Any) -> Callable[[str, Mapping[str, Any]], Iterable[Mapping[str, Any]]]:
    """Adapt a connected ``clickhouse-connect`` client to the safe tool boundary.

    The client is supplied by the application after authentication. This module
    never creates connections and never executes mutating statements.
    """
    def execute(sql: str, params: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        result = client.query(sql, parameters=dict(params))
        if hasattr(result, "named_results"):
            return result.named_results()
        if hasattr(result, "result_rows") and hasattr(result, "column_names"):
            return [dict(zip(result.column_names, row)) for row in result.result_rows]
        raise TypeError("ClickHouse result must expose named_results() or result_rows/column_names")
    return execute


class FPATools:
    """Tool collection.  Compiled SQL never appears in a tool result."""

    def __init__(
        self,
        scope: UserScope,
        *,
        schema: Schema | None = None,
        executor: Callable[[str, Mapping[str, Any]], Iterable[Mapping[str, Any]]] | None = None,
        masking_hook: MaskingGateHook | None = None,
        audit_logger: ExternalAuditLogger | None = None,
    ):
        self.scope = scope
        self.schema = schema or Schema()
        self.executor = executor
        self.masking_hook = masking_hook or MaskingGateHook()
        self.draft_drivers: list[DriverProposal] = []
        self.logger = configure_terminal_logging()
        self.audit_logger = audit_logger or ExternalAuditLogger()

    def list_metrics(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "kind": metadata.get("kind"), "available": metadata.get("available", True),
             "allowed_operations": ["RECOMPUTE"] if metadata.get("kind") == "ratio" else ["SUM"]}
            for name, metadata in sorted(self.schema.metrics.items())
        ]

    def list_dimensions(self) -> list[dict[str, Any]]:
        return [{"name": name, "allowed_filter_operators": ["=", "!=", "IN", "NOT IN"]}
                for name in self.schema.data["planning_dimensions"]]

    def run_finops_query(self, dsl: str) -> QueryToolResult:
        # Scope rejection happens before parsing/compilation so an unscoped
        # caller cannot use error behavior to probe the cube.
        log_event(self.logger, "query_received")
        self.masking_hook.before_tool({"dsl": dsl}, user_id=self.scope.user_id)
        if not self.scope.allowed_companies:
            log_event(self.logger, "scope_rejected", status="REJECTED_SCOPE")
            return QueryToolResult(status="REJECTED_SCOPE", errors=[ToolError(code="REJECTED_SCOPE", message="no allowed company scope")])
        log_event(self.logger, "scope_injected")
        try:
            # SecurityContext is constructed from authenticated scope, not DSL
            # fields or model output.
            compiled = compile_query(dsl, self.schema, SecurityContext(
                self.scope.allowed_companies, self.scope.max_estimated_rows,
            ))
        except (ParseError, DSLValidationError, ValueError) as exc:
            log_event(self.logger, "dsl_validation_failed", status="VALIDATION_ERROR", error_type=type(exc).__name__)
            return QueryToolResult(status="VALIDATION_ERROR", errors=[ToolError(code="INVALID_QUERY", message=str(exc))])
        log_event(self.logger, "query_compiled", estimated_rows=compiled.estimated_rows)
        if self.executor is None:
            log_event(self.logger, "query_execution_skipped", status="EXECUTION_ERROR", code="NO_EXECUTOR")
            return QueryToolResult(status="EXECUTION_ERROR", errors=[ToolError(code="NO_EXECUTOR", message="no ClickHouse executor configured")])
        try:
            log_event(self.logger, "clickhouse_execution_started")
            self.audit_logger.record(
                "clickhouse",
                "request",
                {"sql": compiled.sql, "parameter_names": sorted(compiled.params)},
            )
            raw_rows = list(self.executor(compiled.sql, compiled.params))
            self.masking_hook.after_tool(raw_rows, user_id=self.scope.user_id)
            # Mask after execution too: dimension values can contain hostile
            # or sensitive master-data text even when the query was safe.
            rows = [mask_for_llm(dict(row)) for row in raw_rows]
            self.audit_logger.record("clickhouse", "response", {"rows": rows, "row_count": len(rows)})
            columns = list(rows[0]) if rows else []
            log_event(self.logger, "clickhouse_execution_completed", status="SUCCESS", row_count=len(rows))
            log_event(self.logger, "result_rows_masked", row_count=len(rows))
            return QueryToolResult(status="SUCCESS", rows=rows, columns=columns, row_count=len(rows))
        except Exception as exc:
            self.audit_logger.record("clickhouse", "response", {"status": "ERROR", "error_type": type(exc).__name__})
            log_event(self.logger, "clickhouse_execution_failed", status="EXECUTION_ERROR", error_type=type(exc).__name__)
            return QueryToolResult(status="EXECUTION_ERROR", errors=[ToolError(code="EXECUTION_ERROR", message=str(exc))])

    def propose_driver(self, expr_dsl: str, name: str = "draft_driver") -> DriverProposal:
        # Driver proposals are validated and stored as drafts only; there is
        # deliberately no mutation path into the production schema.
        try:
            node = parse_formula(expr_dsl)
            validate_formula(node, self.schema)
            proposal = DriverProposal(name=name, expr_dsl=expr_dsl, references=sorted(referenced_names(node)))
        except (ParseError, DSLValidationError, ValueError) as exc:
            proposal = DriverProposal(name=name, expr_dsl=expr_dsl, errors=[ToolError(code="INVALID_DRIVER", message=str(exc))])
        self.draft_drivers.append(proposal)
        log_event(self.logger, "driver_proposal_created", status="DRAFT", code="INVALID_DRIVER" if proposal.errors else "VALID")
        return proposal
