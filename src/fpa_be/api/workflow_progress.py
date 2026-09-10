"""Live progress for a running `PlanRecomputeWorkflow`, read via Temporal's
`progress` query -- this is what the Vue interface polls, so approval and
recompute state stay visible even if the worker restarts mid-run -- plus the
two signals that release its `awaiting_approval` wait.

The recompute's human gate is a Temporal signal, a different mechanism from
plan_version's Postgres state machine (`docs/APPROVAL_ARCHITECTURE.md`), but
the same segregation-of-duties rule applies and is enforced here, before
the signal is sent: only a controller may resolve a recompute, never the
person who requested it, and only while it is actually awaiting approval.
Every resolution is written to the audit chain against its plan version.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from temporalio.service import RPCError

from fpa_be.api.auth import ControllerPrincipal, CurrentPrincipal
from fpa_be.api.governance import audit
from fpa_be.workflows import PlanRecomputeWorkflow

router = APIRouter(prefix="/workflows", tags=["workflows"])


class RejectAction(BaseModel):
    reason: str = ""


def _handle(request: Request, workflow_id: str):
    return request.app.state.temporal_client.get_workflow_handle_for(PlanRecomputeWorkflow.run, workflow_id)


async def _progress(request: Request, workflow_id: str) -> dict:
    try:
        return await _handle(request, workflow_id).query(PlanRecomputeWorkflow.progress)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail=f"workflow not found or not queryable: {exc}") from exc


@router.get("/{workflow_id}/progress")
async def workflow_progress(workflow_id: str, request: Request, principal: CurrentPrincipal):
    return await _progress(request, workflow_id)


async def _awaiting_approval(request: Request, workflow_id: str) -> dict:
    progress = await _progress(request, workflow_id)
    if progress["phase"] != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"recompute is in phase {progress['phase']!r}; it can only be resolved while awaiting_approval",
        )
    return progress


async def _audit_resolution(request: Request, progress: dict, workflow_id: str, action: str, principal, extra: dict):
    async with request.app.state.app_pool.acquire() as conn:
        await audit(
            conn,
            "plan_version",
            progress["plan_version_id"],
            action,
            principal,
            {"workflow_id": workflow_id, "revision": progress["revision"], **extra},
        )


@router.post("/{workflow_id}/approve")
async def approve_recompute(
    workflow_id: str, request: Request, principal: ControllerPrincipal
):
    progress = await _awaiting_approval(request, workflow_id)
    if principal.user == progress["requested_by"]:
        raise HTTPException(status_code=403, detail="a recompute cannot be approved by the person who requested it")
    try:
        await _handle(request, workflow_id).signal(PlanRecomputeWorkflow.approve, principal.user)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail=f"workflow not found or not signalable: {exc}") from exc
    await _audit_resolution(request, progress, workflow_id, "recompute_approve", principal, {})
    return {"workflow_id": workflow_id, "signal": "approve", "actor": principal.user}


@router.post("/{workflow_id}/reject")
async def reject_recompute(
    workflow_id: str, body: RejectAction, request: Request, principal: ControllerPrincipal
):
    progress = await _awaiting_approval(request, workflow_id)
    try:
        await _handle(request, workflow_id).signal(PlanRecomputeWorkflow.reject, args=[principal.user, body.reason])
    except RPCError as exc:
        raise HTTPException(status_code=404, detail=f"workflow not found or not signalable: {exc}") from exc
    await _audit_resolution(request, progress, workflow_id, "recompute_reject", principal, {"reason": body.reason})
    return {"workflow_id": workflow_id, "signal": "reject", "actor": principal.user, "reason": body.reason}
