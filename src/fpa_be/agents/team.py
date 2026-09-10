"""The Re-Forecast Copilot's Agno Team: one leader in `coordinate` mode
(Decision 4) plus four members, one per required capability -- answer a
question, explain a gap, propose a driver change, detect data drift.

`coordinate`, not `route`, because `route` lets a member's raw output reach
the user unchecked. In `coordinate` mode the leader owns the final
response, decomposes the ask ("resolve scope -> run query -> explain gap"),
and synthesizes+cites from members' structured results -- which is what
makes the post-hook citation check below possible in one place instead of
duplicated per member.

Guardrails: `CubeDataInjectionGuardrail` is attached as a `pre_hook` on the
leader *and* every member -- a guardrail on the leader alone doesn't protect
a member invoked directly -- alongside Agno's own `PIIDetectionGuardrail`.
Every member also carries two tool hooks: the masking gate
(`disclosure_tool_hook`) innermost around each tool, and the injection
screen outside it. `tool_call_limit` bounds runaway tool loops.
`output_schema` on the leader gives the post-hooks a structured answer to
check -- against what the tools actually returned, never against the
model's own account of it.
"""

import json
import re

from agno.agent import Agent
from agno.guardrails import PIIDetectionGuardrail
from agno.models.base import Model
from agno.team import Team
from agno.team.mode import TeamMode
from pydantic import BaseModel, Field

from fpa_be.agents.drift import DEFAULT_DRIFT_THRESHOLD_PCT
from fpa_be.agents.guardrails import CubeDataInjectionGuardrail
from fpa_be.agents.tools import (
    list_dimensions,
    list_metrics,
    propose_driver,
    run_finops_query,
)
from fpa_be.masking.gate import disclosure_tool_hook

TOOL_CALL_LIMIT = 8
DRIFT_DETECTOR_MEMBER_ID = "drift-detector"

_pii_guardrail = PIIDetectionGuardrail()
_injection_guardrail = CubeDataInjectionGuardrail()
_shared_guardrails = [_pii_guardrail, _injection_guardrail]
# Agno nests tool hooks with the first one outermost, so the masking gate
# sits directly around the tool: nothing -- not even the injection screen --
# sees a result before it has been masked and disclosure-logged.
_shared_tool_hooks = [_injection_guardrail.screen_tool_result, disclosure_tool_hook]


class Citation(BaseModel):
    dsl: str = Field(description="the exact run_finops_query DSL string that produced this row")
    row: list = Field(description="the cited row, exactly as returned by run_finops_query")


class CopilotAnswer(BaseModel):
    answer: str = Field(description="the answer to the planner's question, in plain language")
    numbers_cited: list[str] = Field(
        default_factory=list, description="every numeric value that appears in `answer`, as strings"
    )
    citations: list[Citation] = Field(
        default_factory=list, description="the cube rows backing every number in `answer`"
    )
    drift_flag: bool = Field(
        default=False, description="True if the drift-detection member found vintage disagreement above threshold"
    )


class UncitedNumberError(ValueError):
    """Raised by the post-hook when the answer or a citation is not backed
    by a query a member actually ran."""


def _numbers_in(text: str) -> set[str]:
    # Negative lookbehind for a letter excludes labels like "Q2" or "FY2026"
    # from being treated as cited figures.
    return set(re.findall(r"(?<![A-Za-z])-?\d[\d,]*\.?\d*", text))


def executed_queries(run_output) -> list[dict]:
    """Every successful `run_finops_query` a member actually ran, as the
    tool handed it to the model (after masking): member id, DSL, vintage,
    columns and rows. The citation check and the API's trace are both built
    from this, never from what the model says it ran."""
    executed = []
    for member_response in getattr(run_output, "member_responses", None) or []:
        member_id = getattr(member_response, "agent_id", None)
        for execution in getattr(member_response, "tools", None) or []:
            if execution.tool_name != "run_finops_query" or not execution.result:
                continue
            try:
                payload = json.loads(execution.result)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or "rows" not in payload:
                continue
            executed.append(
                {
                    "member_id": member_id,
                    "dsl": payload.get("dsl"),
                    "vintage": payload.get("vintage"),
                    "columns": payload.get("columns"),
                    "rows": payload["rows"],
                }
            )
    return executed


def _row_key(row) -> tuple[str, ...]:
    return tuple(str(value) for value in row)


def citation_post_hook(run_output=None, **kwargs) -> None:
    """Every citation must name a query a member actually ran and a row that
    query actually returned, and every number in the answer must appear in
    those verified rows -- or the response is rejected."""
    content = getattr(run_output, "content", None)
    if not isinstance(content, CopilotAnswer):
        return

    rows_by_dsl: dict[str, set[tuple[str, ...]]] = {}
    for query in executed_queries(run_output):
        rows_by_dsl.setdefault(query["dsl"], set()).update(_row_key(r) for r in query["rows"])

    cited_numbers: set[str] = set()
    for citation in content.citations:
        returned = rows_by_dsl.get(citation.dsl)
        if returned is None:
            raise UncitedNumberError(f"citation names a query no member ran: {citation.dsl!r}")
        if _row_key(citation.row) not in returned:
            raise UncitedNumberError(f"cited row was not returned by {citation.dsl!r}: {citation.row}")
        for value in citation.row:
            cited_numbers.update(_numbers_in(str(value)))

    uncited = _numbers_in(content.answer) - cited_numbers
    if uncited:
        raise UncitedNumberError(f"answer cites numbers with no matching cube row: {sorted(uncited)}")


def drift_post_hook(run_output=None, **kwargs) -> None:
    """Recomputes drift directly from the drift-detector member's own
    `run_finops_query` tool results and forces `drift_flag` accordingly --
    independent of whatever the leader's synthesized prose says, so a
    suppressed or miscounted drift can never reach the planner as `False`.
    """
    content = getattr(run_output, "content", None)
    if not isinstance(content, CopilotAnswer):
        return

    totals = [
        sum(float(str(row[-1]).replace(",", "")) for row in query["rows"])
        for query in executed_queries(run_output)
        if query["member_id"] == DRIFT_DETECTOR_MEMBER_ID and query["rows"]
    ]
    if len(totals) < 2:
        return
    older, newer = totals[0], totals[-1]
    delta_pct = abs(newer - older) / abs(older) * 100 if older else (100.0 if newer else 0.0)
    if delta_pct > DEFAULT_DRIFT_THRESHOLD_PCT:
        content.drift_flag = True


def _member(name: str, role: str, instructions: list[str], model: Model, include_propose_driver: bool = False) -> Agent:
    tools = [list_metrics, list_dimensions, run_finops_query]
    if include_propose_driver:
        tools.append(propose_driver)
    return Agent(
        id=name,
        name=name,
        role=role,
        model=model,
        instructions=instructions,
        tools=tools,
        pre_hooks=list(_shared_guardrails),
        tool_hooks=list(_shared_tool_hooks),
        tool_call_limit=TOOL_CALL_LIMIT,
        markdown=False,
    )


def build_team(model: Model) -> Team:
    query_answerer = _member(
        "query-answerer",
        role="Answers a planner's English question by running FinOpsExpr queries against the cube.",
        instructions=[
            "Translate the question into one or more run_finops_query calls.",
            "Never guess a number; every figure you report must come from a tool result.",
        ],
        model=model,
    )

    gap_explainer = _member(
        "gap-explainer",
        role="Explains a plan-vs-actual gap by bridging it through run_finops_query.",
        instructions=[
            "Pull the bridge with run_finops_query: SELECT <one revenue or cost measure> "
            "[BY practice|grade|company|account] WHERE ... FOR PERIOD ... [AS OF ...] "
            "COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE.",
            "Every row carries its legs, residual and tol, plus the vintage it read. Name the largest leg(s) "
            "driving the gap and state the vintage; never call a leg 'residual' unless it truly is the "
            "unexplained remainder.",
        ],
        model=model,
    )

    driver_proposer = _member(
        "driver-proposer",
        role="Proposes a new driver value or formula change when a planner asks for a what-if.",
        instructions=[
            "Use propose_driver for the change itself; it always requires human confirmation and only ever "
            "records a Draft -- say so explicitly rather than implying the change is already live.",
            "A proposal is not a plan change: a controller other than the proposer must approve it, and it "
            "still needs a Locked plan version and a signed-off recompute before it affects anything published.",
        ],
        model=model,
        include_propose_driver=True,
    )

    drift_detector = _member(
        DRIFT_DETECTOR_MEMBER_ID,
        role="Reconciles the two most recent ledger vintages for a metric and reports any disagreement.",
        instructions=[
            "Run the same run_finops_query twice, once AS OF each of the two most recent vintages, and "
            "compare totals.",
            "Always report the delta, even if it looks small -- do not round a real disagreement away.",
        ],
        model=model,
    )

    leader = Team(
        members=[query_answerer, gap_explainer, driver_proposer, drift_detector],
        mode=TeamMode.coordinate,
        model=model,
        name="fpa-copilot",
        id="fpa-copilot",
        instructions=[
            "Decompose the planner's request: resolve scope, delegate to the member(s) that can answer it, "
            "then synthesize.",
            "Every number in your final answer must be backed by a citations entry whose `dsl` and `row` are "
            "copied exactly from a run_finops_query result a member received; anything else is rejected.",
            "If the drift-detector member reports drift_flag=true, set drift_flag=true in your own output "
            "and mention it -- never omit a reported drift regardless of how the rest of the answer reads.",
        ],
        pre_hooks=list(_shared_guardrails),
        post_hooks=[drift_post_hook, citation_post_hook],
        tool_call_limit=TOOL_CALL_LIMIT,
        output_schema=CopilotAnswer,
        markdown=False,
    )
    return leader
