"""FastAPI endpoints for the plan_version lifecycle, and the governed entry
point from the plan spine into a recompute.

Who is acting comes from the authenticated principal (`fpa_be.api.auth`),
never the request body; which role may make which transition comes from
the state_transition table. The self-approval and covenant-breach refusals
here are *in addition* to the DB-level guarantees in the governance
migrations (the ck_plan_version_no_self_approval CHECK, the column-level
GRANTs, the Locked-version triggers) -- defense in depth, not the only line
of defense.
"""

import os
import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import WorkflowAlreadyStartedError

from fpa_be.api.auth import CurrentPrincipal, Principal
from fpa_be.api.governance import assert_transition, audit
from fpa_be.workflows import DriverShock, PlanRecomputeInput, PlanRecomputeWorkflow

router = APIRouter(prefix="/plan-versions", tags=["plan-versions"])


class CreatePlanVersion(BaseModel):
    plan_code: str
    scenario_id: str = "base"


class ShockIn(BaseModel):
    name: str
    value: float


class RecomputeRequest(BaseModel):
    driver_shocks: list[ShockIn] = Field(min_length=1)


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
        "recompute_workflow_id": recompute_workflow_id(row["id"], row["revision"]) if row["state"] == "Locked" else None,
    }


async def _get_plan(conn: asyncpg.Connection, plan_id: uuid.UUID) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM plan_version WHERE id = $1", plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="plan_version not found")
    return row


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.app_pool


def recompute_workflow_id(plan_version_id, revision: int) -> str:
    """Derived, not stored: one recompute per (plan version, revision), so
    the id is recoverable from the plan row alone -- a reloaded UI or a
    retried request addresses the same run instead of starting a second one.
    """
    return f"plan-recompute-{plan_version_id}-r{revision}"


async def _move(
    conn: asyncpg.Connection, row: asyncpg.Record, to_state: str, principal: Principal, action: str, **columns
) -> dict:
    """Moves one version to `to_state` if nobody else moved it first: the
    UPDATE is gated on the revision this request read, so of two concurrent
    writers one wins and the other is told it lost."""
    assignments = ", ".join(["state = $3", *(f"{col} = ${i}" for i, col in enumerate(columns, start=4))])
    updated = await conn.fetchrow(
        f"UPDATE plan_version SET {assignments} WHERE id = $1 AND revision = $2 RETURNING *",
        row["id"],
        row["revision"],
        to_state,
        *columns.values(),
    )
    if updated is None:
        raise HTTPException(
            status_code=409, detail="plan_version was changed by someone else since it was read; reload and retry"
        )
    payload = _serialize(updated)
    await audit(conn, "plan_version", row["id"], action, principal, payload)
    return payload


@router.post("", status_code=201)
async def create_plan_version(body: CreatePlanVersion, request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO plan_version (plan_code, scenario_id, requested_by) VALUES ($1, $2, $3) RETURNING *",
            body.plan_code,
            body.scenario_id,
            principal.user,
        )
        payload = _serialize(row)
        await audit(conn, "plan_version", row["id"], "create", principal, payload)
        return payload


@router.get("/{plan_id}")
async def get_plan_version(plan_id: uuid.UUID, request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn:
        return _serialize(await _get_plan(conn, plan_id))


@router.post("/{plan_id}/submit")
async def submit_plan_version(plan_id: uuid.UUID, request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn, conn.transaction():
        row = await _get_plan(conn, plan_id)
        await assert_transition(conn, "plan_version", row["state"], "In-Review", principal)
        return await _move(conn, row, "In-Review", principal, "submit")


@router.post("/{plan_id}/reject")
async def reject_plan_version(plan_id: uuid.UUID, request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn, conn.transaction():
        row = await _get_plan(conn, plan_id)
        await assert_transition(conn, "plan_version", row["state"], "Draft", principal)
        return await _move(conn, row, "Draft", principal, "reject")


@router.post("/{plan_id}/approve")
async def approve_plan_version(plan_id: uuid.UUID, request: Request, principal: CurrentPrincipal):
    async with _pool(request).acquire() as conn, conn.transaction():
        row = await _get_plan(conn, plan_id)
        await assert_transition(conn, "plan_version", row["state"], "Approved", principal)
        if principal.user == row["requested_by"]:
            raise HTTPException(status_code=403, detail="a plan version cannot be self-approved")
        if row["covenant_breach"]:
            raise HTTPException(
                status_code=403, detail="plan version is in covenant breach and cannot be approved"
            )
        return await _move(conn, row, "Approved", principal, "approve", approved_by=principal.user)


@router.post("/{plan_id}/lock")
async def lock_plan_version(plan_id: uuid.UUID, request: Request, principal: CurrentPrincipal):
    """Locks the version. Locking changes no number, so it starts nothing:
    a recompute is started by a driver shock (`POST .../recompute`)."""
    async with _pool(request).acquire() as conn, conn.transaction():
        row = await _get_plan(conn, plan_id)
        await assert_transition(conn, "plan_version", row["state"], "Locked", principal)
        return await _move(conn, row, "Locked", principal, "lock")


@router.post("/{plan_id}/recompute", status_code=202)
async def request_recompute(
    plan_id: uuid.UUID, body: RecomputeRequest, request: Request, principal: CurrentPrincipal
):
    """Governance -> Temporal: a driver shock against a Locked version
    starts its PlanRecomputeWorkflow. If that run is already in flight, the
    shock goes to the workflow's `shock_driver` update, whose validator
    folds it in or refuses it -- the API does not second-guess that.

    The workflow re-checks Locked itself before publishing; this check is
    so a caller hears "no" now instead of from a finished run."""
    shocks = [DriverShock(name=s.name, value=s.value) for s in body.driver_shocks]
    async with _pool(request).acquire() as conn:
        plan = await _get_plan(conn, plan_id)
        if plan["state"] != "Locked":
            raise HTTPException(
                status_code=409, detail=f"only a Locked plan version can be recomputed; this one is {plan['state']}"
            )
        known = {
            r["name"]
            for r in await conn.fetch("SELECT name FROM plan_driver WHERE name = ANY($1::text[])", [s.name for s in shocks])
        }
        unknown = sorted({s.name for s in shocks} - known)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown driver(s): {unknown}")

        workflow_id = recompute_workflow_id(plan["id"], plan["revision"])
        temporal = request.app.state.temporal_client
        try:
            await temporal.start_workflow(
                PlanRecomputeWorkflow.run,
                PlanRecomputeInput(
                    plan_version_id=str(plan["id"]),
                    scenario_id=plan["scenario_id"],
                    revision=plan["revision"],
                    requested_by=principal.user,
                    driver_shocks=shocks,
                ),
                id=workflow_id,
                task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "fpa-recompute"),
            )
            delivery = "started"
        except WorkflowAlreadyStartedError:
            handle = temporal.get_workflow_handle_for(PlanRecomputeWorkflow.run, workflow_id)
            for shock in shocks:
                try:
                    await handle.execute_update(PlanRecomputeWorkflow.shock_driver, shock)
                except WorkflowUpdateFailedError as exc:
                    reason = getattr(exc.cause, "message", str(exc.cause))
                    raise HTTPException(
                        status_code=409, detail=f"recompute {workflow_id} is already running and refused this shock: {reason}"
                    ) from exc
            delivery = "folded_into_running_recompute"

        await audit(
            conn,
            "plan_version",
            plan_id,
            "recompute_requested",
            principal,
            {"workflow_id": workflow_id, "delivery": delivery, "driver_shocks": [vars(s) for s in shocks]},
        )
    return {"workflow_id": workflow_id, "delivery": delivery}
