from contextlib import asynccontextmanager

from fastapi import FastAPI

from fpa_be.api import plan_version_router
from fpa_be.db import app_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.app_pool = await app_pool()
    yield
    await app.state.app_pool.close()


app = FastAPI(lifespan=lifespan)
app.include_router(plan_version_router)


@app.get("/health")
def health():
    return {"status": "ok"}
