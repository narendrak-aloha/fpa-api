"""Commitment Service: the small, standalone, deliberately unreliable
downstream ledger the recompute workflow's commit/compensate activities
push an approved forecast to. It shares no code and no database with
fpa_be, and nothing but the Temporal worker calls it. The point of this
service isn't its own logic -- it's a fixed, adversarial counterparty the
workflow has to be correct against.

Contract (fixed by the assignment):
- POST /commitments takes an optional Idempotency-Key header. The same key
  twice returns the first response and creates nothing new. Without a key,
  every call creates a new commitment.
- DELETE /commitments/{id} compensates a commitment, and is itself allowed
  to fail.
- Failure injection, settable at runtime (POST /_config/failure-rate) or by
  env var, applies to every business endpoint. `rate` (default 0.0) is the
  chance a call fails; `mode` is how:
    "error"   -> 500 before anything is done;
    "timeout" -> the call stalls for `timeout_seconds` (longer than the
                 caller waits) and is *then* applied -- the write lands but
                 the caller never hears about it, the case an idempotency
                 key exists for.

GET /commitments lists the ledger (optionally for one plan version) so a
reviewer can confirm it agrees with the cube.

Handlers are async on one event loop, so an idempotency lookup and the
write that follows it cannot interleave with another request.

Storage is an in-process dict -- this is a small deliberately-unreliable
counterparty, not a system of record; the actual system of record for
"was this forecast committed" is the calling workflow's own history.
"""

import asyncio
import os
import random
import uuid
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

FailureMode = Literal["error", "timeout"]

app = FastAPI(title="Commitment Service")

_commitments: dict[str, dict] = {}
_idempotency_index: dict[str, str] = {}  # Idempotency-Key -> commitment id
_failure_rate: float = float(os.environ.get("COMMITMENT_FAILURE_RATE", "0.0"))
_failure_mode: str = os.environ.get("COMMITMENT_FAILURE_MODE", "error")
_timeout_seconds: float = float(os.environ.get("COMMITMENT_TIMEOUT_SECONDS", "15.0"))

if _failure_mode not in ("error", "timeout"):
    raise ValueError(f"COMMITMENT_FAILURE_MODE must be 'error' or 'timeout', got {_failure_mode!r}")


class CreateCommitment(BaseModel):
    plan_version_id: str
    scenario_id: str
    revision: int
    amount: float
    currency: str = "USD"


class FailureConfig(BaseModel):
    rate: float
    mode: FailureMode | None = None
    timeout_seconds: float | None = None


def _config() -> dict:
    return {"failure_rate": _failure_rate, "failure_mode": _failure_mode, "timeout_seconds": _timeout_seconds}


async def _inject_failure() -> None:
    if random.random() >= _failure_rate:
        return
    if _failure_mode == "timeout":
        await asyncio.sleep(_timeout_seconds)
        return
    raise HTTPException(status_code=500, detail="commitment service: simulated failure")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/_config/failure-rate")
def set_failure_config(body: FailureConfig) -> dict:
    global _failure_rate, _failure_mode, _timeout_seconds
    if not 0.0 <= body.rate <= 1.0:
        raise HTTPException(status_code=400, detail="rate must be between 0.0 and 1.0")
    if body.timeout_seconds is not None and body.timeout_seconds <= 0:
        raise HTTPException(status_code=400, detail="timeout_seconds must be positive")
    _failure_rate = body.rate
    if body.mode is not None:
        _failure_mode = body.mode
    if body.timeout_seconds is not None:
        _timeout_seconds = body.timeout_seconds
    return _config()


@app.get("/_config/failure-rate")
def get_failure_config() -> dict:
    return _config()


@app.get("/commitments")
async def list_commitments(plan_version_id: str | None = None) -> list[dict]:
    await _inject_failure()
    return [c for c in _commitments.values() if plan_version_id is None or c["plan_version_id"] == plan_version_id]


@app.post("/commitments", status_code=201)
async def create_commitment(
    body: CreateCommitment,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _inject_failure()

    if idempotency_key is not None and idempotency_key in _idempotency_index:
        return _commitments[_idempotency_index[idempotency_key]]

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
async def delete_commitment(commitment_id: str):
    await _inject_failure()
    if commitment_id not in _commitments:
        raise HTTPException(status_code=404, detail="commitment not found")
    _commitments[commitment_id]["status"] = "reversed"
    return None
