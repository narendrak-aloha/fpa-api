"""role-scoped transitions, driver proposals, locked plan_version guard

Revision ID: 9a3c5e7f1b2d
Revises: 41ef8949dff6
Create Date: 2026-09-10 21:00:00.000000

- state_transition gains a `role` column: which role may make which
  transition is data, not code (RBAC in the governance store).
- plan_driver_proposal: an agent's proposed assumption lands here as a
  Draft; only a second human resolves it, enforced by a CHECK.
- A Locked plan_version row is immutable except for the move to
  Superseded; a Superseded one is immutable outright. Lines were already
  guarded (fn_plan_version_line_guard); this closes the row itself.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "9a3c5e7f1b2d"
down_revision: str | Sequence[str] | None = "41ef8949dff6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_TRANSITIONS = (
    ("plan_version", "Draft", "In-Review", "planner"),
    ("plan_version", "In-Review", "Approved", "controller"),
    ("plan_version", "In-Review", "Draft", "controller"),
    ("plan_version", "Approved", "Locked", "controller"),
    ("plan_version", "Locked", "Superseded", "controller"),
    ("variance_report", "Open", "Investigating", "agent"),
    ("variance_report", "Open", "Investigating", "controller"),
    ("variance_report", "Investigating", "Open", "controller"),
    ("variance_report", "Investigating", "Closed", "controller"),
    ("variance_report", "Open", "Closed", "controller"),
    ("plan_driver_proposal", "Draft", "Approved", "controller"),
    ("plan_driver_proposal", "Draft", "Rejected", "controller"),
)

_ROLELESS_TRANSITIONS = (
    ("plan_version", "Draft", "In-Review"),
    ("plan_version", "In-Review", "Approved"),
    ("plan_version", "In-Review", "Draft"),
    ("plan_version", "Approved", "Locked"),
    ("plan_version", "Locked", "Superseded"),
    ("variance_report", "Open", "Investigating"),
    ("variance_report", "Investigating", "Closed"),
    ("variance_report", "Investigating", "Open"),
    ("variance_report", "Open", "Closed"),
)


def _transition_table(with_role: bool):
    cols = [sa.column("entity_type", sa.Text), sa.column("from_state", sa.Text), sa.column("to_state", sa.Text)]
    if with_role:
        cols.append(sa.column("role", sa.Text))
    return sa.table("state_transition", *cols)


def upgrade() -> None:
    # ---- role-scoped transitions -------------------------------------------
    op.drop_constraint("state_transition_pkey", "state_transition", type_="primary")
    op.add_column("state_transition", sa.Column("role", sa.Text, nullable=True))
    op.execute("DELETE FROM state_transition")
    op.bulk_insert(
        _transition_table(with_role=True),
        [dict(zip(("entity_type", "from_state", "to_state", "role"), t, strict=True)) for t in _ROLE_TRANSITIONS],
    )
    op.alter_column("state_transition", "role", nullable=False)
    op.create_primary_key(
        "state_transition_pkey", "state_transition", ["entity_type", "from_state", "to_state", "role"]
    )

    # ---- Locked / Superseded plan_version rows are immutable ---------------
    # BEFORE triggers fire in name order, so trg_plan_version_bump_revision has
    # already set NEW.revision by the time this runs; revision/updated_at are
    # the bookkeeping columns the move to Superseded is allowed to change.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_plan_version_locked_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.state IN ('Locked', 'Superseded') THEN
                    RAISE EXCEPTION 'plan_version % is %: it cannot be deleted', OLD.id, OLD.state;
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.state = 'Superseded' THEN
                RAISE EXCEPTION 'plan_version % is Superseded: it is immutable', OLD.id;
            END IF;
            IF OLD.state = 'Locked' AND (
                NEW.state <> 'Superseded'
                OR NEW.plan_code IS DISTINCT FROM OLD.plan_code
                OR NEW.scenario_id IS DISTINCT FROM OLD.scenario_id
                OR NEW.covenant_breach IS DISTINCT FROM OLD.covenant_breach
                OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
                OR NEW.approved_by IS DISTINCT FROM OLD.approved_by
            ) THEN
                RAISE EXCEPTION 'plan_version % is Locked: only the move to Superseded is permitted', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_plan_version_locked_guard
        BEFORE UPDATE OR DELETE ON plan_version
        FOR EACH ROW EXECUTE FUNCTION fn_plan_version_locked_guard();
        """
    )

    # ---- agent-proposed assumptions land as drafts -------------------------
    op.create_table(
        "plan_driver_proposal",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("formula", sa.Text, nullable=False),
        sa.Column("is_rate_driver", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("proposed_by", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="Draft"),
        sa.Column("resolved_by", sa.Text, nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("state IN ('Draft', 'Approved', 'Rejected')", name="ck_plan_driver_proposal_state"),
        sa.CheckConstraint(
            "resolved_by IS NULL OR resolved_by <> proposed_by", name="ck_plan_driver_proposal_no_self_approval"
        ),
        sa.CheckConstraint(
            "(state = 'Draft') = (resolved_by IS NULL AND resolved_at IS NULL)",
            name="ck_plan_driver_proposal_resolution",
        ),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_plan_driver_proposal_resolved_guard() RETURNS trigger AS $$
        BEGIN
            IF OLD.state <> 'Draft' THEN
                RAISE EXCEPTION 'plan_driver_proposal % is %: a resolved proposal is immutable', OLD.id, OLD.state;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_plan_driver_proposal_resolved_guard
        BEFORE UPDATE OR DELETE ON plan_driver_proposal
        FOR EACH ROW EXECUTE FUNCTION fn_plan_driver_proposal_resolved_guard();
        """
    )
    op.execute("GRANT SELECT, INSERT ON plan_driver_proposal TO fpa_app, fpa_controller")
    op.execute("GRANT UPDATE (state, resolved_by, resolved_at) ON plan_driver_proposal TO fpa_app")


def downgrade() -> None:
    op.drop_table("plan_driver_proposal")
    op.execute("DROP FUNCTION IF EXISTS fn_plan_driver_proposal_resolved_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_plan_version_locked_guard ON plan_version")
    op.execute("DROP FUNCTION IF EXISTS fn_plan_version_locked_guard()")

    op.drop_constraint("state_transition_pkey", "state_transition", type_="primary")
    op.execute("DELETE FROM state_transition")
    op.drop_column("state_transition", "role")
    op.bulk_insert(
        _transition_table(with_role=False),
        [dict(zip(("entity_type", "from_state", "to_state"), t, strict=True)) for t in _ROLELESS_TRANSITIONS],
    )
    op.create_primary_key("state_transition_pkey", "state_transition", ["entity_type", "from_state", "to_state"])
