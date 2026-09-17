"""Deterministic orchestration around the optional Agno model team."""

from __future__ import annotations

from pydantic import ValidationError

from fpa_project.dsl.parser import parse_query

from .masking import mask_for_llm, mask_request_text
from .hooks import ArithmeticVerificationPostHook, MaskingGateHook
from .logging_utils import ExternalAuditLogger, configure_terminal_logging, log_event
from .models import AgentFPAResponse, AgentPlan, ModelChangeProposal, PlanningRequest, PlanningResponse, ValidationIssue
from .registry import PlanningRegistry
from .tools import FPATools
from .api_keys import APIKeyRotator
import uuid


def validate_dsl(dsl: str, registry: PlanningRegistry | None = None) -> list[ValidationIssue]:
    """Validate syntax and planning registry semantics without compiling SQL."""
    # Syntax is checked first; registry checks only run against a typed AST.
    registry = registry or PlanningRegistry()
    try:
        query = parse_query(dsl)
    except Exception as exc:
        return [ValidationIssue(code="INVALID_DSL", message=str(exc))]
    return [ValidationIssue(code=i.code, message=i.message, field=i.field) for i in registry.validate(query)]


class FinOpsPlanner:
    """Safe boundary for an Agno team's proposed plan.

    ``generate`` accepts a model-produced ``AgentPlan`` (or a dict matching
    it), validates it, and never invokes the compiler or a database.
    """

    def __init__(self, registry: PlanningRegistry | None = None):
        self.registry = registry or PlanningRegistry()

    def prepare(self, request: PlanningRequest) -> PlanningRequest:
        # Mask both free-form text and structured context before any optional
        # model adapter can see the request.
        return PlanningRequest(request=mask_request_text(request.request), context=mask_for_llm(request.context))

    def generate(self, request: PlanningRequest, candidate: AgentPlan | dict) -> PlanningResponse:
        masked = self.prepare(request).context
        try:
            plan = candidate if isinstance(candidate, AgentPlan) else AgentPlan.model_validate(candidate)
        except ValidationError as exc:
            return PlanningResponse(status="ERROR", errors=[ValidationIssue(code="INVALID_OUTPUT", message=str(exc))], masked_context=masked)
        if plan.out_of_scope:
            return PlanningResponse(status="VALID", plan=plan, masked_context=masked)
        # A typed model response is still untrusted until its DSL is validated.
        errors = validate_dsl(plan.dsl, self.registry)
        return PlanningResponse(status="VALID" if not errors else "ERROR", plan=plan if not errors else None, errors=errors, masked_context=masked)

    @staticmethod
    def propose_model_change(reason: str, *, metrics: list[str] | None = None, dimensions: list[str] | None = None) -> ModelChangeProposal:
        """Create a draft for a human reviewer; deliberately cannot mutate registry data."""
        return ModelChangeProposal(
            reason=reason,
            proposed_metrics=metrics or [],
            proposed_dimensions=dimensions or [],
        )


class FPAOrchestrator:
    """Final deterministic gate from an agent plan to an executed response."""

    MAX_SYNTAX_RETRIES = 5

    def __init__(self, tools: FPATools, *, masking_hook: MaskingGateHook | None = None, audit_logger: ExternalAuditLogger | None = None, api_key_rotator: APIKeyRotator | None = None):
        self.tools = tools
        self.planner = FinOpsPlanner(PlanningRegistry(tools.schema))
        self.masking_hook = masking_hook or tools.masking_hook
        self.arithmetic_hook = ArithmeticVerificationPostHook()
        self.logger = configure_terminal_logging()
        self.audit_logger = audit_logger or tools.audit_logger
        self.api_key_rotator = api_key_rotator or APIKeyRotator.from_env()

    def finalize(self, request: PlanningRequest, candidate: AgentPlan | dict, narrative: str | None = None) -> AgentFPAResponse:
        run_id = uuid.uuid4().hex[:12]
        log_event(self.logger, "request_received", run_id=run_id)
        prepared = self.planner.prepare(request)
        log_event(self.logger, "context_masked", run_id=run_id)
        # Validate the candidate before allowing it to reach the scoped tool.
        result = self.planner.generate(request, candidate)
        dsl = result.plan.dsl if result.plan else ""
        assumptions = result.plan.assumptions if result.plan else []
        if result.status != "VALID":
            log_event(self.logger, "dsl_validation_failed", run_id=run_id, status="VALIDATION_ERROR", code=result.errors[0].code if result.errors else "INVALID_OUTPUT")
            message = "; ".join(issue.message for issue in result.errors)
            return AgentFPAResponse(user_query=request.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR", narrative_explanation=narrative, error_message=message)
        if result.plan.out_of_scope:
            # Nothing is compiled or executed, so the refusal cannot cite numbers either.
            ok, _ = self.arithmetic_hook.verify(result.plan.explanation, [])
            explanation = result.plan.explanation if ok else "This question cannot be answered from the FP&A cube."
            log_event(self.logger, "request_out_of_scope", run_id=run_id, status="OUT_OF_SCOPE")
            return AgentFPAResponse(user_query=prepared.request, execution_status="OUT_OF_SCOPE", narrative_explanation=explanation, assumptions=assumptions)
        # FPATools injects authenticated scope, compiles parameterized SQL and
        # masks result rows before they are returned to this orchestrator.
        query_result = self.tools.run_finops_query(dsl)
        log_event(self.logger, "scoped_query_completed", run_id=run_id, status=query_result.status, row_count=query_result.row_count)
        if query_result.status == "REJECTED_SCOPE":
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="REJECTED_SCOPE", narrative_explanation=narrative, assumptions=assumptions, error_message=query_result.errors[0].message)
        if query_result.status != "SUCCESS":
            message = "; ".join(error.message for error in query_result.errors)
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR", narrative_explanation=narrative, assumptions=assumptions, error_message=message)
        ok, error = self.arithmetic_hook.verify(narrative, query_result.rows)
        if not ok:
            log_event(self.logger, "arithmetic_verification_failed", run_id=run_id, status="VALIDATION_ERROR")
            return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="VALIDATION_ERROR", assumptions=assumptions, error_message=error)
        log_event(self.logger, "arithmetic_verification_completed", run_id=run_id, status="SUCCESS")
        log_event(self.logger, "response_completed", run_id=run_id, status="SUCCESS", row_count=query_result.row_count)
        return AgentFPAResponse(user_query=prepared.request, generated_dsl=dsl, execution_status="SUCCESS", narrative_explanation=narrative, assumptions=assumptions, cited_data_rows=query_result.rows)

    def finalize_with_retries(self, request: PlanningRequest, candidates: list[AgentPlan | dict], narrative: str | None = None) -> AgentFPAResponse:
        """Try at most five candidate plans, retrying only validation failures."""
        last: AgentFPAResponse | None = None
        for candidate in candidates[:self.MAX_SYNTAX_RETRIES]:
            last = self.finalize(request, candidate, narrative)
            if last.execution_status == "SUCCESS" or last.execution_status == "REJECTED_SCOPE":
                return last
        return last or AgentFPAResponse(
            user_query=request.request,
            execution_status="VALIDATION_ERROR",
            error_message="no candidate DSL plan supplied",
        )

    def run_with_team(self, team: object, request: PlanningRequest) -> AgentFPAResponse:
        """Run an Agno Team with a hard five-attempt NL-to-DSL cap."""
        prepared = self.planner.prepare(request)
        run_id = uuid.uuid4().hex[:12]
        self.masking_hook.before_model(
            {"request": prepared.request, "context": prepared.context},
            user_id=self.tools.scope.user_id,
        )
        members = getattr(team, "members", [])
        last_result: AgentFPAResponse | None = None
        correction = prepared.request
        # Retry only bounded model/validation failures; execution and scope
        # outcomes are terminal and are not hidden by another model attempt.
        for attempt in range(1, self.MAX_SYNTAX_RETRIES + 1):
            log_event(self.logger, "team_attempt_started", run_id=run_id, attempt=attempt, max_attempts=self.MAX_SYNTAX_RETRIES)
            if attempt == 1 or not members:
                prompt = correction
                caller = team.run
            else:
                prompt = (
                    f"Return a corrected AgentPlan for this request: {prepared.request}. "
                    f"Previous attempt failed: {correction}. "
                    "Use plain FinOpsExpr text beginning with SELECT; do not use square brackets, JSON, Markdown, SQL, or commentary in dsl."
                )
                caller = members[0].run
            self.audit_logger.record("agno", "request", {"request": prompt, "attempt": attempt}, run_id=run_id)
            try:
                self.api_key_rotator.apply_to_team(team)
                raw = caller(prompt, user_id=self.tools.scope.user_id, dependencies={"scope": self.tools.scope.model_dump()})
                content = getattr(raw, "content", raw)
                self.audit_logger.record("agno", "response", content, run_id=run_id)
                log_event(self.logger, "team_attempt_completed", run_id=run_id, attempt=attempt, status="RECEIVED")
            except Exception as exc:
                log_event(self.logger, "team_attempt_failed", run_id=run_id, attempt=attempt, status="ERROR", error_type=type(exc).__name__)
                correction = f"provider error: {type(exc).__name__}"
                last_result = AgentFPAResponse(user_query=prepared.request, execution_status="VALIDATION_ERROR", error_message="model attempt failed")
                continue
            if isinstance(content, AgentFPAResponse):
                candidate = {"dsl": content.generated_dsl, "explanation": content.narrative_explanation or ""}
                narrative = content.narrative_explanation
            elif isinstance(content, AgentPlan):
                candidate, narrative = content, content.explanation
            elif isinstance(content, dict) and (content.get("dsl") or content.get("out_of_scope")):
                candidate, narrative = content, content.get("explanation") or content.get("narrative_explanation")
            else:
                correction = "response did not contain a non-empty AgentPlan.dsl"
                last_result = AgentFPAResponse(user_query=prepared.request, execution_status="VALIDATION_ERROR", error_message=correction)
                continue
            result = self.finalize(request, candidate, narrative)
            if result.execution_status in {"SUCCESS", "REJECTED_SCOPE", "OUT_OF_SCOPE"}:
                return result
            last_result = result
            correction = result.error_message or "DSL validation failed"
        log_event(self.logger, "team_attempts_exhausted", run_id=run_id, status="VALIDATION_ERROR", max_attempts=self.MAX_SYNTAX_RETRIES)
        return last_result or AgentFPAResponse(user_query=prepared.request, execution_status="VALIDATION_ERROR", error_message="five model attempts exhausted")
