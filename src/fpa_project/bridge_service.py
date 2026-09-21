"""Run a bridge end to end: DSL -> matched lines -> decomposition -> report.

The compiler fetches exactly the lines the bridge runs over; ``dsl.bridge``
decomposes them; this module is the part that has to talk to two databases,
so it is the part that names the vintage, persists the report with its
citations, and routes a material gap to escalation.

Everything an agent can do to a report goes through ``set_status``, and the
database refuses the one thing an agent must never do: close it.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from db.config import database_url
from fpa_project.dsl.bridge import BridgeLine, BridgeNode, BridgeResult, Convention, decompose
from fpa_project.dsl.compiler import SecurityContext, compile_query, vintage_lookup
from fpa_project.dsl.errors import DSLValidationError
from fpa_project.dsl.parser import parse_query

SCHEMA = "fpa_governance"

# A gap this large, in report currency, is escalated rather than left OPEN.
# A fixed figure, deliberately: a threshold that scaled with the plan would
# make a big miss on a big plan look ordinary.
DEFAULT_MATERIALITY = Decimal("250000.00")

Executor = Callable[[str, Mapping[str, Any]], Iterable[Mapping[str, Any]]]

_lock = threading.Lock()
_engine: Engine | None = None


def postgres() -> Engine:
    global _engine
    with _lock:
        if _engine is None:
            _engine = create_engine(database_url(), pool_pre_ping=True)
        return _engine


class BridgeError(ValueError):
    """The bridge could not run, and says why."""


@dataclass
class BridgeReport:
    report_id: str | None
    result: BridgeResult
    # None when the report reads no ledger close (a re-forecast against its
    # baseline); as_of_vintage is a foreign key, so 0 is not a stand-in.
    vintage: int | None
    vintage_closed_at: str | None
    vintage_note: str
    dsl: str
    measure: str
    plan_version: str
    scenario: str
    status: str
    materiality_threshold: Decimal
    citations: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        root = self.result.root
        return {
            "report_id": self.report_id,
            "status": self.status,
            "dsl": self.dsl,
            "measure": self.measure,
            "plan_version": self.plan_version,
            "scenario": self.scenario,
            "vintage": {"vintage": self.vintage, "closed_at": self.vintage_closed_at, "note": self.vintage_note},
            "report_currency": "USD",
            "convention": self.result.convention.value,
            "rollup": list(self.result.levels),
            "line_count": self.result.lines,
            "materiality_threshold": str(self.materiality_threshold),
            "ties": all(node.ties for node in self.result.walk()),
            "breaks": [list(node.path) for node in self.result.breaks()],
            "mix_by_level": {k: str(v.quantize(Decimal("0.01"))) for k, v in self.result.mix_by_level().items()},
            "root": root.to_dict(self.result.levels),
        }


def run_bridge(
    dsl: str,
    executor: Executor,
    scope: SecurityContext,
    *,
    actor: str,
    convention: Convention = Convention.VOLUME_FIRST,
    materiality: Decimal = DEFAULT_MATERIALITY,
    persist: bool = True,
) -> BridgeReport:
    """Compile, fetch, decompose, and (by default) persist.

    Refuses rather than degrades: no matched lines, or an AS OF before the
    first close, are errors with a message, never an empty report.
    """
    query = parse_query(dsl)
    if not query.bridge or query.plan is None:
        raise BridgeError("a bridge needs COMPARE PLAN ... TO ACTUAL BRIDGE")
    compiled = compile_query(dsl, security_context=scope)

    vintage_sql, vintage_params = vintage_lookup(query.as_of)
    closes = list(executor(vintage_sql, vintage_params))
    if not closes:
        raise BridgeError(f"no ledger close on or before {query.as_of}; the books did not exist yet")
    close = closes[0]

    rows = list(executor(compiled.sql, compiled.params))
    if not rows:
        raise BridgeError("no plan line matched an actual line for this cut; nothing to bridge")

    levels = tuple(query.dimensions)
    lines = [_line(row, levels) for row in rows]
    measure = ", ".join(_measure_name(m.name) for m in query.measures)
    result = decompose(lines, levels=levels, convention=convention)

    gap = result.root.gap
    status = "ESCALATED" if abs(gap) >= materiality else "OPEN"
    citations = [
        {
            "company_code": row["a.company"], "period_month": row["a.period_month"],
            "account_code": row["a.account"], "dim_signature_hash": _text(row["a.dim_signature_hash"]),
            "plan_amount": Decimal(str(row["plan_amount"])), "actual_amount": Decimal(str(row["actual_amount"])),
            "path": tuple(str(row[f"a.{d}"]) for d in levels),
        }
        for row in rows
    ]
    report = BridgeReport(
        report_id=None, result=result,
        vintage=int(close["vintage"]), vintage_closed_at=str(close["closed_at"]), vintage_note=str(close["note"]),
        dsl=dsl, measure=measure, plan_version=query.plan.version, scenario=query.plan.scenario,
        status=status, materiality_threshold=materiality, citations=citations,
    )
    if persist:
        report.report_id = _persist(report, actor)
    return report


def _measure_name(value: Any) -> str:
    return getattr(value, "metric", None) or getattr(value, "name", None) or str(value)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _line(row: Mapping[str, Any], levels: tuple[str, ...]) -> BridgeLine:
    d = lambda key: Decimal(str(row[key]))  # noqa: E731 - a local shorthand
    return BridgeLine(
        path=tuple(str(row[f"a.{level}"]) for level in levels),
        plan_quantity=d("plan_quantity"), actual_quantity=d("actual_quantity"),
        plan_unit_price=d("plan_unit_price"), actual_unit_price=d("actual_unit_price"),
        plan_fx=d("plan_fx"), actual_fx=d("actual_fx"),
        account_type=str(row["account_type"]),
        key=(row["a.company"], row["a.period_month"], row["a.account"], _text(row["a.dim_signature_hash"])),
        plan_amount=d("plan_amount"), actual_amount=d("actual_amount"),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _persist(report: BridgeReport, actor: str) -> str:
    result = report.result
    with postgres().begin() as conn:
        conn.execute(text("SELECT set_config('fpa.actor', :actor, true)"), {"actor": actor})
        plan_version_id = conn.execute(
            text(f"SELECT plan_version_id FROM {SCHEMA}.plan_version WHERE plan_version_code = :code"),
            {"code": report.plan_version},
        ).scalar()
        if plan_version_id is None:
            raise BridgeError(f"plan version {report.plan_version!r} is not in the governance store")
        requested_id = report.report_id or str(uuid.uuid4())
        report_id = conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.variance_report "
                "  (variance_report_id, plan_version_id, scenario_code, as_of_vintage, status, created_by, dsl, measure, rollup, "
                "   convention, vintage_closed_at, line_count, total_gap, materiality_threshold, ties) "
                "VALUES (CAST(:report_id AS uuid), :pv, :scenario, :vintage, :status, :actor, :dsl, :measure, :rollup, "
                "        :convention, :closed_at, :lines, :gap, :materiality, :ties) "
                "ON CONFLICT (variance_report_id) DO NOTHING RETURNING variance_report_id"
            ),
            {
                "report_id": requested_id, "pv": plan_version_id, "scenario": report.scenario, "vintage": report.vintage,
                "status": report.status, "actor": actor, "dsl": report.dsl, "measure": report.measure,
                "rollup": list(result.levels), "convention": result.convention.value,
                "closed_at": report.vintage_closed_at, "lines": result.lines,
                "gap": _cents(result.root.gap), "materiality": report.materiality_threshold,
                "ties": all(node.ties for node in result.walk()),
            },
        ).scalar()
        if report_id is None:
            return requested_id

        line_no = 0
        leaf_line_no: dict[tuple[str, ...], int] = {}
        for node in result.walk():
            line_no += 1
            if node.level == len(result.levels):
                leaf_line_no[node.path] = line_no
            conn.execute(
                text(
                    f"INSERT INTO {SCHEMA}.variance_report_line "
                    "  (variance_report_id, line_no, dimension_key, plan_amount, actual_amount, price_variance, "
                    "   volume_variance, mix_variance, fx_variance, rate_variance, efficiency_variance, residual, "
                    "   level, path, line_count, tolerance, mix_between_variance) "
                    "VALUES (:report, :line_no, CAST(:key AS jsonb), :plan, :actual, :price, :volume, :mix, :fx, "
                    "        :rate, :efficiency, :residual, :level, :path, :count, :tolerance, :between)"
                ),
                _line_row(report_id, line_no, node, result.levels),
            )

        for citation in report.citations:
            conn.execute(
                text(
                    f"INSERT INTO {SCHEMA}.variance_report_citation "
                    "  (variance_report_id, line_no, company_code, period_month, account_code, dim_signature_hash, "
                    "   plan_amount, actual_amount) "
                    "VALUES (:report, :line_no, :company, :month, :account, :sig, :plan, :actual) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "report": report_id, "line_no": leaf_line_no[citation["path"]],
                    "company": citation["company_code"], "month": citation["period_month"],
                    "account": citation["account_code"], "sig": citation["dim_signature_hash"],
                    "plan": citation["plan_amount"], "actual": citation["actual_amount"],
                },
            )
    return str(report_id)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _line_row(report_id: Any, line_no: int, node: BridgeNode, levels: tuple[str, ...]) -> dict[str, Any]:
    """Legs rounded to cents, with the residual taking the rounding.

    The check constraint requires the rounded legs to sum to the rounded gap,
    so the residual is recomputed from the rounded values rather than rounded
    itself: whatever rounding left over is the residual's job to hold.
    """
    plan, actual = _cents(node.plan_amount), _cents(node.actual_amount)
    legs = {k: _cents(getattr(node, k)) for k in ("price", "volume", "mix", "fx", "rate", "efficiency")}
    residual = (actual - plan) - sum(legs.values(), Decimal(0))
    return {
        "report": report_id, "line_no": line_no,
        "key": json.dumps(dict(zip(levels, node.path)) | {"level": node.level}),
        "plan": plan, "actual": actual, **legs, "residual": residual,
        "level": node.level, "path": list(node.path), "count": node.line_count,
        "tolerance": node.tol, "between": _cents(node.mix_between),
    }


# ---------------------------------------------------------------------------
# Reading and moving a report
# ---------------------------------------------------------------------------
def report_in_scope(report_id: str, companies: frozenset[str]) -> bool:
    """Stored totals are indivisible: require access to every cited entity.

    Filtering citations alone would still expose the report's wider totals.
    Empty or legacy reports without citations fail closed.
    """
    if not companies:
        return False
    with postgres().begin() as conn:
        return bool(conn.execute(
            text(
                f"SELECT count(*) > 0 AND bool_and(company_code = ANY(CAST(:companies AS text[]))) "
                f"FROM {SCHEMA}.variance_report_citation WHERE variance_report_id = CAST(:id AS uuid)"
            ),
            {"id": report_id, "companies": sorted(companies)},
        ).scalar())


def load_report(report_id: str) -> dict[str, Any] | None:
    with postgres().begin() as conn:
        head = conn.execute(
            text(
                "SELECT r.variance_report_id, v.plan_version_code, r.scenario_code, r.as_of_vintage, "
                "       r.vintage_closed_at, r.status, r.created_by, r.created_at, r.closed_by, r.closed_at, "
                "       r.dsl, r.measure, r.rollup, r.convention, r.report_currency, r.line_count, r.total_gap, "
                "       r.materiality_threshold, r.ties, r.status_changed_by "
                f"FROM {SCHEMA}.variance_report r JOIN {SCHEMA}.plan_version v USING (plan_version_id) "
                "WHERE r.variance_report_id = CAST(:id AS uuid)"
            ),
            {"id": report_id},
        ).mappings().first()
        if head is None:
            return None
        lines = conn.execute(
            text(
                "SELECT line_no, level, path, line_count, plan_amount, actual_amount, "
                "       actual_amount - plan_amount AS gap, price_variance, volume_variance, mix_variance, "
                "       mix_between_variance, fx_variance, rate_variance, efficiency_variance, residual, tolerance "
                f"FROM {SCHEMA}.variance_report_line WHERE variance_report_id = CAST(:id AS uuid) ORDER BY line_no"
            ),
            {"id": report_id},
        ).mappings().all()
    out = dict(head)
    out["variance_report_id"] = str(out["variance_report_id"])
    out["lines"] = [dict(line) for line in lines]
    return out


def citations_for(report_id: str, path: list[str], limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    """The cube rows behind a node: every leaf under its path, with the vintage."""
    with postgres().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT c.company_code, c.period_month, c.account_code, c.dim_signature_hash, "
                "       c.plan_amount, c.actual_amount, l.path, r.as_of_vintage AS vintage, r.vintage_closed_at "
                f"FROM {SCHEMA}.variance_report_citation c "
                f"JOIN {SCHEMA}.variance_report_line l USING (variance_report_id, line_no) "
                f"JOIN {SCHEMA}.variance_report r USING (variance_report_id) "
                "WHERE c.variance_report_id = CAST(:id AS uuid) AND l.path[1:cardinality(CAST(:path AS text[]))] = CAST(:path AS text[]) "
                "ORDER BY l.path, c.company_code, c.period_month, c.account_code, c.dim_signature_hash LIMIT :limit OFFSET :offset"
            ),
            {"id": report_id, "path": path, "limit": limit, "offset": offset},
        ).mappings().all()
    return [dict(row) for row in rows]


class StatusRefused(ValueError):
    pass


def set_status(report_id: str, status: str, actor: str) -> dict[str, Any]:
    """Move a report. The database decides whether the actor may.

    An agent may move a report to INVESTIGATING; the trigger refuses CLOSED
    for anyone who is not a human with the controller or cfo role, refuses
    reopening, and refuses downgrading an escalation.
    """
    if status not in {"OPEN", "INVESTIGATING", "ESCALATED", "REVIEWED", "CLOSED"}:
        raise StatusRefused(f"{status!r} is not a variance report status")
    try:
        with postgres().begin() as conn:
            conn.execute(text("SELECT set_config('fpa.actor', :actor, true)"), {"actor": actor})
            row = conn.execute(
                text(
                    f"UPDATE {SCHEMA}.variance_report SET status = :status "
                    "WHERE variance_report_id = CAST(:id AS uuid) RETURNING status, closed_by, status_changed_by"
                ),
                {"status": status, "id": report_id},
            ).mappings().first()
    except Exception as exc:  # noqa: BLE001 - the trigger's message is the answer
        message = str(getattr(exc, "orig", exc)).splitlines()[0]
        raise StatusRefused(message) from exc
    if row is None:
        raise StatusRefused(f"no variance report {report_id}")
    return dict(row)


def vintage_bridge(dsl: str, left_as_of: str, right_as_of: str, executor: Executor, scope: SecurityContext) -> dict[str, Any]:
    """The same bridge at two closes, and why its gap moved between them.

    The DSL must not carry its own AS OF: the two instants are the question,
    and a query that already named one would be ambiguous about which side it
    meant. Each side is compiled with the caller's scope, names the vintage it
    resolved to, and must match at least one line.
    """
    from dataclasses import replace

    from fpa_project.dsl.bridge import vintage_delta
    from fpa_project.dsl.compiler import Compiler

    query = parse_query(dsl)
    if not query.bridge or query.plan is None:
        raise BridgeError("a vintage bridge needs COMPARE PLAN ... TO ACTUAL BRIDGE")
    if query.as_of:
        raise BridgeError("leave AS OF out of the DSL; the two closes are given as left_as_of and right_as_of")
    levels = tuple(query.dimensions)
    sides = []
    for instant in (left_as_of, right_as_of):
        closes = list(executor(*vintage_lookup(instant)))
        if not closes:
            raise BridgeError(f"no ledger close on or before {instant}; the books did not exist yet")
        compiled = Compiler(security_context=scope).compile(replace(query, as_of=instant))
        rows = list(executor(compiled.sql, compiled.params))
        if not rows:
            raise BridgeError(f"no plan line matched an actual line as of {instant}; nothing to bridge")
        sides.append({"close": closes[0], "lines": [_line(row, levels) for row in rows]})
    left, right = sides
    root = vintage_delta(left["lines"], right["lines"], levels)
    vintage = lambda side: {k: str(side["close"][k]) for k in ("vintage", "closed_at", "note")}  # noqa: E731
    return {
        "dsl": dsl, "rollup": list(levels), "report_currency": "USD",
        "left": vintage(left), "right": vintage(right),
        "ties": all(abs(node.residual) < Decimal("0.005") for node in root.walk()),
        "root": root.to_dict(),
    }
