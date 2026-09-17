"""Load db/seed.yaml into the governance store. Idempotent: existing rows are kept.

The YAML is a list with one entry per row; each entry's ``table`` key names the
target ``<schema>.<table>``. Natural keys (model_code, plan_version_code) are
resolved to generated ids while loading.

Usage:  python -m db.seed [--file db/seed.yaml]   (run `alembic -c db/alembic.ini upgrade head` first)
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Numeric, create_engine, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from db.models import (
    SCHEMA,
    AppUser, AuditEvent, DimAccount, DimCompany, DimCostCenter, LedgerVintage, PlanDriver, PlanFxRate,
    PlanningDimension, PlanningMeasure, PlanningModel, PlanStateTransition, PlanVersion, Role, ScenarioSet, UserRole,
)

DB_DIR = Path(__file__).resolve().parent

# Load order respects foreign keys.
TABLES = [
    Role, AppUser, UserRole, DimCompany, DimAccount, DimCostCenter, LedgerVintage,
    PlanningModel, PlanningDimension, PlanningMeasure, PlanDriver, PlanStateTransition,
    PlanVersion, ScenarioSet, PlanFxRate, AuditEvent,
]


def database_url() -> str:
    if os.getenv("FPA_GOVERNANCE_DB_URL"):
        return os.environ["FPA_GOVERNANCE_DB_URL"]
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(DB_DIR / "alembic.ini")
    return parser["alembic"]["sqlalchemy.url"]


def event_hash(row: dict[str, Any]) -> str:
    payload = json.dumps(row["payload"], separators=(",", ":"))
    chain_input = "|".join([row["actor_user_id"] or "", row["entity_type"], row["entity_id"], row["action"], payload])
    return hashlib.sha256(chain_input.encode()).hexdigest()


def resolve(conn: Connection, model: type, row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    if "model_code" in row and model is not PlanningModel:
        code = row.pop("model_code")
        row["model_id"] = conn.execute(select(PlanningModel.model_id).where(PlanningModel.model_code == code)).scalar_one()
    if "plan_version_code" in row and model is not PlanVersion:
        code = row.pop("plan_version_code")
        row["plan_version_id"] = conn.execute(
            select(PlanVersion.plan_version_id).where(PlanVersion.plan_version_code == code)
        ).scalar_one()
    for column in model.__table__.columns:
        if isinstance(column.type, Numeric) and isinstance(row.get(column.name), (str, int, float)):
            row[column.name] = Decimal(str(row[column.name]))
    if model is AuditEvent and not row.get("event_hash"):
        row["event_hash"] = event_hash(row)
    return row


def group_by_table(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    known = {f"{SCHEMA}.{model.__tablename__}" for model in TABLES}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, record in enumerate(records, start=1):
        row = dict(record)
        table = row.pop("table", None)
        if table not in known:
            raise ValueError(f"record {index}: unknown or missing table {table!r}")
        grouped.setdefault(table.split(".", 1)[1], []).append(row)
    return grouped


def load(conn: Connection, records: list[dict[str, Any]]) -> dict[str, int]:
    seed = group_by_table(records)
    counts: dict[str, int] = {}
    for model in TABLES:
        rows = [resolve(conn, model, row) for row in seed.get(model.__tablename__, [])]
        if not rows:
            continue
        inserted = conn.execute(insert(model).values(rows).on_conflict_do_nothing().returning(literal_column("1")))
        counts[model.__tablename__] = len(inserted.all())
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=DB_DIR / "seed.yaml")
    args = parser.parse_args()
    seed = yaml.safe_load(args.file.read_text())
    engine = create_engine(database_url())
    with engine.begin() as conn:
        counts = load(conn, seed)
    for table, inserted in counts.items():
        print(f"  {table:<26} +{inserted}")


if __name__ == "__main__":
    main()
