"""The one place authenticated identity enters the API: an API key resolves
to a Principal -- who is acting, which governance role they hold, and which
rows of the cube they may see. Every router depends on this; actor, role and
scope are never read from a request body.

A real deployment resolves the key through an identity provider (SSO/OIDC).
The demo keys below are documented in README §1.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from fpa_be.compiler.security import SecurityContext
from fpa_be.registry.reference import COMPANIES

_GROUP_SCOPE = SecurityContext(allowed_companies=frozenset(c.company for c in COMPANIES))


@dataclass(frozen=True)
class Principal:
    user: str
    role: str  # a role named in state_transition.role: "planner" | "controller"
    security_context: SecurityContext


_PRINCIPALS: dict[str, Principal] = {
    "pl-planner-key": Principal("pl-planner", "planner", SecurityContext(allowed_companies=frozenset({"RTPL1"}))),
    "alice-planner-key": Principal("alice", "planner", _GROUP_SCOPE),
    "bob-controller-key": Principal("bob", "controller", _GROUP_SCOPE),
    "carol-controller-key": Principal("carol", "controller", _GROUP_SCOPE),
}


def authenticate(x_api_key: str | None = Header(default=None)) -> Principal:
    if x_api_key is None or x_api_key not in _PRINCIPALS:
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    return _PRINCIPALS[x_api_key]


CurrentPrincipal = Annotated[Principal, Depends(authenticate)]


def require_role(*roles: str):
    def dependency(principal: CurrentPrincipal) -> Principal:
        if principal.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"role {principal.role!r} may not perform this action; requires {' or '.join(roles)}",
            )
        return principal

    return dependency


ControllerPrincipal = Annotated[Principal, Depends(require_role("controller"))]
