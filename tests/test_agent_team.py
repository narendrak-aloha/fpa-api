from fpa_project.agent_team import APIKeyRotator, FPAOrchestrator, FPATools, FinOpsPlanner, PlanningRequest, UserScope, mask_for_llm
from fpa_project.agent_team.hooks import ArithmeticVerificationPostHook, MaskingGateHook
from fpa_project.agent_team.logging_utils import ExternalAuditLogger


def test_api_keys_rotate_round_robin(monkeypatch):
    monkeypatch.setenv("LLM_API_KEYS", "key-a, key-b\nkey-c")
    rotator = APIKeyRotator.from_env()
    assert [rotator.next_key() for _ in range(5)] == ["key-a", "key-b", "key-c", "key-a", "key-b"]


def test_team_api_key_is_rotated_before_each_model_call(monkeypatch):
    monkeypatch.setenv("LLM_API_KEYS", "key-a,key-b")

    class Model:
        api_key = None

    class Team:
        model = Model()
        members = []
        calls = []

        def run(self, prompt, **kwargs):
            self.calls.append(self.model.api_key)
            return type("Run", (), {"content": {"dsl": "NOT DSL"}})()

    team = Team()
    tools = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"})))
    FPAOrchestrator(tools).run_with_team(team, PlanningRequest(request="revenue"))
    assert team.calls == ["key-a", "key-b", "key-a", "key-b", "key-a"]


def test_sensitive_context_is_masked_recursively():
    masked = mask_for_llm({"resource_employee": "E-1", "rows": [{"customer_name": "Acme", "geo_country": "PL"}]})
    assert masked["resource_employee"].startswith("<masked:")
    assert masked["rows"][0]["customer_name"].startswith("<masked:")
    assert masked["rows"][0]["geo_country"] == "PL"


def test_sensitive_request_fragments_are_masked_before_model_input():
    prepared = FinOpsPlanner().prepare(PlanningRequest(request="filter resource_employee: Alice"))
    assert "Alice" not in prepared.request
    assert "<masked:" in prepared.request


def test_valid_plan_is_returned_without_sql():
    response = FinOpsPlanner().generate(
        PlanningRequest(request="revenue by country"),
        {"dsl": "SELECT services_revenue BY geo_country"},
    )
    assert response.status == "VALID"
    assert response.plan.dsl.startswith("SELECT")
    assert not hasattr(response.plan, "sql")


def test_unknown_metric_and_dimension_fail_closed():
    response = FinOpsPlanner().generate(
        PlanningRequest(request="bad query"),
        {"dsl": "SELECT missing_metric BY secret_dimension"},
    )
    assert response.status == "ERROR"
    assert {error.code for error in response.errors} >= {"UNKNOWN_METRIC", "UNKNOWN_DIMENSION"}


def test_metric_predicates_are_not_allowed_by_agent_grammar():
    response = FinOpsPlanner().generate(
        PlanningRequest(request="bad predicate"),
        {"dsl": "SELECT services_revenue WHERE services_revenue > 10"},
    )
    assert response.status == "ERROR"


def test_aggregate_function_is_rejected_as_invalid_dsl():
    response = FinOpsPlanner().generate(
        PlanningRequest(request="bad aggregation"),
        {"dsl": "SELECT SUM(utilisation)"},
    )
    assert response.status == "ERROR"
    assert response.errors[0].code == "INVALID_DSL"


def test_tools_expose_registry_without_sql():
    tools = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"})))
    metrics = {item["name"]: item for item in tools.list_metrics()}
    dimensions = {item["name"] for item in tools.list_dimensions()}
    assert metrics["utilisation"]["allowed_operations"] == ["RECOMPUTE"]
    assert "geo_country" in dimensions
    assert "sql" not in tools.list_metrics()[0]


def test_query_tool_injects_authenticated_scope_and_masks_rows():
    captured = {}

    def executor(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"company": "C001", "customer_name": "Acme", "value": 42}]

    tools = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"})), executor=executor)
    result = tools.run_finops_query("SELECT services_revenue BY company LIMIT 1")
    assert result.status == "SUCCESS"
    assert result.rows[0]["customer_name"].startswith("<masked:")
    assert "a.company IN" in captured["sql"]
    assert "C001" in captured["params"].values()
    assert "sql" not in result.model_dump()


def test_scope_and_no_executor_fail_closed():
    no_scope = FPATools(UserScope(user_id="u1", allowed_companies=frozenset()))
    assert no_scope.run_finops_query("SELECT services_revenue").status == "REJECTED_SCOPE"
    no_executor = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"}), max_estimated_rows=2_000_000))
    assert no_executor.run_finops_query("SELECT services_revenue").status == "EXECUTION_ERROR"


def test_driver_proposal_is_always_draft_and_rejects_ratio_aggregation():
    tools = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"})))
    valid = tools.propose_driver("available_hours * bill_rate", name="capacity_rate")
    invalid = tools.propose_driver("SUM(utilisation)", name="bad")
    assert valid.status == "DRAFT" and not valid.errors
    assert invalid.status == "DRAFT" and invalid.errors
    assert len(tools.draft_drivers) == 2


def test_registry_validates_driver_authoring_and_model_cycles():
    from fpa_project.agent_team.registry import PlanningRegistry

    registry = PlanningRegistry()
    assert registry.validate_driver("growth", "PRIOR(services_revenue, 12)") == []
    assert registry.validate_driver("growth", "unknown_metric + 1")[0].code == "INVALID_DRIVER"
    issues = registry.validate_model({"a": "b + 1", "b": "a * 2"})
    assert any(issue.code == "FORMULA_CYCLE" for issue in issues)


def test_hooks_audit_and_verify_narrative_numbers():
    log = []
    hook = MaskingGateHook(log)
    payload = hook.before_model({"customer_name": "Acme"}, user_id="u1")
    assert payload["customer_name"].startswith("<masked:")
    assert log[0]["sensitive_values_disclosed"] is False
    verifier = ArithmeticVerificationPostHook()
    assert verifier.verify("Revenue was 42", [{"revenue": 42}])[0]
    assert not verifier.verify("Revenue was 43", [{"revenue": 42}])[0]


def test_orchestrator_runs_nl_candidate_through_dsl_and_executor():
    tools = FPATools(
        UserScope(user_id="u1", allowed_companies=frozenset({"C001"})),
        executor=lambda sql, params: [{"revenue": 42}],
    )
    response = FPAOrchestrator(tools).finalize(
        PlanningRequest(request="revenue by company"),
        {"dsl": "SELECT services_revenue BY company"},
        narrative="Revenue was 42",
    )
    assert response.execution_status == "SUCCESS"
    assert response.generated_dsl.startswith("SELECT")
    assert response.cited_data_rows == [{"revenue": 42}]


def test_orchestrator_limits_candidate_retries_to_five():
    tools = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"})), executor=lambda sql, params: [])
    response = FPAOrchestrator(tools).finalize_with_retries(
        PlanningRequest(request="revenue"),
        [{"dsl": "SELECT bad"}] * 5,
    )
    assert response.execution_status == "VALIDATION_ERROR"


def test_team_nl_to_dsl_attempts_are_hard_capped_at_five():
    class AlwaysBadTeam:
        def __init__(self):
            self.calls = 0

        def run(self, prompt, **kwargs):
            self.calls += 1
            return type("Run", (), {"content": {"dsl": "NOT DSL"}})()

    team = AlwaysBadTeam()
    tools = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"})), executor=lambda sql, params: [])
    response = FPAOrchestrator(tools).run_with_team(team, PlanningRequest(request="revenue"))
    assert team.calls == 5
    assert response.execution_status == "VALIDATION_ERROR"


def test_orchestrator_can_consume_agno_like_team_output():
    class FakeTeam:
        def run(self, prompt, **kwargs):
            return type("Run", (), {"content": {"dsl": "SELECT services_revenue", "explanation": ""}})()

    tools = FPATools(UserScope(user_id="u1", allowed_companies=frozenset({"C001"}), max_estimated_rows=2_000_000), executor=lambda sql, params: [{"value": 1}])
    response = FPAOrchestrator(tools).run_with_team(FakeTeam(), PlanningRequest(request="revenue"))
    assert response.execution_status == "SUCCESS"


def test_major_pipeline_steps_are_logged_to_terminal(capsys):
    tools = FPATools(
        UserScope(user_id="u1", allowed_companies=frozenset({"C001"}), max_estimated_rows=2_000_000),
        executor=lambda sql, params: [{"value": 1}],
    )
    FPAOrchestrator(tools).finalize(
        PlanningRequest(request="revenue"),
        {"dsl": "SELECT services_revenue"},
    )
    terminal = capsys.readouterr().err
    assert "request_received" in terminal
    assert "scope_injected" in terminal
    assert "query_compiled" in terminal
    assert "clickhouse_execution_completed" in terminal
    assert "response_completed" in terminal
    assert "services_revenue" not in terminal


def test_external_requests_and_responses_are_audited_redacted(tmp_path):
    audit = ExternalAuditLogger(tmp_path / "audit.jsonl")
    tools = FPATools(
        UserScope(user_id="u1", allowed_companies=frozenset({"C001"}), max_estimated_rows=2_000_000),
        executor=lambda sql, params: [{"customer_name": "Acme", "value": 1}],
        audit_logger=audit,
    )
    tools.run_finops_query("SELECT services_revenue")
    records = [line for line in (tmp_path / "audit.jsonl").read_text().splitlines() if line]
    assert len(records) == 2
    joined = "\n".join(records)
    assert '"system": "clickhouse"' in joined
    assert "Acme" not in joined
    assert "services_revenue" in joined
