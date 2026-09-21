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
Semi-additive measures (headcount) need a single closing period. AS OF selects the sealed ledger vintage, including for plan comparisons.
Actuals cover 2025-2026; plan PV-2026-0001 covers 2026 only. Call list_metrics and list_dimensions for valid names.
Examples:
SELECT services_revenue BY practice FOR PERIOD 2026-Q2
SELECT gross_margin_pct BY geo_region FOR PERIOD 2026-H1
SELECT delivery_cost WHERE geo_country = 'PL' FOR PERIOD 2026-Q2 AS OF '2026-07-05T18:00:00'
SELECT services_revenue BY practice FOR PERIOD 2026-Q2 COMPARE PLAN pv='PV-2026-0001', scenario='base' TO ACTUAL BRIDGE
SELECT YOY(services_revenue) BY practice FOR PERIOD 2026-Q2"""


def build_agno_team(model=None, toolset=None, *, disclosure_writer=None):
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

    from .security import AgentBoundary, persist_disclosure

    if toolset is None or not toolset.scope.allowed_companies:
        raise ValueError("an authenticated scoped toolset is required")
    boundary = AgentBoundary(toolset, writer=disclosure_writer or persist_disclosure)
    if model is None:
        raise ValueError("an explicit model is required for the egress boundary")
    model = boundary.protect_model(model)
    controls = dict(pre_hooks=[boundary.pii, boundary.injection, boundary.pre],
                    post_hooks=[boundary.post], tool_hooks=[boundary.tool])
    common = (
        "You are a FinOpsExpr planner. Output only a typed AgentPlan. "
        "For queries the dsl field must contain FinOpsExpr, never SQL, Python, or raw queries. For driver proposals call propose_driver; after confirmation set proposed_driver=true and leave dsl empty. "
        "A nonempty dsl value must be plain text beginning with SELECT: no square brackets, JSON, Markdown fences, or commentary inside dsl. "
        "Use only metrics and dimensions from the supplied planning registry. "
        "Never reveal personal employee data. Driver changes must use propose_driver, which pauses for human confirmation; never activate a driver. "
        "Every number in explanation must be copied exactly from rows returned by run_finops_query; otherwise use no digits. "
        "If the question cannot be answered from the FP&A cube (for example sports, weather, news, general knowledge, or a metric that does not exist), "
        "set out_of_scope=true, leave dsl empty, do not call run_finops_query, and briefly say what you can answer instead. "
        "Never invent a placeholder query. "
        "Always write and run the dsl for what was asked, even if it names companies or countries the caller may not see: "
        "the compiler limits every result to the caller's entity scope, listed in the result's scope field, and you cannot change it. "
        "In explanation, state that anything outside that scope is not visible to the caller; "
        "never describe it as zero, empty or having no activity.\n\n"
        + FINOPSEXPR_GUIDE
    )
    from agno.tools.function import Function
    proposal_tool = Function.from_callable(toolset.propose_driver)
    proposal_tool.requires_confirmation = True
    tool_functions = [toolset.list_metrics, toolset.list_dimensions, toolset.run_finops_query, proposal_tool]
    interpreter = Agent(**controls, id="fpa-query", tool_call_limit=6, name="QueryAgent", model=model, instructions=[common, "Translate analytical intent into a minimal query DSL plan."], tools=tool_functions, output_schema=AgentPlan)
    critic = Agent(**controls, id="fpa-variance", tool_call_limit=6, name="VarianceAgent", model=model, instructions=[common, "Validate bridge and plan-versus-actual requests; do not explain unexecuted numbers."], tools=tool_functions, output_schema=AgentPlan)
    planner = Agent(**controls, id="fpa-planning", tool_call_limit=6, name="PlanningAgent", model=model, instructions=[common, "Draft driver expressions only through DRAFT proposals."], tools=tool_functions, output_schema=AgentPlan)
    return Team(**controls, tools=tool_functions, id="fpa-team", tool_call_limit=6, name="FPATeam", mode="coordinate", model=model, members=[interpreter, critic, planner], output_schema=AgentPlan, instructions=[common, "Coordinate the specialists and return an AgentPlan. It must have a non-empty dsl field, proposed_driver=true after a proposal, or out_of_scope=true with an empty dsl when the question is not about the FP&A cube. Execution and the final AgentFPAResponse are handled by the orchestrator."])
