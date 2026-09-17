# FinOpsExpr Agent Team

`fpa_project.agent_team` translates natural-language planning requests into a
validated `AgentPlan` containing FinOpsExpr DSL. It is an agent boundary, not a
query executor: it never creates SQL, opens ClickHouse connections, executes
code, or commits model changes.

## Safety contract

- Pydantic models reject extra output fields and require typed structured output.
- `mask_for_llm()` recursively masks employee identifiers, national IDs,
  compensation, customer names, and related fields before model context is sent.
- `PlanningRegistry` validates measures, dimensions, scenarios, predicates, and
  time-function offsets against `data/schema_snapshot.json`.
- Invalid syntax or registry references return structured `ValidationIssue`
  objects and no plan.
- The Agno adapter is optional and exposes no database, filesystem, SQL, or code
  execution tools. Human approval is required outside this module for any
  future model change; `ModelChangeProposal` is always `DRAFT` and the module
  itself has no apply or write path.
- Terminal logging is enabled for major lifecycle events. Logs contain only
  run IDs, statuses, counts, validation codes, and exception types; raw requests,
  DSL, SQL, parameters, and sensitive row values are not logged.
- External request/response envelopes are written to
  `logs/fpa_external_audit.jsonl` by default. Set `FPA_EXTERNAL_AUDIT_LOG` to
  change the path. The audit file includes the DSL and compiled SQL shape for
  traceability, but masks sensitive fields and never stores API keys.

## Usage

```python
from fpa_project.agent_team import FinOpsPlanner, PlanningRequest

planner = FinOpsPlanner()
request = PlanningRequest(request="Show services revenue by country", context={"customer_name": "Acme"})
safe_request = planner.prepare(request)
response = planner.generate(request, {
    "dsl": "SELECT services_revenue BY geo_country",
    "explanation": "Revenue grouped by country",
})
```

Install the optional Agno integration with the project-specific dependency
extra when deploying the model-backed team. The core validation layer remains
usable in tests and offline workers without Agno.

## Manual end-to-end execution

Yes. Provide an authenticated `UserScope`, a connected `clickhouse-connect`
client, and an agent-produced `AgentPlan`:

```python
import clickhouse_connect
from fpa_project.agent_team import (
    FPAOrchestrator, FPATools, PlanningRequest, UserScope, clickhouse_executor,
)

client = clickhouse_connect.get_client(host="localhost", username="...", password="...")
tools = FPATools(
    UserScope(user_id="analyst-1", allowed_companies=frozenset({"C001"})),
    executor=clickhouse_executor(client),
)
response = FPAOrchestrator(tools).finalize(
    PlanningRequest(request="Revenue by country"),
    {"dsl": "SELECT services_revenue BY geo_country FOR PERIOD 2026-Q2"},
    narrative="",
)
print(response.model_dump_json(indent=2))
```

The flow is NL/request boundary → masked context → DSL parse and registry
validation → scoped parameterized SQL compilation → ClickHouse execution →
masked result rows. The module does not create the ClickHouse connection. A
real database test requires reachable ClickHouse credentials and the optional
`clickhouse` dependency; the repository tests use an injected fake executor.
