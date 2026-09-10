"""The authenticated HTTP boundary between the agent tier and everything
else: the caller's identity (`fpa_be.api.auth`) becomes the team's row scope
via Agno dependencies and its `user_id` -- never accepted from the request
body, and never something the model itself can widen.

The response carries a trace of which member ran which DSL, built from the
tool executions themselves rather than from the model's own account.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from fpa_be.agents.model import default_model
from fpa_be.agents.team import build_team, executed_queries
from fpa_be.api.auth import CurrentPrincipal

router = APIRouter(prefix="/copilot", tags=["copilot"])

_team = None


def _get_team():
    global _team
    if _team is None:
        _team = build_team(default_model())
    return _team


class CopilotQuery(BaseModel):
    question: str


@router.post("/query")
async def query(body: CopilotQuery, principal: CurrentPrincipal):
    run_output = await _get_team().arun(
        body.question,
        user_id=principal.user,
        dependencies={"security_context": principal.security_context},
    )
    trace = [
        {"member_id": q["member_id"], "dsl": q["dsl"], "vintage": q["vintage"], "row_count": len(q["rows"])}
        for q in executed_queries(run_output)
    ]
    if run_output.is_paused:
        # A tool that requires confirmation (propose_driver) is waiting on a
        # human; nothing it would record has been written yet.
        pending = [
            {
                "member_id": r.member_agent_id,
                "tool": r.tool_execution.tool_name,
                "arguments": r.tool_execution.tool_args,
            }
            for r in run_output.active_requirements
            if r.tool_execution is not None
        ]
        return {"status": "paused", "answer": None, "trace": trace, "pending_confirmations": pending}
    return {"status": "answered", "answer": run_output.content, "trace": trace}
