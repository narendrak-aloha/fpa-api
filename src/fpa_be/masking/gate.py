"""Phase 13: the single chokepoint every tool's cube-reading path passes
through before a result ever reaches the model.

Pipeline: classify by dimension (never by sniffing the value's text) ->
mask/tokenize personal columns -> hash-log the disclosure to
`llm_disclosure_log` (never the payload itself, only its hash) -> only then
return to the caller. Any failure anywhere in this pipeline blocks the send
-- fail closed, no partial send.
"""

import hashlib
import json
from collections.abc import Sequence

import asyncpg

from fpa_be.compiler.security import SecurityContext
from fpa_be.db import app_dsn

# Personal by type, not by content: any value under these columns is masked
# regardless of what it looks like. `national_id`, `employee_name`, and the
# compensation-bearing employee fields are listed for when a future tool
# exposes `dim_employee` directly -- neither is reachable through today's
# four tools, since `resource_employee` is the only employee-dimension
# column selectable via FinOpsExpr.
PERSONAL_COLUMNS: dict[str, str] = {
    "customer": "customer_identity",
    "resource_employee": "employee_identity",
    "national_id": "national_id",
    "employee_name": "employee_identity",
    "annual_loaded_cost": "compensation",
    "standard_bill_rate": "compensation",
}


class DisclosureLogWriteError(RuntimeError):
    """Raised when the `llm_disclosure_log` write fails. Callers must treat
    this as fail-closed: never send the payload, masked or not, if this is
    raised."""


def _token(cls: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"[{cls}:{digest}]"


def classify_and_mask(columns: Sequence[str], rows: Sequence[Sequence]) -> tuple[list[list], set[str]]:
    """Tokenizes personal columns in place (consistent per raw value, so the
    model can still group/join masked rows) and reports which PII classes
    were found. Columns not in `PERSONAL_COLUMNS` pass through unchanged."""
    personal_idx = {i: PERSONAL_COLUMNS[c] for i, c in enumerate(columns) if c in PERSONAL_COLUMNS}
    if not personal_idx:
        return [list(row) for row in rows], set()

    classes: set[str] = set()
    masked_rows = []
    for row in rows:
        masked = list(row)
        for i, cls in personal_idx.items():
            masked[i] = _token(cls, str(row[i]))
            classes.add(cls)
        masked_rows.append(masked)
    return masked_rows, classes


def _scope_json(security_context: SecurityContext) -> str:
    return json.dumps(
        {
            "allowed_companies": sorted(security_context.allowed_companies)
            if security_context.allowed_companies
            else None,
            "allowed_geo_countries": sorted(security_context.allowed_geo_countries)
            if security_context.allowed_geo_countries
            else None,
        }
    )


async def mask_and_disclose(
    tool_name: str,
    security_context: SecurityContext,
    columns: Sequence[str],
    rows: Sequence[Sequence],
) -> list[list]:
    """The one path from a cube result to what a tool hands the model:
    classify personal columns by name, mask them, log the disclosure
    (classes, method, scope, and a payload hash -- never the payload), and
    only then return the send-safe rows. A failed log write raises and
    blocks the send entirely.
    """
    masked_rows, classes = classify_and_mask(columns, rows)
    payload_hash = hashlib.sha256(json.dumps(masked_rows, default=str).encode("utf-8")).hexdigest()

    conn = await asyncpg.connect(app_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO llm_disclosure_log (tool_name, classes, methods, scope, payload_hash)
            VALUES ($1, $2, $3, $4, $5)
            """,
            tool_name,
            sorted(classes) or ["none"],
            ["tokenize"] if classes else ["none"],
            _scope_json(security_context),
            payload_hash,
        )
    except Exception as exc:
        raise DisclosureLogWriteError(f"failed to record llm_disclosure_log entry: {exc}") from exc
    finally:
        await conn.close()

    return masked_rows
