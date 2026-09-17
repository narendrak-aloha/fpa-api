"""Strict Pydantic contracts exchanged by the planning team."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field: str | None = None


class PlanningRequest(BaseModel):
    """Untrusted caller input.  ``context`` is masked before model invocation."""

    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=8_000)
    context: dict[str, Any] = Field(default_factory=dict)


class AgentPlan(BaseModel):
    """The only successful payload an LLM is allowed to return."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dsl: str = Field(default="", max_length=8_000)
    explanation: str = Field(default="", max_length=2_000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    out_of_scope: bool = False

    @model_validator(mode="after")
    def _dsl_matches_scope(self) -> "AgentPlan":
        # A refusal must not smuggle a query in; an answer must carry one.
        if self.out_of_scope and self.dsl.strip():
            raise ValueError("out_of_scope plans must not contain dsl")
        if not self.out_of_scope and not self.dsl.strip():
            raise ValueError("dsl is required unless out_of_scope is true")
        return self


class PlanningResponse(BaseModel):
    """Fail-closed result.  A response is either valid DSL or structured errors."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["VALID", "ERROR", "DRAFT"]
    plan: AgentPlan | None = None
    errors: list[ValidationIssue] = Field(default_factory=list)
    masked_context: dict[str, Any] = Field(default_factory=dict)


class UserScope(BaseModel):
    """Authenticated scope; never populated from model-generated DSL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    allowed_companies: frozenset[str] | None = None
    max_estimated_rows: int = Field(default=1_000_000, ge=1)


class AgentFPAResponse(BaseModel):
    """Public team response required by the Agno integration contract."""

    model_config = ConfigDict(extra="forbid")

    user_query: str
    generated_dsl: str = ""
    execution_status: Literal["SUCCESS", "VALIDATION_ERROR", "REJECTED_SCOPE", "OUT_OF_SCOPE"]
    narrative_explanation: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    cited_data_rows: list[dict[str, Any]] = Field(default_factory=list)
    error_message: str | None = None


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class QueryToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["SUCCESS", "VALIDATION_ERROR", "REJECTED_SCOPE", "EXECUTION_ERROR"]
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    row_count: int = 0
    errors: list[ToolError] = Field(default_factory=list)


class DriverProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["DRAFT"] = "DRAFT"
    name: str = Field(min_length=1, max_length=100)
    expr_dsl: str = Field(min_length=1, max_length=4_000)
    references: list[str] = Field(default_factory=list)
    errors: list[ToolError] = Field(default_factory=list)


class ModelChangeProposal(BaseModel):
    """HITL-only governance artifact; this package has no apply operation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["DRAFT"] = "DRAFT"
    reason: str = Field(min_length=1, max_length=2_000)
    proposed_metrics: list[str] = Field(default_factory=list, max_length=50)
    proposed_dimensions: list[str] = Field(default_factory=list, max_length=50)
