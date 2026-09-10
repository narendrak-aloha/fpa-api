"""Governance endpoints for agent-proposed driver changes. The agent tier's
`propose_driver` only ever inserts a Draft `plan_driver_proposal`; a
controller who is not the proposer resolves it here, and approval is the one
place a proposal becomes a live `plan_driver` row. Self-approval is refused
here and by `ck_plan_driver_proposal_no_self_approval` in Postgres.
"""

import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, Request

from fpa_be.api.auth import CurrentPrincipal, Principal
from fpa_be.api.governance import assert_transition, audit
from fpa_be.dsl.errors import FinOpsExprError
from fpa_be.dsl.parser import parse_expr

router = APIRouter(prefix="/driver-proposals", tags=["driver-proposals"])


def _serialize(row: asyncpg.Record) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "formula": row["formula"],
        "is_rate_driver": row["is_rate_driver"],
        "effective_date": row["effective_date"].isoformat(),
        "proposed_by": row["proposed_by"],
        "state": row["state"],
        "resolved_by": row["resolved_by"],
    }


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.app_pool


@router.get("")
async def list_proposals(request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch("SELECT * FROM plan_driver_proposal ORDER BY created_at")
    return [_serialize(r) for r in rows]


async def _lock_draft(conn: asyncpg.Connection, proposal_id: uuid.UUID, to_state: str, principal: Principal):
    row = await conn.fetchrow("SELECT * FROM plan_driver_proposal WHERE id = $1 FOR UPDATE", proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="plan_driver_proposal not found")
    await assert_transition(conn, "plan_driver_proposal", row["state"], to_state, principal)
    if principal.user == row["proposed_by"]:
        raise HTTPException(status_code=403, detail="a proposal cannot be resolved by the person who proposed it")
    return row


@router.post("/{proposal_id}/approve")
async def approve_proposal(proposal_id: uuid.UUID, request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn, conn.transaction():
        proposal = await _lock_draft(conn, proposal_id, "Approved", principal)
        try:
            parse_expr(proposal["formula"])
        except FinOpsExprError as exc:
            raise HTTPException(status_code=422, detail=f"malformed driver formula: {exc}") from exc
        updated = await conn.fetchrow(
            "UPDATE plan_driver_proposal SET state = 'Approved', resolved_by = $2, resolved_at = now() "
            "WHERE id = $1 RETURNING *",
            proposal_id,
            principal.user,
        )
        # rate_value is absent on purpose: it is fpa_controller-only at the
        # grant level, and a proposal never carries one.
        await conn.execute(
            """
            INSERT INTO plan_driver (name, formula, is_rate_driver, effective_date, created_by)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (name) DO UPDATE SET
                formula = EXCLUDED.formula,
                is_rate_driver = EXCLUDED.is_rate_driver,
                effective_date = EXCLUDED.effective_date
            """,
            proposal["name"],
            proposal["formula"],
            proposal["is_rate_driver"],
            proposal["effective_date"],
            proposal["proposed_by"],
        )
        payload = _serialize(updated)
        await audit(conn, "plan_driver_proposal", proposal_id, "approve", principal, payload)
    return payload


@router.post("/{proposal_id}/reject")
async def reject_proposal(proposal_id: uuid.UUID, request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn, conn.transaction():
        await _lock_draft(conn, proposal_id, "Rejected", principal)
        updated = await conn.fetchrow(
            "UPDATE plan_driver_proposal SET state = 'Rejected', resolved_by = $2, resolved_at = now() "
            "WHERE id = $1 RETURNING *",
            proposal_id,
            principal.user,
        )
        payload = _serialize(updated)
        await audit(conn, "plan_driver_proposal", proposal_id, "reject", principal, payload)
    return payload
