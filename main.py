import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client

from fpa_be.api import (
    copilot_router,
    driver_proposals_router,
    plan_version_router,
    variance_router,
    workflow_progress_router,
)
from fpa_be.db import app_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.app_pool = await app_pool()
    app.state.temporal_client = await Client.connect(
        os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    yield
    await app.state.app_pool.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FPA_FE_ORIGIN", "http://localhost:8080")],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(plan_version_router)
app.include_router(variance_router)
app.include_router(workflow_progress_router)
app.include_router(copilot_router)
app.include_router(driver_proposals_router)


@app.get("/health")
def health():
    return {"status": "ok"}
