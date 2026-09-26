# Done

Checkpoints from the assignment's "Done" list, checked against the code and the running stack on **2026-09-25**.

How this was checked:
- Migrations, source and tests were read.
- Every test was run against the live stack: `pytest -m 'not integration'` **311 passed, 0 failed**; `pytest -m integration` **46 passed, 0 failed**.
- The durable-recompute, lock and disclosure checks were run live on the stack. The commands and the numbers they produced are below each item.

Legend: `[x]` done · `[~]` partial or not proven · `[ ]` missing.

**Score: 38 done, 0 partial, 0 missing, out of 38.**

## Environment

- [x] ClickHouse answers, Temporal's UI loads at :8233, Postgres accepts a connection. All are defined in `docker/docker-compose.yml`, with the app, the worker and the Commitment Service.
- [x] `seed_fpa.py` has run clean and the row counts match the README. Two independent seeds (the dev stack and a fresh-volume stack) agree exactly: `fact_gl_actual FINAL` 1,025,023, `fact_plan_line` 231,390, `dim_customer` 920, `dim_employee` 5,000, `dim_fx_actual` 216, `dim_fx_plan` 108, `dim_ledger_vintage` 2. The dimension counts match `data/README.md`. The 1,034,766 in that README is rows *generated*: a ReplacingMergeTree count "settles under" it as duplicate keys merge, so an unmerged `count()` varies with merge timing (1,030,412 and 1,031,253 on the two stacks).
- [x] The repo has its first commit, and `docker compose up` brings the whole stack up from nothing. `scripts/acceptance_stack.py` builds an isolated stack with fresh volumes on other ports. `up --build --wait` was healthy in 71 s: migrations ran to 014, both seeders ran, a direct query answered, and the Temporal UI returned 200. Torn down with `down -v` afterwards.
  - Note: ClickHouse is `:latest` in compose (commit `95d807e`) and resolved to 26.8. The pruning test now accepts both spellings of the min-max index (below).

## The plan spine

- [x] A plan version can be authored, reviewed, approved by a second user and locked, through the interface.
- [x] Self-approval is refused, by `ck_plan_approval_no_self_approval` and the application check. Live: the planner's own approval of the parked run was refused (1 refusal; the run stayed parked).
- [x] A covenant breach blocks approval, by `ck_plan_version_covenant_before_approval`.
- [x] **Editing a locked version is refused, including from psql.** *Fixed:* migration `014` adds `INSERT` to `plan_line_lock_guard` and checks the version a line moves *into* as well as the one it leaves. Live psql against LOCKED `PV-2026-0001`: `INSERT` of a new line gives `ERROR: plan version is locked; create a superseding version instead`, and so does `UPDATE plan_version`. An `INSERT` into a DRAFT version still succeeds.
- [x] A line without a derivation trace cannot be saved (`ck_plan_version_line_derivation_trace_*`).
- [x] Scenario branches exist as driver overrides (`scenario_set` + `scenario_driver_override`), with no duplicated plan rows.
- [x] Altering one historical audit row makes the chain verifier fail (`make audit-tamper`, then `make audit-verify`).

## FinOpsExpr

- [x] Both entry rules parse to an AST. There is no `eval` or `exec` in `src/`.
- [x] A query compiles to parameterised SQL that runs against the cube and returns correct numbers.
- [x] A bad formula, an unknown measure, an illegal aggregation (`SUM(utilisation)`) and a graph cycle each produce a specific error (`test_grammar.py`, `test_formula.py`, `test_compiler.py`).
- [x] Row scope is enforced in the compiler (`test_compiler.py`, `test_bridge_integration::test_scope_decides_what_the_bridge_may_run_over`).
- [x] **A partition-pruning test runs against `EXPLAIN indexes=1`.** *Fixed:* the test read one label. It now reads the `Indexes:` block, accepts `Min-Max` (26.x) or `MinMax` (24.x), and asserts that the last index's selected parts are fewer than the first index's total. Live: Min-Max 6/31 parts. Passing.
- [x] Grammar tests cover precedence, nesting, the time operators, the measure types and the failure cases.

## The variance bridge

- [x] The residual is inside tolerance at every level of the rollup (unit and integration).
- [x] Volume plus mix equals the total quantity variance.
- [x] Practice mix and grade-within-practice mix are separate, non-zero legs.
- [x] **Every leg is materially non-zero on the Poland Q2 cut.** `test_every_leg_is_exercised_on_the_poland_cut` passes on the freshly seeded cube. The 2026-09-22 failure came from a published re-forecast in the old local cube shifting the plan side.
- [x] A property test over generated plan/actual pairs holds the identity (25 seeds, every level, both conventions).
- [x] A variance report persists with cited lines and a named vintage, and only a human closes it.

## Durable recompute

All of these were run live on 2026-09-25 against `PV-2026-0001`.

- [x] A driver shock starts `PlanRecomputeWorkflow`, which can be watched in the Temporal UI.
- [x] **Killing the worker mid-run and restarting it completes the run without duplicated work.** The worker was killed during `RECOMPUTING` (0 of 55,782 rows done), dead for about 45 s, then restarted. The log shows `partition n resuming at offset 0` and the run parked at 55,782/55,782. Staged cube rows for revision 2: 55,782 raw and 55,782 `FINAL`. The successor has 55,782 draft lines, all distinct on the grain.
- [x] Two identical runs leave identical state. After approving run 1: 231,390 rows, sum 10,862,813,060.44, revision 2, content hash `2819696053892154865`, ledger `{rev 2 RESERVED: 6}`. The identical shock again: the same four values and the same ledger.
- [x] Cancel, the progress query, and a mid-run second shock (folded in while computing, refused once parked).
- [x] **A run parks on approval, survives a worker restart while parked, and resumes on the signal.** The worker was killed while `AWAITING_APPROVAL`. After the restart the run was still `AWAITING_APPROVAL 55782/55782`. The covenant pass (controller) and approval (CFO) then took it to `COMPLETED`, with the successor `LOCKED`, approved by `u-cfo`.
- [x] A rejection leaves nothing published or committed, and the approval timer expires as a rejection.
- [x] **The Commitment Service at 100% failure leaves the cube and ledger consistent.** The shock was `utilisation 0.74 → 0.72` (merged to `0.75 → 0.72` with the published one), run with `FAIL=1 scripts/reforecast_e2e.sh`. Afterwards the cube is unchanged (same content hash, still revision 2), the ledger holds nothing for revision 3, and publication 3 is `COMPENSATED`. *Also fixed:* each `_fail()` cleanup step now runs on its own, so a failed discard no longer skips closing the successor and mirroring `FAILED`. Covered by `test_a_failed_discard_does_not_skip_the_rest_of_the_cleanup`; the committed-history replay still passes.
- [x] A replay test runs in CI against committed histories.

## The agents

- [x] A natural-language question produces DSL, then SQL, then a cited, correct answer.
- [x] No agent can reach either database except through the compiler (the tools are `list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver`).
- [x] **Personal data is masked before egress, and the disclosure log is written before the send and never editable after.** *Fixed:* migration `014` adds `llm_disclosure_log_no_update` (UPDATE/DELETE) and `llm_disclosure_log_no_truncate`. Live psql: `UPDATE`, `DELETE` and `TRUNCATE` each give `ERROR: llm_disclosure_log is append-only`. The team-cost run below added one row per model send.
- [x] A classification failure blocks the call (`test_agent_security.py`).
- [x] A number that is not in the cited data cannot survive into the answer (arithmetic post-hook).
- [x] A proposed assumption pauses for a human and lands as a draft with an approval record (`agent_proposal`, migration 013).
- [x] Hostile text is treated as data, and a scope-widening attempt fails, including against a member invoked directly.
- [x] **The team's mode is defensible, with token and latency numbers.** `scripts/measure_team_cost.py` runs the same questions through the same orchestrator as the `coordinate` team and as a single agent (`build_agno_team(single=True)`, with the same guardrails, tools and instructions). It counts every model call's usage at the provider, including cache tokens. Results are in `docs/team_cost.json` and the README. On 3 questions: single agent answered 2/3, median 33.6 s, 2 model calls, 33,143 tokens; `coordinate` team answered 3/3, median 36.9 s, 4 calls, 75,244 tokens (about 2.3×, mostly cached prompt). The first measurement found the team had never delegated: Agno's `delegate_task_to_member` returns a stream, the boundary's tool hook refused it as unclassifiable, and the leader answered alone. *Fixed* in `AgentBoundary.tool` (delegation passes after the scope check; every other tool still fails closed on an unknown type; `test_a_delegation_stream_passes_but_any_other_generator_is_blocked`). After the fix, members produced the DSL in 2 of 3 team runs (`fpa-variance`, `fpa-query`) and no delegation was blocked.

## The interface

- [x] All five are visible in a browser: DSL/SQL beside the answer, the bridge waterfall with residual, drill-through with vintage, live workflow progress, and approve/reject/lock. The member trace is in the response, the audit log and the UI.
- [x] The approve action goes through the same API permission rules.

## The approver's evidence (the four follow-up changes)

Each is additive: no checkpoint above changed state because of them.

1. **Bridge beside Approve / Reject / Lock.** New `GET /api/v1/plan-versions/{code}/impact` (`src/fpa_project/reforecast_impact.py`). For a re-forecast successor it returns the driver moves, the planner's reasons, and a bridge from the frozen baseline to the staged revision. That is the same pairing `compute_variance` persists after publish, decomposed by the unchanged `dsl.bridge.decompose`. It is read-only, behind the same permission check as every plan read. The UI shows it (`ReforecastImpact.vue`) next to the decision buttons and in the parked-run section. Live on `PV-2026-0001-R2` (base): gap −1,842,231.93 = volume −2,118,778.84, mix −1,896.96, efficiency +278,444.23, price, FX and rate 0, residual −0.36 against a tolerance of 185.94, tying at every level.
2. **Plan list.** `GET /plan-versions` now also returns the code of the version each one re-forecasts. `PlanList.vue` lists every version with state, source, requester and covenant, sorts what needs the current user first ("Covenant review", "Awaiting decision", "Ready to lock", "Your draft"), and loads a plan on click.
3. **The planner's reason.** An optional `reason` on `POST /reforecast` is written to the audit log (`entity_type='reforecast'`, `action='REASON'`) only after the run accepts the shock. It is kept out of `RecomputeInput`, so the idempotency key and the recorded histories are untouched. The impact read attaches each reason to the revision that carries its shocks. The agent-proposal path was left as it is: a proposal is a driver *formula* draft, not a shock value, so starting a re-forecast from one would invent meaning it does not have.
4. **Driver select.** The driver field is a dropdown of active drivers from `GET /drivers`. It falls back to a text box when the token cannot read the registry.

## Known limits

- The waterfall scales from zero, so a 1.8M change on a 169M plan draws as thin legs. The numbers and the tree carry the detail. This is the existing bridge chart's behaviour.
- A successor whose commitment push was compensated stays `LOCKED` with its publication `COMPENSATED` (`PV-2026-0001-R3`). This is existing design: the version records the decision, and the publication records that it never reached the cube.
- Outside this list: the video is still not recorded (`README.md:3`).
