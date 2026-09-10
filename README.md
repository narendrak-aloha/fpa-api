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
`.env` for you. Drop a real `ANTHROPIC_API_KEY` (or any Agno-supported
provider's key) into it if you want the Copilot's English-question path
live — the rest of the system (governance, DSL, compiler, bridge, Temporal)
works with it blank, and the test suite documents exactly which tests are
skipped without one (see §7).

Then:

```bash
# backend test suite (271 tests as of this writing)
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

Every arrow into ClickHouse — from the API's variance endpoints, the
Temporal activities, and the agent tools — goes through the same compiler
(`src/fpa_be/compiler/compile.py`). There is no second path. **No `run_sql`
tool exists anywhere in this codebase** (`tests/agents/test_tools.py` and
`tests/security/test_adversarial.py` both assert this directly).

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

**Read path** (a planner asks a question, or the UI loads a report):
`FinOpsExpr string → parse_query → check_node (typecheck/aggregation
legality) → compile(ast, security_context) → parameterized SQL →
ClickHouse`. The caller's `SecurityContext` is injected into the `WHERE`
clause server-side at compile time — never trusted from the query text —
so the exact same code path a human's UI request takes is what an agent
tool takes too. Personal columns (`customer`, `resource_employee`) are
tokenized and disclosure-logged (`mask_and_disclose`) before any row
reaches an agent's context.

**Write path** (a driver shock triggers a re-forecast): API accepts the
plan-version lifecycle transitions (Draft → In-Review → Approved → Locked)
and, once Locked, a shock starts `PlanRecomputeWorkflow`. The workflow
snapshots drivers, resolves the dirty set (topological order over the
formula DAG), evaluates each dirty driver, writes `plan_version_line` rows,
waits for a human `approve`/`reject` signal (or times out), publishes to
ClickHouse, commits to the standalone Commitment Service, and computes the
resulting variance bridge — with a compensating rollback if the commit
fails after publish. This is the only path that ever writes to
`fact_plan_line` or `plan_version_line`; nothing else does, and the
database itself refuses writes to a `Locked` version's lines regardless of
who's asking (§4).

---

## 4. Service boundaries and who enforces what

| Boundary | Enforced by | Why not just the app |
|---|---|---|
| Self-approval refusal | Postgres `CHECK` constraint (`ck_plan_version_no_self_approval`) | A future endpoint, a script, or `psql` itself must not be able to bypass it |
| Covenant-breach approval block | App check (`approve_plan_version`) *and* column-level `REVOKE`/`GRANT` so only `fpa_controller` can write `covenant_breach` | Two layers: the app refuses to *act* on a breach; the DB refuses to let the app *role* even set the flag |
| Locked-version immutability | Postgres trigger (`fn_plan_version_line_guard`) rejecting `UPDATE`/`DELETE` on `plan_version_line` when the parent is `Locked` | Attacked directly with raw `psql` in `tests/governance/test_plan_version_line_lock.py` and again in the Phase 15 walkthrough — an app-only check doesn't survive that |
| Tamper-evident audit | Postgres trigger computing `hash = sha256(prev_hash \|\| payload)` on insert, append-only trigger blocking `UPDATE`/`DELETE`, `fn_verify_audit_chain()` verifier | The chain has to be independently verifiable without trusting the app that wrote it |
| DSL type/aggregation legality, unknown metric/dimension, cyclic driver DAG | App (`src/fpa_be/dsl/resolver.py`) | Not a governance fact about a specific row — a property of the query/formula language itself |
| Row-scope injection (a caller never sees another entity's rows) | App, but at the *compiler* layer (`compile()`), not per-endpoint | One chokepoint every tool and endpoint shares, instead of N places that could each get it wrong differently |
| PII masking + disclosure logging | App (`src/fpa_be/masking/gate.py`), fail-closed | Classification is by dimension name, never by sniffing values — a disclosure-log write failure blocks the send entirely, verified in `tests/security/test_adversarial.py` |
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

They meet at exactly one edge: `propose_driver` (an Agno tool, HITL-gated)
can produce the driver shock that *starts* a `PlanRecomputeWorkflow`, but a
proposal is not a plan change — it still needs a `Locked` plan version and
a separately-signed-off recompute before anything publishes. See
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

**2. Second shock mid-run: rejected, not merged.** Once
`resolve_dirty_set` has run for a revision, a further `shock_driver` update
is refused (`"dirty set already resolved for this revision"` —
`tests/workflows/test_plan_recompute.py::TestSecondShockRejection`).
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

- **No live LLM in this dev environment.** `.env`'s `ANTHROPIC_API_KEY` is
  blank here, so the English-question path (`POST /copilot/query`) is only
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
  creep on top of it.
- **Commitment Service storage is an in-process dict.** Deliberately —
  it's a small, adversarial counterparty the workflow has to be correct
  against (idempotency keys, a configurable failure rate), not a system of
  record. The actual system of record for "was this committed" is the
  calling workflow's own Temporal history.
- **Single ClickHouse node, single Postgres instance, no multi-tenancy.**
  Fine for the assignment's scale (~1.27M rows); a real deployment would
  need ClickHouse replication/sharding and connection pooling tuned well
  past `asyncpg`'s defaults.
- **API key auth is a hardcoded dict** (`_API_KEYS` in
  `src/fpa_be/api/copilot.py`), not a real identity provider. It's
  explicitly commented as the one place caller identity becomes a
  `SecurityContext` — swapping in OIDC/SSO only touches that one function.
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
- **Real identity/SSO** in front of `/copilot/query` instead of the
  hardcoded API-key map, and role-based scopes instead of one
  `SecurityContext` per key.
- **ClickHouse replication** and a load test against the full ~1.27M-row
  cube under concurrent agent + UI traffic, to find the actual cost-budget
  ceiling (`MAX_ESTIMATED_ROWS`) empirically rather than by inspection.
