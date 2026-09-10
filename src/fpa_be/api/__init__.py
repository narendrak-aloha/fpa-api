from fpa_be.api.copilot import router as copilot_router
from fpa_be.api.driver_proposals import router as driver_proposals_router
from fpa_be.api.plan_version import router as plan_version_router
from fpa_be.api.variance import router as variance_router
from fpa_be.api.workflow_progress import router as workflow_progress_router

__all__ = [
    "copilot_router",
    "driver_proposals_router",
    "plan_version_router",
    "variance_router",
    "workflow_progress_router",
]
