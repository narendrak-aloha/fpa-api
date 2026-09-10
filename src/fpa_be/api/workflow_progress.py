"""Live progress for a running `PlanRecomputeWorkflow`, read via Temporal's
`progress` query -- this is what the Vue interface polls, so approval and
recompute state stay visible even if the worker restarts mid-run.
"""

from fastapi import APIRouter, HTTPException, Request
from temporalio.service import RPCError

from fpa_be.workflows import PlanRecomputeWorkflow

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _temporal_client(request: Request):
    return request.app.state.temporal_client


@router.get("/{workflow_id}/progress")
async def workflow_progress(workflow_id: str, request: Request):
    handle = _temporal_client(request).get_workflow_handle_for(PlanRecomputeWorkflow.run, workflow_id)
    try:
        return await handle.query(PlanRecomputeWorkflow.progress)
    except RPCError as exc:
        raise HTTPException(status_code=404, detail=f"workflow not found or not queryable: {exc}") from exc
