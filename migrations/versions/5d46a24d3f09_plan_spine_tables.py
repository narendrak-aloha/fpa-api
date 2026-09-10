"""plan spine tables

Revision ID: 5d46a24d3f09
Revises:
Create Date: 2026-09-10 07:55:34.172539

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

# revision identifiers, used by Alembic.
revision: str = "5d46a24d3f09"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "plan_version",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("plan_code", sa.Text, nullable=False),
        sa.Column("scenario_id", sa.Text, nullable=False, server_default="base"),
        sa.Column("state", sa.Text, nullable=False, server_default="Draft"),
        sa.Column("covenant_breach", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("requested_by", sa.Text, nullable=False),
        sa.Column("approved_by", sa.Text, nullable=True),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "state IN ('Draft','In-Review','Approved','Locked','Superseded')", name="ck_plan_version_state"
        ),
        # Defense in depth: the API also refuses this, but a stale/rogue
        # writer that skips the app layer entirely still can't self-approve.
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> requested_by", name="ck_plan_version_no_self_approval"
        ),
    )

    # Legal state transitions are data, not code -- the app looks these up
    # rather than hardcoding a switch statement per entity type.
    op.create_table(
        "state_transition",
        sa.Column("entity_type", sa.Text, primary_key=True),
        sa.Column("from_state", sa.Text, primary_key=True),
        sa.Column("to_state", sa.Text, primary_key=True),
    )
    op.bulk_insert(
        sa.table(
            "state_transition",
            sa.column("entity_type", sa.Text),
            sa.column("from_state", sa.Text),
            sa.column("to_state", sa.Text),
        ),
        [
            {"entity_type": "plan_version", "from_state": "Draft", "to_state": "In-Review"},
            {"entity_type": "plan_version", "from_state": "In-Review", "to_state": "Approved"},
            {"entity_type": "plan_version", "from_state": "In-Review", "to_state": "Draft"},
            {"entity_type": "plan_version", "from_state": "Approved", "to_state": "Locked"},
            {"entity_type": "plan_version", "from_state": "Locked", "to_state": "Superseded"},
            {"entity_type": "variance_report", "from_state": "Open", "to_state": "Investigating"},
            {"entity_type": "variance_report", "from_state": "Investigating", "to_state": "Closed"},
            {"entity_type": "variance_report", "from_state": "Investigating", "to_state": "Open"},
            {"entity_type": "variance_report", "from_state": "Open", "to_state": "Closed"},
        ],
    )

    op.create_table(
        "plan_driver",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("formula", sa.Text, nullable=False),
        sa.Column("is_rate_driver", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("rate_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "plan_version_line",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "plan_version_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_version.id"),
            nullable=False,
        ),
        sa.Column("scenario_id", sa.Text, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("account", sa.Text, nullable=False),
        sa.Column("period_month", sa.Date, nullable=False),
        sa.Column("dim_signature_hash", sa.Text, nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("amount_functional", sa.Numeric(18, 2), nullable=False),
        sa.Column("driver_derivation_trace", pg.JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "plan_version_id",
            "scenario_id",
            "revision",
            "company",
            "account",
            "period_month",
            "dim_signature_hash",
            name="uq_plan_version_line_grain",
        ),
    )

    op.create_table(
        "plan_fx_rate",
        sa.Column(
            "plan_version_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_version.id"),
            primary_key=True,
        ),
        sa.Column("period_month", sa.Date, primary_key=True),
        sa.Column("currency", sa.Text, primary_key=True),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
    )

    op.create_table(
        "variance_report",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "plan_version_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plan_version.id"),
            nullable=False,
        ),
        sa.Column("cut_label", sa.Text, nullable=False),
        sa.Column("dimension_filter", pg.JSONB, nullable=False),
        sa.Column("vintage", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="Open"),
        sa.Column("materiality_flag", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("advanced_by", sa.Text, nullable=False, server_default="human"),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("state IN ('Open','Investigating','Closed')", name="ck_variance_report_state"),
        sa.CheckConstraint("advanced_by IN ('human','agent')", name="ck_variance_report_advanced_by"),
        # Only a human closes one -- enforced here, not just in the app.
        sa.CheckConstraint(
            "state <> 'Closed' OR advanced_by = 'human'", name="ck_variance_report_close_requires_human"
        ),
    )

    op.create_table(
        "variance_report_line",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "report_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("variance_report.id"),
            nullable=False,
        ),
        sa.Column("rollup_path", sa.Text, nullable=False),
        sa.Column("group_gap", sa.Numeric(18, 2), nullable=False),
        sa.Column("price", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("volume", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("practice_mix", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("grade_mix", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("fx", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("rate", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("efficiency", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("residual", sa.Numeric(18, 2), nullable=False),
        sa.Column("tol", sa.Numeric(18, 2), nullable=False),
        sa.Column("cited_rows", pg.JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "llm_disclosure_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("classes", pg.ARRAY(sa.Text), nullable=False),
        sa.Column("methods", pg.ARRAY(sa.Text), nullable=False),
        sa.Column("scope", pg.JSONB, nullable=False),
        # Never the payload -- only its hash. See src/fpa_be/masking/.
        sa.Column("payload_hash", sa.Text, nullable=False),
    )

    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("entity_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("actor_role", sa.Text, nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("prev_hash", sa.Text, nullable=True),
        sa.Column("hash", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("audit_event")
    op.drop_table("llm_disclosure_log")
    op.drop_table("variance_report_line")
    op.drop_table("variance_report")
    op.drop_table("plan_fx_rate")
    op.drop_table("plan_version_line")
    op.drop_table("plan_driver")
    op.drop_table("state_transition")
    op.drop_table("plan_version")
