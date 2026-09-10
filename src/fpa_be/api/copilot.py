"""The authenticated HTTP boundary between the agent tier and everything
else (Phase 12): caller identity is resolved from the request's API key to
a `SecurityContext` here, once, and handed to the team via Agno
dependencies -- never accepted from the request body, and never something
the model itself can widen.
"""

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from fpa_be.agents.model import default_model
from fpa_be.agents.team import CopilotAnswer, build_team
from fpa_be.compiler.security import SecurityContext

router = APIRouter(prefix="/copilot", tags=["copilot"])

# API key -> caller's row scope. A real deployment resolves this from an
# identity provider (SSO/OIDC); this is the one place in the whole system
# that authenticated identity becomes a SecurityContext.
_API_KEYS: dict[str, SecurityContext] = {
    "pl-planner-key": SecurityContext(allowed_companies=frozenset({"RTPL1"})),
}

_team = None


def _get_team():
    global _team
    if _team is None:
        _team = build_team(default_model())
    return _team


def _authenticate(x_api_key: str | None) -> SecurityContext:
    if x_api_key is None or x_api_key not in _API_KEYS:
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    return _API_KEYS[x_api_key]


class CopilotQuery(BaseModel):
    question: str


@router.post("/query")
async def query(body: CopilotQuery, x_api_key: str | None = Header(default=None)) -> CopilotAnswer:
    security_context = _authenticate(x_api_key)
    run_output = await _get_team().arun(
        body.question,
        dependencies={"security_context": security_context},
    )
    return run_output.content
