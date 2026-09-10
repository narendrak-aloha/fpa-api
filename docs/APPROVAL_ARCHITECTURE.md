# Approval Architecture

The system has three distinct approval mechanisms. They look similar
("someone has to say yes before something happens") but they enforce
different things, at different layers, against different failure modes.
Conflating them would be a mistake: each one is the only thing standing
between the system and a specific bad outcome.

## 1. Agno HITL (tool/agent confirmation)

**Where:** `propose_driver` and any other agent tool that would change a
driver value or plan input (Phase 11/12).

**What it guards against:** an LLM acting on a plan change without a human
in the loop for that specific action, in that specific conversation. This is
a per-tool-call confirmation gate, scoped to the agent tier only -- it knows
nothing about plan-version state machines or Temporal workflows.

**Failure mode it prevents:** the agent silently proposing (or worse,
directly writing) a driver change because a user's phrasing sounded like an
instruction rather than a question.

Even once confirmed, the tool only records a Draft `plan_driver_proposal`.
A controller who is not the proposer resolves it (`/driver-proposals`, with
`ck_plan_driver_proposal_no_self_approval` in Postgres), and approval is
what writes `plan_driver`, so the agent tier and the plan spine agree on who
approved what.

## 2. PostgreSQL governance approval (plan-version lifecycle)

**Where:** `plan_version.state` transitions (`Draft -> In-Review -> Approved
-> Locked`), enforced by `src/fpa_be/api/plan_version.py` and, more
importantly, by the database itself: `ck_plan_version_no_self_approval`,
the column-level GRANTs on covenant/rate fields, and
`trg_plan_version_line_guard` rejecting writes once a version is `Locked`.

**What it guards against:** a plan version being approved by the same
person who authored it, or a covenant-breach plan being approved by anyone,
or a `Locked` version's lines being edited after the fact -- durable
governance facts about *who* is allowed to move *this specific version*
through *this specific state machine*, independent of any workflow run.

**Failure mode it prevents:** self-dealing approval, and any code path
(API bug, direct `psql` access, a future workflow) mutating a version's
committed lines after it's supposed to be immutable. This is why it's
DB-enforced rather than only checked in FastAPI (Decision 5 in the plan) --
an app-only check can be bypassed by anything that isn't the app.

## 3. Temporal signal (durable workflow wait)

**Where:** `PlanRecomputeWorkflow.await_approval`
(`src/fpa_be/workflows/plan_recompute.py`) -- `workflow.wait_condition`
racing an `approve`/`reject` signal against a bounded timer.

**What it guards against:** a recompute publishing to the cube and
committing to treasury without a human approving *this specific recomputed
revision*, and does so durably -- the wait survives a worker crash and
restart, because it's Temporal history, not in-process state. This is a
run-scoped gate: it approves one revision of one workflow execution, not a
plan version's lifecycle (that's mechanism 2) and not an agent's tool call
(that's mechanism 1).

The signal is only sent by `POST /workflows/{id}/approve`, which requires a
controller, refuses the person who requested the recompute, refuses unless
the run is actually `awaiting_approval`, and writes an audit row against the
plan version. Segregation of duties still holds here, enforced before the
signal rather than discovered inside the workflow.

**Failure mode it prevents:** an unattended recompute publishing a revision
nobody looked at, and -- via Decision 3 (expire, don't escalate) -- a
recompute that nobody ever gets around to approving publishing anyway after
some indefinite wait. If the timer fires first, the workflow completes in
`Rejected(reason="timeout")`: nothing publishes, nothing commits.

## Why keep them separate

Each mechanism answers a different question:

| Mechanism        | Question it answers                                    | Scope                    |
|------------------|----------------------------------------------------------|---------------------------|
| Agno HITL         | Should the agent even suggest this change?               | One tool call             |
| Postgres governance | Is this plan version allowed to move to this state?   | One plan version, forever |
| Temporal signal   | Should this specific recomputed revision be published?    | One workflow execution    |

A recompute can only reach `await_approval` for a plan version that's
already `Locked` (mechanism 2 already satisfied, and re-checked inside the
workflow itself before publish -- Phase 8's own "only Locked versions
publish" requirement). An agent proposing the driver shock that triggered
the recompute in the first place goes through mechanism 1 first. Collapsing
these into one check would mean a single approval click satisfies
requirements it was never scoped to satisfy.
