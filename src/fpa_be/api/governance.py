"""Governance-store helpers shared by every router that moves a governed
object: the role-scoped transition lookup (state_transition is data, so
adding a state or a role never means editing this code) and the
hash-chained audit write.
"""

import json

import asyncpg
from fastapi import HTTPException

from fpa_be.api.auth import Principal


async def assert_transition(
    conn: asyncpg.Connection, entity_type: str, from_state: str, to_state: str, principal: Principal
) -> None:
    rows = await conn.fetch(
        "SELECT role FROM state_transition WHERE entity_type = $1 AND from_state = $2 AND to_state = $3",
        entity_type,
        from_state,
        to_state,
    )
    if not rows:
        raise HTTPException(status_code=409, detail=f"illegal transition {from_state} -> {to_state}")
    if principal.role not in {r["role"] for r in rows}:
        raise HTTPException(
            status_code=403,
            detail=f"role {principal.role!r} may not move {entity_type} {from_state} -> {to_state}",
        )


async def audit(
    conn: asyncpg.Connection, entity_type: str, entity_id, action: str, principal: Principal, payload: dict
) -> None:
    await conn.execute(
        "INSERT INTO audit_event (entity_type, entity_id, action, actor, actor_role, payload) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        entity_type,
        str(entity_id),
        action,
        principal.user,
        principal.role,
        json.dumps(payload, default=str),
    )
