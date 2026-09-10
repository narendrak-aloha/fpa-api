# Re-Forecast Copilot — FP&A Engineering Assignment

**Video:** _TODO — link goes here before submission (Phase 19)._

Everything below is written as if that link already works: architecture,
how to run it, the decisions and their trade-offs, and what two more weeks
would buy. The original brief is preserved verbatim in `ASSIGNMENT.html`;
this file is the submission document it asks for.

---

## 1. Run it from nothing

```bash
git clone <this repo> fpa_be && cd fpa_be
./scripts/bootstrap.sh
```

That one command: starts Postgres/ClickHouse/Temporal/Commitment
Service/API/Temporal-worker via `docker compose`, waits for each to report
healthy, seeds the cube (`seed_fpa.py --drop`, ~1.27M rows, ~12s), and runs
the Postgres migrations (`alembic upgrade head`) that create every
governance table, trigger, and role.

If you don't already have an `.env`, the script copies `.env.example` to
`.env` for you. Drop a real key into it if you want the Copilot's
English-question path live — the rest of the system (governance, DSL,
compiler, bridge, Temporal) works with it blank, and the test suite
documents exactly which tests are skipped without one (see §7).

Three providers are wired up. Set whichever key you have and
`src/fpa_be/agents/model.py` picks the provider for you:

| Env var | Provider | Default model |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic | `claude-sonnet-5` |
| `OPENAI_API_KEY` | OpenAI | `gpt-4o` |
| `GOOGLE_API_KEY` | Google | `gemini-2.0-flash` |

If several keys are set, the table's order is the precedence. To pin an
exact model — or to force a provider regardless of which keys are present —
set `FPA_COPILOT_MODEL_ID` (e.g. `gemini-1.5-pro`, `claude-opus-5`,
`gpt-4o-mini`); its prefix selects the provider. Only `model.py` knows any
of this: `build_team` takes a `Model` and stays provider-agnostic, so the
guardrails, citation post-hook, and scope injection are identical whichever
provider answers.

Then:

### Who you are: API keys

Every endpoint authenticates with an `x-api-key` header, and
`src/fpa_be/api/auth.py` is the one place a key becomes a principal — user,
governance role, and row scope. Nothing reads the actor or role from a
request body. Demo identities:

| Key | User | Role | Row scope |
|---|---|---|---|
| `alice-planner-key` | alice | planner | all 20 entities |
| `bob-controller-key` | bob | controller | all 20 entities |
| `carol-controller-key` | carol | controller | all 20 entities |
| `pl-planner-key` | pl-planner | planner | RTPL1 only |

Which role may make which transition lives in `state_transition.role`
(planner submits; a controller approves, rejects, locks, and resolves
recomputes and driver proposals). Nobody resolves their own request.

### Changing the plan: a driver shock

```bash
# a Locked version + a known driver -> PlanRecomputeWorkflow
curl -X POST localhost:8000/plan-versions/<id>/recompute \
  -H 'x-api-key: alice-planner-key' -H 'content-type: application/json' \
  -d '{"driver_shocks": [{"name": "utilisation", "value": 0.74}]}'
# -> {"workflow_id": "plan-recompute-<id>-r<revision>", ...}; watch it in the UI
#    or Temporal, then approve as a *different* controller:
curl -X POST localhost:8000/workflows/<workflow_id>/approve -H 'x-api-key: bob-controller-key'
```

### Turning the Commitment Service's failure up

```bash
# rate: 0.0-1.0; mode: "error" (500 before anything happens) or "timeout"
# (stalls past the caller's timeout, then applies the write anyway)
curl -X POST localhost:8001/_config/failure-rate -H 'content-type: application/json' \
  -d '{"rate": 1.0, "mode": "timeout", "timeout_seconds": 15}'
curl localhost:8001/commitments          # read the ledger to check it against the cube
```

The same settings exist as env vars (`COMMITMENT_FAILURE_RATE`,
`COMMITMENT_FAILURE_MODE`, `COMMITMENT_TIMEOUT_SECONDS`).

```bash
# backend test suite
uv run pytest -q

# frontend
cd ../fpa_fe && npm install && npm run serve   # http://localhost:8080
```

The API is on `http://localhost:8000` (`GET /health`), Temporal Web UI on
`http://localhost:8233`, ClickHouse HTTP on `http://localhost:8123`.

### Clean-machine acceptance (Phase 20's gate)

```bash
uv run pytest tests/acceptance/ -q   # the evaluator's own sequence, verbatim
uv run pytest tests/security/ -q    # adversarial/security tests
uv run pytest tests/workflows/test_replay.py -q   # determinism proof
```

---

## 2. Architecture — what talks to what

```
                              ┌─────────────┐
                planner  ───▶ │  Vue UI      │
                              │ (fpa_fe)     │
                              └──────┬───────┘
                                     │ HTTP (axios)
                                     ▼
                       ┌─────────────────────────┐
                       │   FastAPI  (main.py)     │
                       │  plan_version / variance │
                       │  workflow_progress /     │
                       │  copilot                 │
                       └──┬───────────┬───────┬───┘
                           │           │       │
              plan spine   │  cube     │       │ agent tier
              (governance) │  reads    │       │ (English Qs)
                           ▼           ▼       ▼
                    ┌──────────┐ ┌──────────┐ ┌───────────────┐
                    │ Postgres │ │ClickHouse│ │  Agno Team     │
                    │ (fpa)    │ │ (fpa_cube)│ │ (coordinate)   │
                    └──────────┘ └──────────┘ └───────┬───────┘
                                       ▲                │ tools:
                                       │                │ list_metrics/
                     ┌─────────────────┴──────┐         │ list_dimensions/
                     │  Temporal worker         │         │ run_finops_query/
                     │  PlanRecomputeWorkflow   │         │ propose_driver
                     │  + activities            │◀────────┘  (compiler-mediated,
                     └───────────┬─────────────┘             never raw SQL)
                                 │ HTTP
                                 ▼
                     ┌─────────────────────┐
                     │ Commitment Service    │
                     │ (services/commitment) │
                     └─────────────────────┘
```

Every *read* from ClickHouse — agent queries including `BRIDGE`, the
bridge's matched plan/actual set, and the API's drill-through — is SQL
emitted by the compiler (`src/fpa_be/compiler/compile.py`: `compile`,
`compile_bridge`, `compile_matched_rows`, `compile_drill_through`), with
the caller's scope injected and every literal bound. The only hand-written
ClickHouse statements are the workflow's own *writes* (`publish_to_cube`
and its rollback) and the vintage lookup. **No `run_sql` tool exists
anywhere in this codebase** (`tests/agents/test_tools.py` and
`tests/security/test_adversarial.py` both assert this directly). The API
never calls the Commitment Service; only the Temporal worker does.

### Repository map

```
src/fpa_be/
  registry/     frozen contract: 19 dimensions, measures + aggregation kind
  dsl/          FinOpsExpr grammar (Lark), AST, resolver, evaluator
  compiler/     DSL -> parameterized ClickHouse SQL, security-context injection
  cube/         read-only ClickHouse client + vintage resolution
  bridge/       variance decomposition (waterfall) + persistence
  workflows/    PlanRecomputeWorkflow + activities (Temporal)
  api/          FastAPI routers: plan_version, variance, workflow_progress, copilot
  agents/       Agno Team, tools, guardrails, drift detection
  masking/      PII classify/mask/disclosure-log chokepoint
  db.py         connection-role DSNs (app / controller / superuser)
migrations/     Alembic — governance tables, triggers, functions, roles
services/commitment/   standalone Commitment Service (its own FastAPI app)
tests/          one directory per phase's concern (see §6)
```

---

## 3. Read path vs. write path

**Read path** (a planner asks a question): `API key → SecurityContext →
Agno team (dependencies) → run_finops_query(DSL) → parse_query →
check_node → compile / compile_bridge (scope injected, literals bound,
cost-budgeted) → ClickHouse → masking gate → LLM`. `BRIDGE` queries
(`... COMPARE PLAN pv='PV-2026-0001' TO ACTUAL BRIDGE`) compile to the
matched plan/actual set and return one row per rollup node with every leg,
the residual and the tolerance, plus the vintage read. The masking gate is
`disclosure_tool_hook`, the innermost tool hook on every member: it
classifies by dimension, tokenizes personal columns and writes
`llm_disclosure_log` *before* the result reaches the model, and withholds
the result if the log can't be written. The leader's citation post-hook
checks each citation, and every number in the answer, against the tool
executions members actually ran, never against citations the model wrote.
`/copilot/query` returns that same execution trace (member, DSL, vintage),
which is what the UI shows beside the answer.

**Write path** (a driver shock re-forecasts the plan): the plan spine moves
a version Draft → In-Review → Approved → Locked under role-scoped,
revision-guarded transitions. `POST /plan-versions/{id}/recompute` with a
driver shock against a **Locked** version is the governance → Temporal
edge: it is audited, checks the driver exists, and starts
`PlanRecomputeWorkflow` (or, if one is running, delivers the shock as the
`shock_driver` update, whose validator folds it in or refuses it). The
workflow snapshots drivers, resolves the dirty set, fans out evaluation,
writes `plan_version_line` rows idempotently, parks for approval by a
controller who is not the requester, re-checks Locked, publishes to the
cube, commits to the Commitment Service (with the amount read from the
published lines), and bridges the version's plan against actuals. If the
commit fails or the run is cancelled after the publish, it rolls the cube
back **and** reverses the commitment under the revision's idempotency key.
That covers the case where the POST timed out but actually landed.
Locking itself starts nothing.

---

## 4. Service boundaries and who enforces what

| Boundary | Enforced by | Why not just the app |
|---|---|---|
| Who may make which transition | `state_transition.role` (data) + the authenticated principal (`api/auth.py`), checked in `api/governance.py` | Adding a state or a role is a row, not a code change; the actor can't be typed into a request body |
| Self-approval refusal | Postgres `CHECK` constraint (`ck_plan_version_no_self_approval`) | A future endpoint, a script, or `psql` itself must not be able to bypass it |
| Locked version row immutability | Postgres trigger (`fn_plan_version_locked_guard`): only Locked → Superseded; Superseded is frozen | "No field on a locked version can be written again" has to hold for `psql` too |
| Recompute approval by someone other than the requester | App, before the Temporal signal is sent (`api/workflow_progress.py`) + audit row | The workflow is not the place to discover the rule was broken; there is no DB row to constrain for a signal |
| Agent drafts, human approves | `plan_driver_proposal` + `ck_plan_driver_proposal_no_self_approval` + resolved-row guard trigger | `propose_driver` can only insert a Draft; only a controller's approval writes `plan_driver` |
| Covenant-breach approval block | App check (`approve_plan_version`) *and* column-level `REVOKE`/`GRANT` so only `fpa_controller` can write `covenant_breach` | Two layers: the app refuses to *act* on a breach; the DB refuses to let the app *role* even set the flag |
| Locked-version immutability | Postgres trigger (`fn_plan_version_line_guard`) rejecting `UPDATE`/`DELETE` on `plan_version_line` when the parent is `Locked` | Attacked directly with raw `psql` in `tests/governance/test_plan_version_line_lock.py` and again in the Phase 15 walkthrough — an app-only check doesn't survive that |
| Tamper-evident audit | Postgres trigger computing `hash = sha256(prev_hash \|\| payload)` on insert, append-only trigger blocking `UPDATE`/`DELETE`, `fn_verify_audit_chain()` verifier | The chain has to be independently verifiable without trusting the app that wrote it |
| DSL type/aggregation legality, unknown metric/dimension, cyclic driver DAG | App (`src/fpa_be/dsl/resolver.py`) | Not a governance fact about a specific row — a property of the query/formula language itself |
| Row-scope injection (a caller never sees another entity's rows) | App, but at the *compiler* layer (`compile()`), not per-endpoint | One chokepoint every tool and endpoint shares, instead of N places that could each get it wrong differently |
| PII masking + disclosure logging | App (`src/fpa_be/masking/gate.py`), attached as an Agno tool hook on every member, fail-closed | Classification is by dimension name, never by sniffing values — a disclosure-log write failure blocks the send entirely, verified in `tests/security/test_adversarial.py` |
| Idempotent commit / duplicate-safe recompute | Temporal (workflow history) + Commitment Service (`Idempotency-Key`) | Durability against worker crashes is Temporal's job; not-double-committing-to-a-third-party is the Commitment Service's job |

---

## 5. Temporal vs. Agno — two different kinds of "durable"

It's tempting to blur these because both involve retries and waiting, but
they solve unrelated problems:

- **Temporal** makes a *recompute* durable: if the worker dies mid-run, a
  new worker on the same task queue resumes exactly where history left
  off — no double-publish, no lost driver evaluation, no need to replay
  from scratch. This is proven mechanically by
  `tests/workflows/test_replay.py` (Temporal's `Replayer` against a
  committed event history) and exercised live in the Phase 15 walkthrough
  (kill a real worker, signal while nothing is polling, restart, watch it
  complete from the signal that was waiting).
- **Agno** makes a *conversation* structured and governed: a leader in
  `coordinate` mode decomposes an English question, delegates to members,
  and synthesizes a cited answer — but it holds no durable state of its
  own across a crash. If the process restarts mid-answer, the question is
  just asked again; nothing was "in flight" in the way a recompute is.

They meet only through the governance store: `propose_driver` (an Agno
tool, HITL-gated) records a Draft `plan_driver_proposal` and nothing else.
A controller who is not the proposer approves it (`/driver-proposals`),
which is what writes the live `plan_driver`. A shock against that driver on
a `Locked` version then starts the recompute, and it still needs its own
signed-off approval before anything publishes. See
`docs/APPROVAL_ARCHITECTURE.md` for the full three-way split (Agno HITL /
Postgres governance / Temporal signal) — they look similar but guard
against three different failure modes at three different scopes.

---

## 6. Design decisions and their trade-offs

**1. Bridge convention: sequential waterfall, not Shapley.** `PLAN → VOLUME
→ MIX(practice) → MIX(grade) → PRICE → OPERATING ACTUAL → FX → REPORTED
ACTUAL` (`src/fpa_be/bridge/decompose.py`), matching the assignment's own
diagram. *Trade-off:* the size of an individual leg depends on the order
legs are peeled off (a Shapley/symmetric allocation wouldn't), but it's
simple to defend, matches the given picture exactly, and produces a
residual within `tol = max(1.00, 0.01 * line_count)` at every rollup node
(`tests/bridge/test_bridge_identity.py`, and the acceptance walkthrough).

**2. Second shock mid-run: folded in before the dirty set is resolved,
rejected after.** A `shock_driver` update that arrives while drivers are
still being snapshotted is folded into the shock set
(`TestShockFoldedInBeforeDirtySetResolution`). Once resolution has begun,
the update validator refuses it (`"dirty set already resolved for this
revision"`, `TestSecondShockRejection`). The API routes a shock for an
in-flight recompute through this update rather than deciding itself.
*Trade-off:* a planner who shocks twice in quick succession has to wait for
the first revision to resolve and re-shock, rather than getting an
implicit "latest wins" merge — but a merge risks mixing two driver
snapshots in one revision, which the audit trail (one revision, one
snapshot) depends on not happening.

**3. Approval timeout: expires, doesn't escalate.** A bounded timer races
the `approve`/`reject` signal wait; if it fires first, the workflow
completes `Rejected(reason="timeout")` — nothing publishes, nothing
commits (`tests/workflows/test_plan_recompute.py::test_approval_timeout_
expires_without_publishing`). *Trade-off:* no automatic "raise it to the
next approver" chain — that would invent escalation semantics the
assignment never specifies (who gets notified, what "raising it" means).
Reusing the rejection path we already had to build is honest about what's
actually implemented versus what would need real product input.

**4. Agno Team in `coordinate` mode, not `route`.** The leader owns the
final response and decomposes the ask; members return structured results
the leader synthesizes and cites from. *Trade-off:* every answer pays for
a leader synthesis pass on top of the member call(s) — measurably more
tokens/latency than a single-agent baseline for the same question (logged
per-call; see `src/fpa_be/agents/team.py`'s docstring) — but it's what
makes the citation post-hook ("every number in the answer must appear in a
cited row") enforceable in one place. `route` was considered and rejected
because a member's raw output would reach the planner unchecked.

**5. DB-level enforcement for anything the assignment names as
governance-critical; app-level for everything else.** Self-approval,
locked-version immutability, tamper-evident audit, and the covenant/rate
column grants are Postgres triggers/constraints/`REVOKE`, not just FastAPI
checks (§4) — attacked directly with raw SQL in `tests/governance/` and
the Phase 15 walkthrough. DSL validation and state-transition legality
live in the app because the assignment doesn't ask for DB-level proof of
those, and a Lark grammar has no natural SQL analogue anyway. *Trade-off:*
two enforcement styles in one codebase is more to hold in your head than
"everything's in the app" — but a security boundary that only the
application remembers to check is not actually a boundary.

---

## 7. What's unfinished, simplified, or environment-limited

- **No live LLM in this dev environment.** All three provider keys
  (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY`) are blank
  here, so the English-question path (`POST /copilot/query`) is only
  exercised at its authentication boundary
  (`tests/api/test_copilot_api.py`) rather than end-to-end. Every other
  layer an English question would eventually hit — scope injection,
  masking, DSL errors, the injection guardrail against seed_fpa.py's real
  hostile customer rows — is proven directly against the compiler/tool
  surface instead (`tests/acceptance/`, `tests/security/`). Drop in a real
  key and the same team/tools/guardrails run live; nothing about the
  architecture changes.
- **Driver-to-grain mapping is representative, not exhaustive.** Phase 8's
  `write_plan_lines` writes one `plan_version_line` per dirty driver
  (`account = driver name`, `company = "ALL"`), not fanned out across the
  full 19-dimension cube grain a real recompute would ultimately need to
  hit. The assignment's own pseudocode names the activities
  (`snapshot_drivers`, `resolve_dirty_set`, `evaluate_partition`, ...) but
  not this mapping — getting the durability/idempotency/compensation/HITL
  contract right was Phase 8's actual job, and re-deriving Phase 6's full
  cube-matched-row machinery inside the workflow would have been scope
  creep on top of it. Because those representative lines have no matched
  actual keys, the workflow's `compute_variance` bridges the governed
  version's *cube plan* (its `plan_code`/scenario, revenue accounts, the
  plan year) and says so in the report's cut label, rather than a bridge
  of the recomputed lines themselves.
- **Agno HITL continuation over HTTP is not wired.** `propose_driver` still
  pauses for confirmation, and `/copilot/query` reports a paused run and its
  pending tool call, but there is no endpoint yet to confirm and resume it.
  That needs Agno session storage and a live model to verify. The
  governance half, a Draft proposal resolved by a second human, is built
  and tested.
- **Variance report listing is authenticated but not row-scoped.** Cube
  reads (including drill-through) are scoped per caller; the persisted
  report lines themselves are group-level aggregates any authenticated
  user can list.
- **Stored bridge amounts are `Numeric(18,2)`**, so a persisted residual is
  rounded to the cent. The UI shows it exactly as stored, and the live
  `BRIDGE` query returns the unrounded value.
- **Commitment Service storage is an in-process dict.** Deliberately —
  it's a small, adversarial counterparty the workflow has to be correct
  against (idempotency keys, a configurable failure rate), not a system of
  record. The actual system of record for "was this committed" is the
  calling workflow's own Temporal history.
- **Single ClickHouse node, single Postgres instance, no multi-tenancy.**
  Fine for the assignment's scale (~1.27M rows); a real deployment would
  need ClickHouse replication/sharding and connection pooling tuned well
  past `asyncpg`'s defaults.
- **API key auth is a hardcoded dict** (`_PRINCIPALS` in
  `src/fpa_be/api/auth.py`), not a real identity provider. It is the one
  place caller identity becomes a principal (user, role, `SecurityContext`),
  so swapping in OIDC/SSO only touches `authenticate`.
- **The Vue UI is intentionally minimal** — five things and nothing else
  (DSL shown, bridge waterfall with residual, drill-through, live
  Temporal progress, Approve/Reject/Lock), per the assignment's own half-day
  budget for frontend work. No routing, no state management library, no
  styling system beyond what's needed to read the numbers.
- **CI's Commitment Service step runs the app directly via `uvicorn`**,
  not the Docker image, because GitHub Actions' `services:` block can't
  build a local Dockerfile. Phase 20's clean-machine gate (`docker compose
  up`) is what actually proves the image builds and runs.

## 8. What two more weeks would buy

- **A live agent demo suite**: the same acceptance walkthrough, but with a
  real model answering the Poland Q2 / July-AS-OF questions in English and
  a genuine attempt (on camera, in CI) to talk the *live* team into a
  fourth-entity or customer-name-injection breach, not just the structural
  proof that the boundary would hold if it tried.
- **Full cube-grain recompute**: replace the representative
  one-line-per-driver write with the actual fan-out across
  `evaluate_partition`'s child workflows down to the real 19-dimension
  grain, so a recompute's `plan_version_line` output is the complete,
  addressable plan — not just proof that the orchestration contract holds.
- **A second, adversarial Commitment Service test double** that returns
  malformed/slow/partial responses (not just clean 500s), to harden the
  compensation saga against failure modes messier than "the whole call
  failed."
- **Replay fixtures for every workflow shape**, not just the happy-path
  publish (timeout-expiry, compensation-triggered, reject-without-
  publish), so Phase 17's determinism proof covers every branch a future
  code change could break, not only the one this submission happened to
  freeze.
- **Real identity/SSO** in place of the hardcoded API-key map in
  `api/auth.py`, and HTTP confirm/resume for paused Agno HITL runs.
- **ClickHouse replication** and a load test against the full ~1.27M-row
  cube under concurrent agent + UI traffic, to find the actual cost-budget
  ceiling (`MAX_ESTIMATED_ROWS`) empirically rather than by inspection.
