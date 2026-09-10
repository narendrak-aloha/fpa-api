"""governance enforcement

Revision ID: 41ef8949dff6
Revises: 5d46a24d3f09
Create Date: 2026-09-10 07:55:34.446431

DB-enforced guarantees the assignment names explicitly: locked-version
immutability, tamper-evident audit, and field-level permission on the
covenant flag / rate fields. Everything here is meant to be attacked
directly with psql, not just exercised through the API.
"""
import os
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "41ef8949dff6"
down_revision: str | Sequence[str] | None = "5d46a24d3f09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dev-default passwords, overridable via env for anything beyond local/demo use.
APP_DB_PASSWORD = os.environ.get("FPA_APP_DB_PASSWORD", "fpa_app_dev")
CONTROLLER_DB_PASSWORD = os.environ.get("FPA_CONTROLLER_DB_PASSWORD", "fpa_controller_dev")


def upgrade() -> None:
    # ---- roles -----------------------------------------------------------
    # fpa_app is what the API connects as day to day; fpa_controller is a
    # second, higher-privilege login used only for covenant/rate writes.
    # Neither can bypass the other's restrictions by re-authenticating as
    # itself -- the split is enforced by Postgres, not by application code
    # choosing to be polite.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fpa_app') THEN
                CREATE ROLE fpa_app LOGIN PASSWORD '{APP_DB_PASSWORD}';
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fpa_controller') THEN
                CREATE ROLE fpa_controller LOGIN PASSWORD '{CONTROLLER_DB_PASSWORD}';
            END IF;
        END
        $$;
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fpa_app, fpa_controller")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fpa_app, fpa_controller")

    # ---- field-level permission on covenant / rate fields -----------------
    # A table-level UPDATE grant covers every column, so a column-level
    # REVOKE on top of it is a no-op -- the blanket UPDATE granted above has
    # to come off *both* roles first, then be re-granted column-by-column.
    # Revoking from fpa_app alone would leave fpa_controller's original
    # blanket grant intact, letting it write columns it has no business
    # touching (e.g. plan_version.state) -- this was caught by
    # tests/governance/test_plan_version.py and test_plan_driver.py.
    op.execute("REVOKE UPDATE ON plan_version FROM fpa_app, fpa_controller")
    op.execute(
        "GRANT UPDATE (plan_code, scenario_id, state, requested_by, approved_by) "
        "ON plan_version TO fpa_app"
    )
    op.execute("GRANT UPDATE (covenant_breach) ON plan_version TO fpa_controller")

    op.execute("REVOKE UPDATE ON plan_driver FROM fpa_app, fpa_controller")
    op.execute(
        "GRANT UPDATE (name, formula, is_rate_driver, effective_date) ON plan_driver TO fpa_app"
    )
    op.execute("GRANT UPDATE (rate_value) ON plan_driver TO fpa_controller")

    # ---- audit_event is append-only, for anyone --------------------------
    op.execute("REVOKE UPDATE, DELETE ON audit_event FROM fpa_app, fpa_controller")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_audit_event_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only: % on row % is not permitted', TG_OP, OLD.id;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_event_append_only
        BEFORE UPDATE OR DELETE ON audit_event
        FOR EACH ROW EXECUTE FUNCTION fn_audit_event_append_only();
        """
    )

    # ---- tamper-evident, hash-chained audit log ---------------------------
    # One global chain (not per-entity): every insert links to the previous
    # row regardless of entity_type, so altering any historical row breaks
    # verification for every row after it, not just its own entity's history.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_audit_event_hash() RETURNS trigger AS $$
        DECLARE
            v_prev_hash text;
        BEGIN
            SELECT hash INTO v_prev_hash FROM audit_event ORDER BY id DESC LIMIT 1;
            IF v_prev_hash IS NULL THEN
                v_prev_hash := repeat('0', 64);
            END IF;
            NEW.prev_hash := v_prev_hash;
            NEW.hash := encode(
                digest(
                    v_prev_hash || NEW.entity_type || NEW.entity_id || NEW.action
                    || NEW.actor || NEW.actor_role || NEW.payload::text || NEW.created_at::text,
                    'sha256'
                ),
                'hex'
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_event_hash
        BEFORE INSERT ON audit_event
        FOR EACH ROW EXECUTE FUNCTION fn_audit_event_hash();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_verify_audit_chain()
        RETURNS TABLE(bad_id bigint, reason text) AS $$
            WITH chain AS (
                SELECT id, entity_type, entity_id, action, actor, actor_role, payload, created_at,
                       prev_hash, hash,
                       LAG(hash) OVER (ORDER BY id) AS expected_prev_hash
                FROM audit_event
            ),
            checked AS (
                SELECT id,
                       prev_hash IS DISTINCT FROM COALESCE(expected_prev_hash, repeat('0', 64))
                           AS link_broken,
                       hash IS DISTINCT FROM encode(
                           digest(
                               COALESCE(prev_hash, '') || entity_type || entity_id || action
                               || actor || actor_role || payload::text || created_at::text,
                               'sha256'
                           ),
                           'hex'
                       ) AS hash_mismatch
                FROM chain
            )
            SELECT id,
                   CASE WHEN link_broken THEN 'prev_hash link broken' ELSE 'hash mismatch' END
            FROM checked
            WHERE link_broken OR hash_mismatch
            ORDER BY id;
        $$ LANGUAGE sql STABLE;
        """
    )

    # ---- locked plan versions are immutable -------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_plan_version_line_guard() RETURNS trigger AS $$
        DECLARE
            v_state text;
        BEGIN
            SELECT state INTO v_state FROM plan_version WHERE id = OLD.plan_version_id;
            IF v_state = 'Locked' THEN
                RAISE EXCEPTION 'plan_version % is Locked: lines are immutable', OLD.plan_version_id;
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
        CREATE TRIGGER trg_plan_version_line_guard
        BEFORE UPDATE OR DELETE ON plan_version_line
        FOR EACH ROW EXECUTE FUNCTION fn_plan_version_line_guard();
        """
    )

    # ---- optimistic concurrency -------------------------------------------
    # The app still must gate its UPDATE on the revision it read
    # (WHERE id = $1 AND revision = $2); this trigger makes the monotonic
    # bump itself impossible to forget or fake from the app side.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_plan_version_bump_revision() RETURNS trigger AS $$
        BEGIN
            NEW.revision := OLD.revision + 1;
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_plan_version_bump_revision
        BEFORE UPDATE ON plan_version
        FOR EACH ROW EXECUTE FUNCTION fn_plan_version_bump_revision();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_plan_version_bump_revision ON plan_version")
    op.execute("DROP FUNCTION IF EXISTS fn_plan_version_bump_revision()")
    op.execute("DROP TRIGGER IF EXISTS trg_plan_version_line_guard ON plan_version_line")
    op.execute("DROP FUNCTION IF EXISTS fn_plan_version_line_guard()")
    op.execute("DROP FUNCTION IF EXISTS fn_verify_audit_chain()")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_hash ON audit_event")
    op.execute("DROP FUNCTION IF EXISTS fn_audit_event_hash()")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_append_only ON audit_event")
    op.execute("DROP FUNCTION IF EXISTS fn_audit_event_append_only()")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fpa_app, fpa_controller")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM fpa_app, fpa_controller")
    op.execute("DROP ROLE IF EXISTS fpa_controller")
    op.execute("DROP ROLE IF EXISTS fpa_app")
