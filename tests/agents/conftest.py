import os

import pytest
from agno.run.base import RunContext

from fpa_be.compiler.security import SecurityContext

os.environ.setdefault("CLICKHOUSE_HOST", "localhost")
os.environ.setdefault("CLICKHOUSE_PORT", "8123")

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))


def make_run_context(security_context: SecurityContext = PL_SCOPE, user_id: str | None = "planner@example.com") -> RunContext:
    return RunContext(
        run_id="test-run",
        session_id="test-session",
        user_id=user_id,
        dependencies={"security_context": security_context},
    )


@pytest.fixture
def run_context():
    return make_run_context()
