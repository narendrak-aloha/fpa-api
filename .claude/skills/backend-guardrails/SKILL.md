---
name: backend-guardrails
description: Enforceable guardrails for the fpa_be backend (FinOpsExpr DSL, SQL compiler security boundary, Postgres governance spine, variance bridge, Temporal recompute, Agno agent tier, PII gate, Commitment Service). Use when reviewing backend code AND when adding or changing any backend feature, endpoint, tool, activity, workflow, migration, or test — build to these rules instead of inventing new ones. Trigger on any work under fpa_be/src, fpa_be/migrations, fpa_be/services, fpa_be/tests, main.py, or worker.py.
---

# Backend guardrails

Two modes, same rules:

- **Reviewing** — walk the sections that the diff touches, report each violation as `file:line` + which rule + what breaks. A rule with no violation is not worth mentioning.
- **Building** — these constrain the implementation. If a requested feature can't be built without breaking one, say so and propose the compliant shape before writing code.

A rule below is violated if the code *could* do the forbidden thing, not only if it currently does.

---

## 1. The compiler is the only path to SQL

- `Agent → SQL` is forbidden. The only path is `DSL → parse_query → check_node → compile(ast, security_context) → parameterized SQL → ClickHouse`.
- **No `run_sql` tool may exist anywhere**, under any name. Any new agent tool that accepts SQL, a table name, or a raw predicate is a violation. Tools are exactly four: `list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver` (`src/fpa_be/agents/tools.py`).
- Every literal is parameterized. String-concatenating any user, model, or request value into SQL is a violation even if it is "obviously safe".
- New read paths (endpoints, activities, scripts) go through `compile()`. A hand-written ClickHouse query outside `src/fpa_be/compiler/` or `src/fpa_be/cube/` needs an explicit reason in a comment.
- Cost budget (`MAX_ESTIMATED_ROWS`) is enforced at compile time; refuse over budget with `QueryTooExpensiveError`. Never silently narrow, sample, or fall back.

## 2. Row scope is server-side and non-negotiable

- `SecurityContext` is resolved **once**, from the authenticated caller, at the HTTP boundary (`src/fpa_be/api/copilot.py::_authenticate`). Never from the request body, never from DSL text, never from a prompt string.
- `compile()` injects the caller's scope into the `WHERE` clause regardless of what the DSL asked for. A widening `WHERE`, an `OR` chain, or a tautology must not widen the result set.
- A caller scoped to N entities can never see entity N+1. Any new query shape needs a test asserting this.
- A bare `SecurityContext()` means unrestricted. Agent-facing callers must never construct one.
- Tools raise `NoSecurityContextError` rather than defaulting to unrestricted when scope is missing. Fail closed.

## 3. Governance is enforced in Postgres, not only in FastAPI

App-layer checks are additive; these must hold against raw `psql` with a superuser connection:

- Locked `plan_version` → `UPDATE`/`DELETE` on `plan_version_line` rejected by `fn_plan_version_line_guard`.
- Self-approval rejected by `CHECK` constraint (`approved_by != requested_by`), on INSERT *and* UPDATE.
- `covenant_breach` and `plan_driver.rate_value` are writable only by `fpa_controller`, via column-level `GRANT` **after revoking the blanket table-level UPDATE from both roles** — a column `REVOKE` on top of a table-level grant is a no-op.
- `audit_event` is append-only (trigger blocks UPDATE/DELETE) and hash-chained (`hash = sha256(prev_hash || payload)`); `fn_verify_audit_chain()` must flag any tampered row.
- `plan_version_line.driver_derivation_trace` is `NOT NULL`.
- Covenant breach blocks approval regardless of actor or role.
- Optimistic concurrency: `UPDATE ... WHERE revision = $1`; a losing writer gets a specific stale-write error, never a silent overwrite.
- State transitions are table-driven (`state_transition`), not `if/elif` in Python. Illegal transition → 409.
- `variance_report`: `Open → Investigating → Closed`. An agent may advance to `Investigating`; **only a human may Close**. Materiality breach escalates toward `Investigating`, never `Closed`.
- Scenario is a driver override, not a duplicated plan.

Any new governance rule: decide DB vs app deliberately. If the assignment names it as a guarantee, it belongs in a migration and needs a raw-SQL attack test under `tests/governance/`.

## 4. DSL and registry typing

- No `eval`, `exec`, or `compile` in the DSL path. Lark grammar + `Transformer` only (`src/fpa_be/dsl/`).
- Measures are typed: `additive` sums freely; `semi_additive` sums across dimensions but takes the closing period across time (`argMax`, never a naive time sum); `ratio` is **recomputed from stored numerator/denominator at the requested grain**, never summed or averaged.
- `SUM(utilisation)` / `AVG(gross_margin_pct)` → `IllegalAggregationError`. Adding a measure means declaring its kind.
- `utilisation` and `realisation` are distinct multipliers. Never conflate, never collapse into one factor.
- Unknown metric → `UnknownMetricError`; unknown dimension → `UnknownDimensionError`; malformed input → `FinOpsExprSyntaxError`; cyclic `calc_order_dag` → `CyclicDriverError`. Specific types, never a bare `ValueError`, never a silent pass.
- Errors surfaced through a tool are structured (`{"error": ...}`) and must not leak a traceback, a `site-packages` path, or a `src/fpa_be` path.
- `company`, `account`, `period_month` are separate axes — not part of the 19-column `dim_signature_hash` payload. The hash algorithm must stay byte-identical to `seed_fpa.py`.

## 5. Vintage is part of the answer

- `AS OF <ts>` resolves through `dim_ledger_vintage` to the vintage whose `closed_at <= ts`. No `AS OF` resolves to the documented default (latest) — documented, not implicit.
- **Every query answer and every bridge report carries the vintage it read**, all the way to the API response.
- `fact_gl_actual` is a `ReplacingMergeTree`: read with `FINAL` or explicit `argMax`. A naive `SELECT *` double-counts pre-merge duplicates and is a bug.
- Period predicates must be shaped so ClickHouse prunes partitions (`toYYYYMM(period_month)`).
- `dim_signature_hash` is `FixedString(16)`: raw `bytes` over the wire, hex-encoded when stored in Postgres `jsonb`. Decode back to bytes before any ClickHouse comparison.

## 6. Variance bridge

- Convention is **sequential**: `PLAN → VOLUME → MIX(practice) → MIX(grade-within-practice) → PRICE → OPERATING ACTUAL → FX → REPORTED ACTUAL`. Changing leg order changes the decomposition — don't reorder without updating the documented rationale.
- `residual = group_gap - sum(legs)`, and `abs(residual) < tol` where `tol = max(1.00, 0.01 * line_count)` — asserted at **every rollup node** (root → country → practice → grade), not just the top.
- `volume + mix == total_quantity_variance` at every level.
- Operational legs use the **plan** FX rate throughout (constant currency); only the FX leg swaps in the actual rate — enforced by construction, not just tested.
- **Never round the residual away** and never hide it. A materially-zero leg on the Poland Q2 cut is treated as a bug, not a pass.
- Matched set joins on `(company, period_month, account, dim_signature_hash)`, only over keys present on both sides.
- Persisted reports cite the exact cube rows (`cited_rows`) and the vintage used, so drill-through works.

## 7. Temporal

- Workflow code is **pure orchestration**. Every I/O call, clock read, and random value lives in an activity. No `datetime.now()`, `uuid4()`, `random`, or network call in the workflow body.
- Idempotency keys derive from `(plan_version_id, revision, activity_name)` — never generated per call.
- Only `Locked` plan versions publish, re-checked **inside** the workflow immediately before publish, not assumed from the caller.
- Second shock after the dirty set is resolved for the current revision → **reject** via the update validator (one revision = one driver snapshot, which the audit trail depends on).
- Approval timeout → **expire** to `Rejected(reason="timeout")`. Nothing publishes, nothing commits. No escalation chain.
- Compensation: if `commit_to_treasury` fails after `publish_to_cube` succeeded, roll the cube publish back so the cube and the commitment ledger never disagree.
- Cancellation aborts cleanly — no half-published revision.
- A progress query reports phase + fraction of the dirty set complete while running.
- **Determinism:** any edit to `PlanRecomputeWorkflow`'s body (reordering awaits, adding/removing an activity call, changing a signal or timer) must keep `tests/workflows/test_replay.py` green against the committed history fixture. If the history shape changed deliberately, regenerate via `scripts/generate_replay_fixture.py` and say so in the commit — never regenerate to make a failing replay go away.
- Child workflow IDs are derived deterministically from the parent workflow ID.

## 8. Agno agent tier

- `Team` in **`coordinate`** mode with a leader owning the final response. Not `route` (a member's raw output would reach the user unchecked), never `broadcast`.
- Guardrails (`pre_hooks`) attach to the leader **and every member** — a guardrail on the leader alone does not protect a member invoked directly.
- `tool_hooks` screen tool *results*: a `dim_customer` value that reads like an instruction is quarantined in place (labeled inert data), not dropped and not raised on.
- Post-hook: every number in the final answer must appear in the cited cube rows, or the response is rejected. Checked against the structured `output_schema`, not by re-parsing prose.
- Drift detection is **structural** — recomputed from the member's own tool results in a post-hook, so no prompt or model output can suppress the flag.
- Scope reaches agents through Agno `dependencies` at run time. **Prompt text is not a control** — `"You are only allowed PL"` in an instruction string is a violation.
- Stable member IDs as module constants, never regenerated per run.
- `output_schema`, `tool_call_limit`, bounded retry, and tracing stay set on leader and members.
- `propose_driver` keeps `requires_confirmation=True` (HITL). A proposal is not a plan change.

## 9. PII / masking gate

Single chokepoint, in this exact order, for every path that returns cube data to a model:

`resolve scope → fetch through the compiler → classify → mask/tokenize → write llm_disclosure_log → send`

- Classification is **by dimension name** (`PERSONAL_COLUMNS`), never by sniffing the value's text. A lookalike column name is not personal; a declared one always is regardless of what the value looks like.
- The disclosure log records classes, methods, scope, and a **payload hash — never the payload**.
- **Fail closed:** a failure at any step (including the log write) blocks the send entirely. No partial send, no "log it later".
- Masking is consistent per raw value so the model can still group/join masked rows.

## 10. Commitment Service

- `POST /commitments` with `Idempotency-Key`: same key twice returns the first result and creates nothing new; no key creates a new commitment every call.
- `DELETE /commitments/{id}` is compensation and is itself allowed to fail — the workflow must be correct against that.
- Failure rate stays runtime-configurable (0.0 default) so the compensation saga can be tested against 500s.
- Storage is deliberately in-process. The system of record for "was this committed" is the calling workflow's Temporal history — don't promote this service to a source of truth.

## 11. Tests

- New governance rule → a raw-SQL attack test in `tests/governance/`, not only an API test.
- New query shape → a scope test proving another entity's rows can't leak.
- New bridge behaviour → identity assertions at multiple rollup nodes, plus the `hypothesis` property test, not just one hardcoded cut.
- New workflow branch → a time-skipping behavioural test; if it changes history shape, the replay fixture too.
- `tests/api/conftest.py` has an **autouse fixture that truncates `audit_event, plan_version_line, plan_driver, plan_version`** before every test in that directory. Never seed demo/dev data and then run anything under `tests/api/` expecting it to survive.
- There is no usable `ANTHROPIC_API_KEY` in this environment. A test needing a live model call is skipped with a documented reason and the same guarantee is proven at the layer that actually enforces it (compiler, gate, guardrail) — never faked, never silently dropped.
- Don't weaken an assertion to make a test pass. A failure in `tests/acceptance/`, `tests/security/`, or `tests/workflows/test_replay.py` means the owning layer is wrong.

## 12. Delivery

- `seed_fpa.py` is **frozen**. Never modify it.
- Clean-machine rule: anything requiring a step not written in the README counts as not working. No undocumented command, no manually created row, no hand-edited seed, no manually-set secret.
- Service start order matters: `api` and `temporal-worker` connect as the `fpa_app` role that the governance migration creates — they must not start before migrations run.
- README stays accurate for: run-from-nothing, architecture, read path vs write path, service boundaries, Postgres-vs-app enforcement, Temporal-vs-Agno boundary, design decisions and trade-offs, what's unfinished/simplified, and what two more weeks would buy.
