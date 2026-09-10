"""The caller's row scope. Resolved once per request (Phase 11/12 wire this
up from Agno's dependency-injected caller identity, never from a prompt)
and passed into compile() -- the compiler injects it into the WHERE
clause server-side, regardless of what the DSL asked for.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SecurityContext:
    # None means unrestricted -- reserved for internal/admin callers, never
    # for an agent-facing request. An agent-facing SecurityContext always
    # sets at least one of these to a concrete, non-None frozenset.
    allowed_companies: frozenset[str] | None = None
    allowed_geo_countries: frozenset[str] | None = None
