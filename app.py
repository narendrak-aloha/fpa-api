"""FastAPI service: natural-language FP&A question -> Agno team -> DSL -> SQL -> ClickHouse -> cited answer.

Run:  uvicorn app:app --reload --port 8000   then open http://localhost:8000
LLM behind the Agno team (chosen per request):
      claude-code -> Claude Agent SDK using the local `claude` login (subscription, optional FPA_CLAUDE_CODE_MODEL)
      claude-api  -> ANTHROPIC_API_KEY (+ FPA_CLAUDE_MODEL);  gemini -> GOOGLE_API_KEY (+ FPA_MODEL_ID)
Env:  CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from fpa_project.agent_team import (  # noqa: E402
    AgentFPAResponse, FPAOrchestrator, FPATools, PlanningRequest, UserScope,
    build_agno_team, clickhouse_executor,
)
from fpa_project.dsl import DSLValidationError, ParseError, SecurityContext, compile_query  # noqa: E402

app = FastAPI(title="FPA Query API", version="1.0.0")

MANIFEST = ROOT / "data" / "out" / "cube_manifest.json"
ALL_COMPANIES = frozenset(c["company"] for c in json.loads(MANIFEST.read_text())["companies"]) if MANIFEST.exists() else frozenset()

PROVIDER_KEYS = {
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEYS", "GEMINI_API_KEYS", "LLM_API_KEYS"),
    "claude-api": ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS"),
}

_client = None


def get_client():
    global _client
    if _client is None:
        import clickhouse_connect
        _client = clickhouse_connect.get_client(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            username=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "fpa"),
        )
    return _client


def provider_configured(provider: str) -> bool:
    if provider == "claude-code":
        return shutil.which("claude") is not None
    return any(os.getenv(k) for k in PROVIDER_KEYS[provider])


def build_model(provider: str):
    if provider == "claude-code":
        from fpa_project.agent_team.claude_code_model import ClaudeCodeModel
        return ClaudeCodeModel()
    if provider == "claude-api":
        from agno.models.anthropic import Claude
        return Claude(id=os.getenv("FPA_CLAUDE_MODEL", "claude-sonnet-5"))
    from agno.models.google import Gemini
    return Gemini(id=os.getenv("FPA_MODEL_ID", "gemini-2.5-flash"))


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8_000)
    companies: list[str] | None = None
    max_rows: int = Field(default=1_000_000, ge=1)
    provider: Literal["claude-code", "claude-api", "gemini"] = "claude-code"


class QueryResponse(BaseModel):
    agent_response: AgentFPAResponse
    provider: str
    mode: Literal["agno_team", "direct_dsl", "not_run"]
    sql: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    duration_ms: int = 0


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


@app.post("/api/v1/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    started = time.monotonic()
    # Scope comes from the caller, never from the model or DSL text.
    companies = frozenset(req.companies) if req.companies else ALL_COMPANIES
    scope = UserScope(user_id="web-ui", allowed_companies=companies, max_estimated_rows=req.max_rows)
    text = req.query.strip()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    def fail(message: str) -> QueryResponse:
        return QueryResponse(
            agent_response=AgentFPAResponse(user_query=text, execution_status="VALIDATION_ERROR", error_message=message),
            provider=req.provider, mode="not_run", duration_ms=elapsed(),
        )

    try:
        tools = FPATools(scope, executor=clickhouse_executor(get_client()))
    except Exception as exc:
        return fail(f"ClickHouse unavailable: {exc}")
    orchestrator = FPAOrchestrator(tools)
    request = PlanningRequest(request=text)

    try:
        if text.upper().startswith("SELECT"):
            # Already FinOpsExpr: skip the model, keep the same validation/execution gate.
            mode = "direct_dsl"
            result = orchestrator.finalize(request, {"dsl": text}, narrative="")
        elif not provider_configured(req.provider):
            hint = "install Claude Code and run `claude` to log in" if req.provider == "claude-code" else f"set {PROVIDER_KEYS[req.provider][0]}"
            return fail(f"{req.provider} is not configured: {hint}, or type FinOpsExpr starting with SELECT.")
        else:
            mode = "agno_team"
            # Built per request so every member's tools carry this caller's scope.
            team = build_agno_team(model=build_model(req.provider), toolset=tools)
            result = orchestrator.run_with_team(team, request)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}")

    result = result.model_copy(update={
        "cited_data_rows": [{k: _jsonable(v) for k, v in row.items()} for row in result.cited_data_rows],
    })
    response = QueryResponse(
        agent_response=result,
        provider=req.provider,
        mode=mode,
        columns=list(result.cited_data_rows[0]) if result.cited_data_rows else [],
    )
    if result.generated_dsl:
        try:
            compiled = compile_query(result.generated_dsl, security_context=SecurityContext(companies, req.max_rows))
            response.sql, response.params = compiled.sql, compiled.params
        except (ParseError, DSLValidationError, ValueError):
            pass
    response.duration_ms = elapsed()
    return response


@app.get("/api/v1/providers")
def providers() -> list[dict[str, Any]]:
    return [{"id": p, "configured": provider_configured(p)} for p in ("claude-code", "claude-api", "gemini")]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "templates" / "index.html")
