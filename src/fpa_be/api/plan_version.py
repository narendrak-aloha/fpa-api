"""FastAPI endpoints for the plan_version lifecycle.

Legal state transitions are looked up from the state_transition table
(data, not a hardcoded switch per Decision 5). The self-approval and
covenant-breach refusals here are *in addition* to the DB-level
guarantees in migrations/versions/41ef8949dff6_governance_enforcement.py
(the ck_plan_version_no_self_approval CHECK, the column-level GRANTs) --
this is defense in depth, not the only line of defense.
"""

import json
import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/plan-versions", tags=["plan-versions"])


class CreatePlanVersion(BaseModel):
    plan_code: str
    scenario_id: str = "base"
    requested_by: str


class ActorAction(BaseModel):
    actor: str
    actor_role: str = "planner"


def _serialize(row: asyncpg.Record) -> dict:
    return {
        "id": str(row["id"]),
        "plan_code": row["plan_code"],
        "scenario_id": row["scenario_id"],
        "state": row["state"],
        "covenant_breach": row["covenant_breach"],
        "requested_by": row["requested_by"],
        "approved_by": row["approved_by"],
        "revision": row["revision"],
    }


async def _audit(conn: asyncpg.Connection, entity_id, action: str, actor: str, actor_role: str, payload: dict):
    await conn.execute(
        "INSERT INTO audit_event (entity_type, entity_id, action, actor, actor_role, payload) "
        "VALUES ('plan_version', $1, $2, $3, $4, $5)",
        str(entity_id), action, actor, actor_role, json.dumps(payload),
    )


async def _get_plan(conn: asyncpg.Connection, plan_id: uuid.UUID) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM plan_version WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="plan_version not found")
    return row


async def _assert_legal_transition(conn: asyncpg.Connection, from_state: str, to_state: str) -> None:
    row = await conn.fetchrow(
        "SELECT 1 FROM state_transition WHERE entity_type = 'plan_version' "
        "AND from_state = $1 AND to_state = $2",
        from_state, to_state,
    )
    if row is None:
        raise HTTPException(status_code=409, detail=f"illegal transition {from_state} -> {to_state}")


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.app_pool


@router.post("", status_code=201)
async def create_plan_version(body: CreatePlanVersion, request: Request):
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO plan_version (plan_code, scenario_id, requested_by) VALUES ($1, $2, $3) RETURNING *",
            body.plan_code, body.scenario_id, body.requested_by,
        )
        await _audit(conn, row["id"], "create", body.requested_by, "planner", _serialize(row))
        return _serialize(row)


@router.get("/{plan_id}")
async def get_plan_version(plan_id: uuid.UUID, request: Request):
    async with _pool(request).acquire() as conn:
        return _serialize(await _get_plan(conn, plan_id))


@router.post("/{plan_id}/submit")
async def submit_plan_version(plan_id: uuid.UUID, body: ActorAction, request: Request):
    async with _pool(request).acquire() as conn:
        row = await _get_plan(conn, plan_id)
        await _assert_legal_transition(conn, row["state"], "In-Review")
        updated = await conn.fetchrow(
            "UPDATE plan_version SET state = 'In-Review' WHERE id = $1 RETURNING *", plan_id
        )
        await _audit(conn, plan_id, "submit", body.actor, body.actor_role, _serialize(updated))
        return _serialize(updated)


@router.post("/{plan_id}/reject")
async def reject_plan_version(plan_id: uuid.UUID, body: ActorAction, request: Request):
    async with _pool(request).acquire() as conn:
        row = await _get_plan(conn, plan_id)
        await _assert_legal_transition(conn, row["state"], "Draft")
        updated = await conn.fetchrow(
            "UPDATE plan_version SET state = 'Draft' WHERE id = $1 RETURNING *", plan_id
        )
        await _audit(conn, plan_id, "reject", body.actor, body.actor_role, _serialize(updated))
        return _serialize(updated)


@router.post("/{plan_id}/approve")
async def approve_plan_version(plan_id: uuid.UUID, body: ActorAction, request: Request):
    async with _pool(request).acquire() as conn:
        row = await _get_plan(conn, plan_id)
        await _assert_legal_transition(conn, row["state"], "Approved")
        if body.actor == row["requested_by"]:
            raise HTTPException(status_code=403, detail="a plan version cannot be self-approved")
        if row["covenant_breach"]:
            raise HTTPException(
                status_code=403, detail="plan version is in covenant breach and cannot be approved"
            )
        updated = await conn.fetchrow(
            "UPDATE plan_version SET state = 'Approved', approved_by = $2 WHERE id = $1 RETURNING *",
            plan_id, body.actor,
        )
        await _audit(conn, plan_id, "approve", body.actor, body.actor_role, _serialize(updated))
        return _serialize(updated)


@router.post("/{plan_id}/lock")
async def lock_plan_version(plan_id: uuid.UUID, body: ActorAction, request: Request):
    async with _pool(request).acquire() as conn:
        row = await _get_plan(conn, plan_id)
        await _assert_legal_transition(conn, row["state"], "Locked")
        updated = await conn.fetchrow(
            "UPDATE plan_version SET state = 'Locked' WHERE id = $1 RETURNING *", plan_id
        )
        await _audit(conn, plan_id, "lock", body.actor, body.actor_role, _serialize(updated))
        return _serialize(updated)
