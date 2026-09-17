# FPA DSL compiler

This project contains a small, dependency-free Python implementation of a
schema-aware FinOpsExpr parser and ClickHouse SQL compiler. It accepts the
assignment's query DSL, produces a typed AST, validates names against a
static schema snapshot derived from `seed_fpa.py`, and emits parameterised SQL.

## Layout

```text
fpa-project/
  data/schema_snapshot.json   Static contract derived from seed_fpa.py
  src/fpa_project/dsl/         Lexer, AST, parser, schema and compiler
  src/fpa_project/agent_team/  Safe NL-to-FinOpsExpr Agno boundary
  tests/                       Positive and negative parser/compiler tests
  pyproject.toml               Package metadata and pytest dependency
```

## Architecture and data flow

The project is a dependency-light library with a strict boundary between
planning, compilation, and execution:

```text
PlanningRequest + UserScope
          |
          v
agent_team.masking -> optional Agno team -> AgentPlan
                                      |
                                      v
                         parser -> typed AST
                                      |
                    PlanningRegistry + Schema
                                      |
                                      v
                     Compiler -> CompiledQuery(sql, params)
                                      |
                           FPATools + injected executor
                                      |
                                      v
                         masked QueryToolResult / response
```

`dsl` owns language semantics. `lexer.py` recognizes tokens, `parser.py`
builds immutable AST dataclasses, `schema.py` loads the checked-in contract,
and `compiler.py` emits parameterized ClickHouse SQL. Actual reads use
`FINAL`; `AS OF` resolves a ledger vintage; ratio measures are recomputed from
their numerator/denominator; and `BRIDGE` joins actuals to plan on
`company`, `period_month`, `account`, and `dim_signature_hash` before
calculating variance foundations. `bridge.py` contains the deterministic
price/volume/mix/FX reconciliation helper for matched rows.

`agent_team` owns the untrusted boundary. `models.py` rejects extra fields and
keeps responses typed. `masking.py` protects employee, customer, compensation,
and identifier fields recursively. `registry.py` applies the planning-safe
subset of schema rules. `tools.py` is the only execution adapter and requires
an authenticated `UserScope` plus an injected executor. `planner.py` is the
orchestrator: it validates candidates, applies scope through the tools, checks
numeric narrative claims, and caps model retries. `logging_utils.py` and the
hooks provide allow-listed operational logs and redacted external audit data.

No module in this package creates a ClickHouse connection, executes writes, or
mutates the schema. Applications own authentication, connection lifecycle,
and any human approval workflow for draft model changes.

### Source of truth and change boundaries

`seed_fpa.py` defines the fixture's physical tables and business story;
`data/schema_snapshot.json` is the compiler's checked-in semantic contract.
When a metric, dimension, scenario, or table grain changes, update the seed,
snapshot, and tests together. The compiler should not infer schema changes from
live database metadata because that would make query behavior change silently.

Runtime code uses only the Python standard library. Install the test dependency
with `python -m pip install -e ".[dev]"`; run tests with `python -m pytest`.
The compiler does not open a database connection. `clickhouse-connect` can be
added by an application that wants to execute the returned SQL and parameters.

The package therefore supports two deployment modes: an offline validation
worker that uses the parser/registry/compiler without ClickHouse, and a
connected application that supplies `clickhouse-connect` through
`FPATools(executor=...)`. The latter is the only mode that executes queries.

## Agent team

`fpa_project.agent_team` provides a strict, optional Agno team for turning
natural-language requests into FinOpsExpr DSL. Its output is Pydantic-typed and
validated against the planning registry; personal employee data is masked before
model context is prepared. The team cannot generate SQL or execute writes. See
`src/fpa_project/agent_team/README.md`.

When the optional team is used, API keys rotate round-robin per model
invocation. Store a comma- or newline-separated set in the environment, for
example `LLM_API_KEYS=key-one,key-two,key-three`. Provider-specific list
variables (`OPENAI_API_KEYS`, `ANTHROPIC_API_KEYS`, `GOOGLE_API_KEYS`, or
`GEMINI_API_KEYS`) and their singular equivalents are also supported. Existing
model configuration remains unchanged when none of these variables is set.

## Schema source

`data/schema_snapshot.json` records the tables, columns, dimensions, accounts,
scenarios and measures used by the compiler. The table and dimension metadata
comes from `seed_fpa.py`:

- `fact_gl_actual` is the actuals fact table and is read with `FINAL`.
- `fact_plan_line` is the plan fact table keyed by plan version and scenario.
- The 19 planning dimensions are the canonical `DIM_COLUMNS` tuple.
- `company`, `account`, and `period_month` remain separate fact axes.

The snapshot is intentionally checked in. A future schema change should update
the snapshot and its tests rather than silently changing compiler behaviour.

## Financial formulae

The seed stores `quantity`, `unit_price`, and `amount_functional`, with the
invariant `amount_functional = round(quantity * unit_price, 2)`. The semantic
measures use these standard FP&A definitions:

- `services_revenue`: sum of accounts 41000, 41010, 41020 and 41400.
- `delivery_cost`: sum of delivery COGS accounts 51000, 51050, 51100,
  51300, and 51500.
- `subcontractor_cost`: account 51100.
- `gross_margin`: `services_revenue - delivery_cost`.
- `gross_margin_pct`: `gross_margin / services_revenue`, guarded with
  `nullIf` to avoid division by zero.
- `utilisation`: delivery quantity divided by available delivery capacity;
  the compiler represents the denominator as `sum(quantity)` for delivery
  payroll/service rows because the seed does not contain a separate capacity
  column.
- `realisation`: realised revenue per delivery quantity, represented as
  `services_revenue / delivery quantity`.
- `headcount`: closing-period row count proxy over employee-bearing rows;
  it is semi-additive over time.
- `bookings`: services revenue booked in the selected period.
- `open_pipeline`: not physically present in the seed; it is rejected by the
  default snapshot unless an application extends the schema.

Ratio measures are never summed or averaged as independent values. They are
recomputed from their numerator and denominator at the requested grain.

## Safety contract

Unknown fields and measures fail before SQL generation. Values are emitted as
ClickHouse named parameters such as `{p0:String}`; user strings are never
concatenated into SQL. The returned `CompiledQuery` contains both SQL and the
parameter dictionary for the caller.

`SecurityContext` should be constructed from the authenticated caller rather
than from model output. It injects an allowed-company predicate and enforces an
estimated row budget. The module does not trust a scope value supplied inside
the DSL string.

Driver formulae can be parsed and validated with `parse_formula()` and
`validate_formula()`. Formula references resolve only to schema metrics or
registered driver names; `detect_cycles()` rejects dependency cycles while
allowing `PRIOR(...)` historical references.

The compiler also rejects negative row budgets, malformed caller scopes,
reverse or empty period ranges, and `AS OF` on plan queries (where it would be
silently ignored). Measure aliases are validated as parser-compatible
identifiers before they are placed in SQL. Formula functions have explicit
arity checks, so malformed expressions cannot pass authoring validation.

`PlanningRegistry.validate_driver()` and `validate_model()` are the authoring
boundary for Postgres-backed persistence: call them before inserting a driver
or planning model. They return structured `RegistryIssue` values, validate
references and function semantics, and reject dependency cycles. The current
repository does not include a Postgres adapter, so persistence integration is
intentionally left to the application layer.
