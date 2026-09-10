"""Commitment Service: the small, standalone, deliberately unreliable
downstream ledger the recompute workflow's `commit_to_treasury` activity
pushes an approved forecast to (Phase 8/10). The point of this service
isn't its own logic -- it's a fixed, adversarial counterparty the workflow
has to be correct against.

Contract (fixed by the assignment):
- POST /commitments takes an optional Idempotency-Key header. The same key
  twice returns the first response and creates nothing new. Without a key,
  every call creates a new commitment.
- DELETE /commitments/{id} compensates a commitment, and is itself allowed
  to fail (so the workflow's compensation path has something real to be
  correct against, not just a happy-path mock).
- A runtime-configurable failure rate (env var default, or the
  POST /_config/failure-rate endpoint) makes either endpoint return 500.
  Defaults to 0.0 so `docker compose up` isn't broken by default.

Storage is an in-process dict -- this is a small deliberately-unreliable
counterparty, not a system of record; the actual system of record for
"was this forecast committed" is the calling workflow's own history.
"""

import os
import random
import uuid
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Commitment Service")

_commitments: dict[str, dict] = {}
_idempotency_index: dict[str, str] = {}  # Idempotency-Key -> commitment id
_failure_rate: float = float(os.environ.get("COMMITMENT_FAILURE_RATE", "0.0"))


class CreateCommitment(BaseModel):
    plan_version_id: str
    scenario_id: str
    revision: int
    amount: float
    currency: str = "USD"


class FailureRateConfig(BaseModel):
    rate: float


def _maybe_fail() -> None:
    if random.random() < _failure_rate:
        raise HTTPException(status_code=500, detail="commitment service: simulated failure")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/_config/failure-rate")
def set_failure_rate(body: FailureRateConfig) -> dict:
    global _failure_rate
    if not 0.0 <= body.rate <= 1.0:
        raise HTTPException(status_code=400, detail="rate must be between 0.0 and 1.0")
    _failure_rate = body.rate
    return {"failure_rate": _failure_rate}


@app.get("/_config/failure-rate")
def get_failure_rate() -> dict:
    return {"failure_rate": _failure_rate}


@app.post("/commitments", status_code=201)
def create_commitment(
    body: CreateCommitment,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _maybe_fail()

    if idempotency_key is not None and idempotency_key in _idempotency_index:
        existing_id = _idempotency_index[idempotency_key]
        return _commitments[existing_id]

    commitment_id = str(uuid.uuid4())
    record = {
        "id": commitment_id,
        "plan_version_id": body.plan_version_id,
        "scenario_id": body.scenario_id,
        "revision": body.revision,
        "amount": body.amount,
        "currency": body.currency,
        "status": "committed",
    }
    _commitments[commitment_id] = record
    if idempotency_key is not None:
        _idempotency_index[idempotency_key] = commitment_id
    return record


@app.delete("/commitments/{commitment_id}", status_code=204)
def delete_commitment(commitment_id: str):
    _maybe_fail()
    if commitment_id not in _commitments:
        raise HTTPException(status_code=404, detail="commitment not found")
    _commitments[commitment_id]["status"] = "reversed"
    return None
