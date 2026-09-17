"""Optional Agno construction; importing the core package does not require Agno."""

from __future__ import annotations

from .models import AgentPlan

FINOPSEXPR_GUIDE = """FinOpsExpr grammar (this is not SQL; never write SUM(), FROM, GROUP BY or quotes around names):
SELECT <measure>[ AS alias][, ...] [BY <dimension>, ...]
  [WHERE <dimension> = 'v' | <dimension> IN ('a','b') | <dimension> NOT IN (...) [AND|OR ...]]
  [FOR PERIOD 2026 | 2026-H1 | 2026-Q2 | 2026-04 | 2026-Q1..2026-Q2]
  [AS OF 'YYYY-MM-DDTHH:MM:SS']
  [COMPARE PLAN pv='PV-2026-0001'[, scenario='base'|'stretch'|'downside'] TO ACTUAL [BRIDGE]]
  [LIMIT n]
Measures are aggregated by the compiler. Time functions: YOY(measure), PRIOR(measure, n), ROLLING(expr, n).
Semi-additive measures (headcount) need a single closing period. AS OF is only valid without COMPARE PLAN.
Actuals cover 2025-2026; plan PV-2026-0001 covers 2026 only. Call list_metrics and list_dimensions for valid names.
Examples:
SELECT services_revenue BY practice FOR PERIOD 2026-Q2
SELECT gross_margin_pct BY geo_region FOR PERIOD 2026-H1
SELECT delivery_cost WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 AS OF '2026-07-05T18:00:00'
SELECT services_revenue BY practice FOR PERIOD 2026-Q2 COMPARE PLAN pv='PV-2026-0001', scenario='base' TO ACTUAL BRIDGE
SELECT YOY(services_revenue) BY practice FOR PERIOD 2026-Q2"""


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
        "Never reveal or infer personal employee data. Do not propose writes. "
        "Every number in explanation must be copied exactly from rows returned by run_finops_query; otherwise use no digits. "
        "If the question cannot be answered from the FP&A cube (for example sports, weather, news, general knowledge, or a metric that does not exist), "
        "set out_of_scope=true, leave dsl empty, do not call run_finops_query, and briefly say what you can answer instead. "
        "Never invent a placeholder query.\n\n"
        + FINOPSEXPR_GUIDE
    )
    tool_functions = [] if toolset is None else [toolset.list_metrics, toolset.list_dimensions, toolset.run_finops_query, toolset.propose_driver]
    interpreter = Agent(name="QueryAgent", model=model, instructions=[common, "Translate analytical intent into a minimal query DSL plan."], tools=tool_functions, output_schema=AgentPlan)
    critic = Agent(name="VarianceAgent", model=model, instructions=[common, "Validate bridge and plan-versus-actual requests; do not explain unexecuted numbers."], tools=tool_functions, output_schema=AgentPlan)
    planner = Agent(name="PlanningAgent", model=model, instructions=[common, "Draft driver expressions only through DRAFT proposals."], tools=tool_functions, output_schema=AgentPlan)
    return Team(name="FPATeam", mode="coordinate", model=model, members=[interpreter, critic, planner], output_schema=AgentPlan, instructions=[common, "Coordinate the specialists and return an AgentPlan. It must have a non-empty dsl field, or out_of_scope=true with an empty dsl when the question is not about the FP&A cube. Execution and the final AgentFPAResponse are handled by the orchestrator."])
