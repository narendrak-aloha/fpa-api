"""Load db/seed.yaml into the governance store. Idempotent: existing rows are kept.

The YAML is a list with one entry per row; each entry's ``table`` key names the
target ``<schema>.<table>``. Natural keys (model_code, plan_version_code) are
resolved to generated ids while loading.

Usage:  python -m db.seed [--file db/seed.yaml]   (run `alembic -c db/alembic.ini upgrade head` first)
"""

from __future__ import annotations

import argparse
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Numeric, create_engine, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from db.config import database_url
from db.models import (
    SCHEMA,
    AppUser, AuditEvent, DimAccount, DimCompany, DimCostCenter, LedgerVintage, PlanDriver, PlanDriverBinding,
    PlanFxRate, PlanningDimension, PlanningMeasure, PlanningModel, PlanStateTransition, PlanVersion, Role,
    ScenarioDriverOverride, ScenarioSet, UserCompanyScope, UserRole,
)

DB_DIR = Path(__file__).resolve().parent

# Load order respects foreign keys.
TABLES = [
    Role, AppUser, UserRole, DimCompany, UserCompanyScope, DimAccount, DimCostCenter, LedgerVintage,
    PlanningModel, PlanningDimension, PlanningMeasure, PlanDriver, PlanDriverBinding, PlanStateTransition,
    PlanVersion, ScenarioSet, ScenarioDriverOverride, PlanFxRate, AuditEvent,
]


def token_hash(token: str) -> str:
    """What app_user.api_token_hash holds: the token is never stored."""
    return hashlib.sha256(token.encode()).hexdigest()


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
    if "scenario_code" in row and model is ScenarioDriverOverride:
        # plan_version_code was resolved to plan_version_id just above.
        code, plan_version_id = row.pop("scenario_code"), row.pop("plan_version_id")
        row["scenario_set_id"] = conn.execute(
            select(ScenarioSet.scenario_set_id)
            .where(ScenarioSet.scenario_code == code, ScenarioSet.plan_version_id == plan_version_id)
        ).scalar_one()
    if "driver_code" in row and model is not PlanDriver:
        code = row.pop("driver_code")
        # scalar_one raises if the seed ever grows a second vintage of a driver,
        # which is the right moment to make the reference explicit.
        row["driver_id"] = conn.execute(select(PlanDriver.driver_id).where(PlanDriver.driver_code == code)).scalar_one()
    for column in model.__table__.columns:
        if isinstance(column.type, Numeric) and isinstance(row.get(column.name), (str, int, float)):
            row[column.name] = Decimal(str(row[column.name]))
    if model is AppUser and "api_token" in row:
        row["api_token_hash"] = token_hash(row.pop("api_token"))
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
        # One INSERT per distinct key set. A multi-row insert takes its column
        # list from the first row, so a column that only some rows set (say
        # is_human on a service identity) would be dropped from the others
        # without a word and the server default applied instead.
        inserted = 0
        by_keys: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in rows:
            by_keys.setdefault(tuple(sorted(row)), []).append(row)
        for group in by_keys.values():
            result = conn.execute(insert(model).values(group).on_conflict_do_nothing().returning(literal_column("1")))
            inserted += len(result.all())
        counts[model.__tablename__] = inserted
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=DB_DIR / "seed.yaml")
    args = parser.parse_args()
    seed = yaml.safe_load(args.file.read_text())
    engine = create_engine(database_url())
    with engine.begin() as conn:
        # Plan FX rates are controller-only fields (migration 011). The seed
        # writes them as the controller who owns them.
        conn.execute(text("SELECT set_config('fpa.actor', 'u-controller', true)"))
        counts = load(conn, seed)
    for table, inserted in counts.items():
        print(f"  {table:<26} +{inserted}")


if __name__ == "__main__":
    main()
