"""SQLAlchemy models for the FP&A governance store (Postgres schema ``fpa_governance``).

These models are the source for ``alembic revision --autogenerate``. Triggers and
functions are not expressible here; they live in the migrations.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    MetaData, Numeric, SmallInteger, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "fpa_governance"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


NOW = text("now()")
GEN_UUID = text("gen_random_uuid()")


def fk(target: str, **kwargs: Any) -> ForeignKey:
    return ForeignKey(f"{SCHEMA}.{target}", **kwargs)


# --- 001 identity and access -------------------------------------------------

class AppUser(Base):
    __tablename__ = "app_user"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class Role(Base):
    __tablename__ = "role"

    role_code: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text)


class UserRole(Base):
    __tablename__ = "user_role"

    user_id: Mapped[str] = mapped_column(Text, fk("app_user.user_id"), primary_key=True)
    role_code: Mapped[str] = mapped_column(Text, fk("role.role_code"), primary_key=True)


# --- 002 reference dimensions ------------------------------------------------

class DimCompany(Base):
    __tablename__ = "dim_company"
    __table_args__ = (
        CheckConstraint("country_code = upper(country_code)", name="country_code_upper"),
        CheckConstraint("functional_currency = upper(functional_currency)", name="functional_currency_upper"),
    )

    company_code: Mapped[str] = mapped_column(Text, primary_key=True)
    company_name: Mapped[str] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(CHAR(2))
    region: Mapped[str] = mapped_column(Text)
    functional_currency: Mapped[str] = mapped_column(CHAR(3))


class DimAccount(Base):
    __tablename__ = "dim_account"
    __table_args__ = (
        CheckConstraint("account_type IN ('Revenue', 'COGS', 'OpEx')", name="account_type"),
        CheckConstraint("engine_tag IN ('Services', 'Recurring', 'Shared')", name="engine_tag"),
    )

    account_code: Mapped[str] = mapped_column(Text, primary_key=True)
    account_name: Mapped[str] = mapped_column(Text)
    account_type: Mapped[str] = mapped_column(Text)
    engine_tag: Mapped[str] = mapped_column(Text)


class DimCostCenter(Base):
    __tablename__ = "dim_cost_center"

    cost_center_code: Mapped[str] = mapped_column(Text, primary_key=True)
    country_code: Mapped[str] = mapped_column(CHAR(2))
    practice_code: Mapped[str] = mapped_column(Text)


class LedgerVintage(Base):
    __tablename__ = "ledger_vintage"
    __table_args__ = (CheckConstraint("vintage > 0", name="vintage_positive"),)

    vintage: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    closed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text)


# --- 003 planning model registry ---------------------------------------------

class PlanningModel(Base):
    __tablename__ = "planning_model"
    __table_args__ = (CheckConstraint("plan_year BETWEEN 2000 AND 2200", name="plan_year_range"),)

    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    model_code: Mapped[str] = mapped_column(Text, unique=True)
    model_name: Mapped[str] = mapped_column(Text)
    plan_year: Mapped[int] = mapped_column(Integer)
    reporting_currency: Mapped[str] = mapped_column(CHAR(3))
    calc_order_dag: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class PlanningDimension(Base):
    __tablename__ = "planning_dimension"
    __table_args__ = (
        UniqueConstraint("model_id", "ordinal", name="uq_planning_dimension_model_ordinal"),
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
    )

    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id", ondelete="CASCADE"), primary_key=True)
    dimension_code: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(SmallInteger)


class PlanningMeasure(Base):
    __tablename__ = "planning_measure"
    __table_args__ = (
        CheckConstraint("aggregation_type IN ('additive', 'ratio', 'semi_additive')", name="aggregation_type"),
    )

    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id", ondelete="CASCADE"), primary_key=True)
    measure_code: Mapped[str] = mapped_column(Text, primary_key=True)
    aggregation_type: Mapped[str] = mapped_column(Text)
    sql_expression: Mapped[str] = mapped_column(Text)
    available: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class PlanDriver(Base):
    __tablename__ = "plan_driver"
    __table_args__ = (
        UniqueConstraint("model_id", "driver_code", "effective_from", name="uq_plan_driver_code_effective_from"),
        CheckConstraint("value_type IN ('numeric', 'percentage', 'currency', 'count')", name="value_type"),
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'RETIRED')", name="status"),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="effective_range"),
        Index("driver_effective_idx", "model_id", "driver_code", "effective_from", "effective_to"),
    )

    driver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id"))
    driver_code: Mapped[str] = mapped_column(Text)
    driver_name: Mapped[str] = mapped_column(Text)
    formula: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[dt.date] = mapped_column(Date)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    unit: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'ACTIVE'"))
    created_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


# --- 004 plan versions and scenarios -----------------------------------------

class PlanStateTransition(Base):
    __tablename__ = "plan_state_transition"
    __table_args__ = (
        UniqueConstraint("from_state", "to_state", "role_code", name="uq_plan_state_transition_rule"),
    )

    transition_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_state: Mapped[str] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text)
    role_code: Mapped[str] = mapped_column(Text, fk("role.role_code"))


class PlanVersion(Base):
    __tablename__ = "plan_version"
    __table_args__ = (
        CheckConstraint("plan_year BETWEEN 2000 AND 2200", name="plan_year_range"),
        CheckConstraint("state IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'LOCKED', 'SUPERSEDED', 'REJECTED')", name="state"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("approved_by IS NULL OR approved_by <> requested_by", name="no_self_approval"),
        CheckConstraint("state NOT IN ('APPROVED', 'LOCKED') OR covenant_ok", name="covenant_before_approval"),
        CheckConstraint("state NOT IN ('APPROVED', 'LOCKED') OR approved_by IS NOT NULL", name="approver_required"),
    )

    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    plan_version_code: Mapped[str] = mapped_column(Text, unique=True)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("planning_model.model_id"))
    plan_year: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(Text, server_default=text("'DRAFT'"))
    covenant_ok: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    covenant_note: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    approved_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    supersedes_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class ScenarioSet(Base):
    __tablename__ = "scenario_set"
    __table_args__ = (
        UniqueConstraint("plan_version_id", "scenario_code", name="uq_scenario_set_plan_scenario"),
        CheckConstraint("scenario_code ~ '^[a-z][a-z0-9_]*$'", name="scenario_code_format"),
        CheckConstraint("state IN ('DRAFT', 'APPROVED', 'LOCKED')", name="state"),
        Index("one_base_scenario_per_plan", "plan_version_id", unique=True, postgresql_where=text("is_base")),
    )

    scenario_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id", ondelete="CASCADE"))
    scenario_code: Mapped[str] = mapped_column(Text)
    scenario_name: Mapped[str] = mapped_column(Text)
    is_base: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'DRAFT'"))


class ScenarioDriverOverride(Base):
    __tablename__ = "scenario_driver_override"

    scenario_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("scenario_set.scenario_set_id", ondelete="CASCADE"), primary_key=True)
    driver_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_driver.driver_id"), primary_key=True)
    override_value: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    rationale: Mapped[str] = mapped_column(Text)


class PlanFxRate(Base):
    __tablename__ = "plan_fx_rate"
    __table_args__ = (
        CheckConstraint("period_month = date_trunc('month', period_month)::date", name="period_month_first_day"),
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id", ondelete="CASCADE"), primary_key=True)
    period_month: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    from_currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    to_currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True, server_default=text("'USD'"))
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8))


class PlanVersionLine(Base):
    __tablename__ = "plan_version_line"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id", "scenario_code", "company_code", "period_month", "account_code", "dim_signature_hash",
            name="uq_plan_version_line_grain",
        ),
        CheckConstraint("period_month = date_trunc('month', period_month)::date", name="period_month_first_day"),
        CheckConstraint("dim_signature_hash ~ '^[0-9a-f]{16}$'", name="dim_signature_hash_format"),
        CheckConstraint("jsonb_typeof(driver_derivation_trace) = 'object'", name="derivation_trace_object"),
        CheckConstraint("driver_derivation_trace <> '{}'::jsonb", name="derivation_trace_not_empty"),
        CheckConstraint("amount_functional = round(quantity * unit_price, 2)", name="amount_equals_quantity_x_price"),
        Index("plan_line_lookup_idx", "plan_version_id", "scenario_code", "period_month", "company_code", "account_code"),
    )

    plan_line_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id", ondelete="CASCADE"))
    scenario_code: Mapped[str] = mapped_column(Text)
    company_code: Mapped[str] = mapped_column(Text, fk("dim_company.company_code"))
    period_month: Mapped[dt.date] = mapped_column(Date)
    account_code: Mapped[str] = mapped_column(Text, fk("dim_account.account_code"))
    dim_signature_hash: Mapped[str] = mapped_column(CHAR(16))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    amount_functional: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    functional_currency: Mapped[str] = mapped_column(CHAR(3))
    driver_derivation_trace: Mapped[Any] = mapped_column(JSONB)
    source_revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


# --- 005 plan approval -------------------------------------------------------

class PlanApproval(Base):
    __tablename__ = "plan_approval"
    __table_args__ = (
        CheckConstraint("decision IN ('PENDING', 'APPROVED', 'REJECTED')", name="decision"),
        CheckConstraint("decided_by IS NULL OR decided_by <> requested_by", name="no_self_approval"),
        CheckConstraint(
            "(decision = 'PENDING' AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(decision <> 'PENDING' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_complete",
        ),
    )

    approval_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    requested_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    decided_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    decision: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    covenant_ok: Mapped[bool] = mapped_column(Boolean)
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --- 006 variance reporting --------------------------------------------------

class VarianceReport(Base):
    __tablename__ = "variance_report"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'REVIEWED', 'CLOSED')", name="status"),
        CheckConstraint("status <> 'CLOSED' OR (closed_by IS NOT NULL AND closed_at IS NOT NULL)", name="closed_requires_closer"),
    )

    variance_report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    plan_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("plan_version.plan_version_id"))
    scenario_code: Mapped[str] = mapped_column(Text)
    as_of_vintage: Mapped[int | None] = mapped_column(Integer, fk("ledger_vintage.vintage"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'OPEN'"))
    created_by: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    closed_by: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class VarianceReportLine(Base):
    __tablename__ = "variance_report_line"
    __table_args__ = (
        CheckConstraint("line_no > 0", name="line_no_positive"),
        CheckConstraint("jsonb_typeof(dimension_key) = 'object'", name="dimension_key_object"),
        CheckConstraint(
            "round(actual_amount - plan_amount, 2) = round(price_variance + volume_variance + mix_variance + "
            "fx_variance + rate_variance + efficiency_variance + residual, 2)",
            name="legs_tie_to_gap",
        ),
    )

    variance_report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), fk("variance_report.variance_report_id", ondelete="CASCADE"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    dimension_key: Mapped[Any] = mapped_column(JSONB)
    plan_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    actual_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    price_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    volume_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    mix_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    fx_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    rate_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    efficiency_variance: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))
    residual: Mapped[Decimal] = mapped_column(Numeric(20, 2), server_default=text("0"))


# --- 007 audit and disclosure ------------------------------------------------

class LlmDisclosureLog(Base):
    __tablename__ = "llm_disclosure_log"

    disclosure_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=GEN_UUID)
    user_id: Mapped[str] = mapped_column(Text, fk("app_user.user_id"))
    scope: Mapped[Any] = mapped_column(JSONB)
    field_classes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    model_name: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(CHAR(64))
    response_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    disclosed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("audit_entity_idx", "entity_type", "entity_id", "occurred_at"),)

    audit_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    actor_user_id: Mapped[str | None] = mapped_column(Text, fk("app_user.user_id"))
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB)
    previous_hash: Mapped[str | None] = mapped_column(CHAR(64))
    event_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
