"""The single chokepoint between cube data and any model.

Pipeline, in order, every time: resolve the caller's scope -> (the tool has
already fetched through the compiler) -> classify by dimension (never by
sniffing the value's text) -> mask/tokenize personal columns -> hash-log the
disclosure to `llm_disclosure_log` (never the payload itself, only its hash)
-> only then hand the result to the model. Any failure anywhere blocks the
send -- fail closed, no partial send.

`disclosure_tool_hook` is how this is attached: as an Agno tool hook on
every agent (see `fpa_be.agents.team`), so the guarantee is structural
rather than something each tool body has to remember to call.
"""

import hashlib
import inspect
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

_WITHHELD = json.dumps({"error": "tool result withheld: the disclosure log could not be written"})


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


def _payload_hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, default=str).encode("utf-8")).hexdigest()


async def _log_disclosure(
    tool_name: str, security_context: SecurityContext, classes: set[str], payload_hash: str
) -> None:
    try:
        conn = await asyncpg.connect(app_dsn())
    except Exception as exc:
        raise DisclosureLogWriteError(f"failed to reach llm_disclosure_log: {exc}") from exc
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


async def mask_and_disclose(
    tool_name: str,
    security_context: SecurityContext,
    columns: Sequence[str],
    rows: Sequence[Sequence],
) -> list[list]:
    """Classify personal columns by name, mask them, log the disclosure
    (classes, method, scope, and a payload hash -- never the payload), and
    only then return the send-safe rows. A failed log write raises and
    blocks the send entirely.
    """
    masked_rows, classes = classify_and_mask(columns, rows)
    await _log_disclosure(tool_name, security_context, classes, _payload_hash(masked_rows))
    return masked_rows


def _cube_payload(result) -> dict | None:
    if not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except ValueError:
        return None
    if isinstance(payload, dict) and "columns" in payload and "rows" in payload:
        return payload
    return None


async def disclosure_tool_hook(function_name, function_call, arguments, run_context=None):
    """Agno tool hook: runs the tool, then gates its result before the model
    ever reads it. Cube results (`columns` + `rows`) are classified and
    masked; every result, cube or not, gets a disclosure-log row. A missing
    scope or a failed log write withholds the result entirely."""
    security_context = (getattr(run_context, "dependencies", None) or {}).get("security_context")
    if security_context is None:
        return json.dumps({"error": "no security_context for this run; tool result withheld"})

    result = function_call(**arguments)
    if inspect.isawaitable(result):
        result = await result

    try:
        payload = _cube_payload(result)
        if payload is None:
            await _log_disclosure(function_name, security_context, set(), _payload_hash(result))
            return result
        payload["rows"] = await mask_and_disclose(function_name, security_context, payload["columns"], payload["rows"])
        return json.dumps(payload, default=str)
    except DisclosureLogWriteError:
        return _WITHHELD
