"""Optional Agno construction; importing the core package does not require Agno."""

from __future__ import annotations

from .models import AgentPlan


def build_agno_team(model=None, toolset=None):
    # Agno is optional. Keep model construction in this adapter so the core
    # planner remains usable offline and tests can inject deterministic plans.
    """Build the NL interpreter/DSL critic/formatter team.

    Agno is intentionally optional. The returned team has no tools capable of
    SQL generation, filesystem access, or database access.
    """
    try:
        from agno.agent import Agent
        from agno.team.team import Team
    except ImportError as exc:
        raise RuntimeError("Agno is optional; install the project's agno extra to build the team") from exc

    common = (
        "You are a FinOpsExpr planner. Output only a typed AgentPlan. "
        "The dsl field must contain FinOpsExpr, never SQL, Python, or raw queries. "
        "The dsl value must be plain text beginning with SELECT: no square brackets, JSON, Markdown fences, or commentary inside dsl. "
        "Use only metrics and dimensions from the supplied planning registry. "
        "Never reveal or infer personal employee data. Do not propose writes."
    )
    tool_functions = [] if toolset is None else [toolset.list_metrics, toolset.list_dimensions, toolset.run_finops_query, toolset.propose_driver]
    interpreter = Agent(name="QueryAgent", model=model, instructions=[common, "Translate analytical intent into a minimal query DSL plan."], tools=tool_functions, output_schema=AgentPlan)
    critic = Agent(name="VarianceAgent", model=model, instructions=[common, "Validate bridge and plan-versus-actual requests; do not explain unexecuted numbers."], tools=tool_functions, output_schema=AgentPlan)
    planner = Agent(name="PlanningAgent", model=model, instructions=[common, "Draft driver expressions only through DRAFT proposals."], tools=tool_functions, output_schema=AgentPlan)
    return Team(name="FPATeam", mode="coordinate", model=model, members=[interpreter, critic, planner], output_schema=AgentPlan, instructions=[common, "Coordinate the specialists and return an AgentPlan with a non-empty dsl field. Do not return a narrative without a DSL plan; execution and the final AgentFPAResponse are handled by the orchestrator."])
