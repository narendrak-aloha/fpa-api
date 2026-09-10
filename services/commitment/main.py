"""Commitment Service placeholder.

Standalone downstream ledger the Temporal write path commits to. This is a
Phase 0 skeleton (health check only) so `docker compose up` brings the whole
stack up end to end; POST /commitments, DELETE /commitments/{id}, idempotency
handling, and the configurable failure-rate are built out in Phase 10.
"""

from fastapi import FastAPI

app = FastAPI(title="Commitment Service")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
