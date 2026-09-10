"""Attacked over raw connections, the way a psql session would: a Locked
plan_version row can only move to Superseded and a Superseded one not at
all; transitions name the role that may make them; an agent's driver
proposal can only be resolved by someone other than its proposer, once.
"""

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


async def _locked_plan(conn):
    return await conn.fetchval(
        "INSERT INTO plan_version (plan_code, state, requested_by, approved_by) "
        "VALUES ('PV-LOCK', 'Locked', 'alice', 'bob') RETURNING id"
    )


async def _proposal(conn, proposed_by="pl-planner"):
    return await conn.fetchval(
        "INSERT INTO plan_driver_proposal (name, formula, effective_date, proposed_by) "
        "VALUES ('utilisation', '0.74', '2026-01-01', $1) RETURNING id",
        proposed_by,
    )


class TestLockedPlanVersion:
    async def test_no_field_can_be_edited_even_by_superuser(self, superuser_conn):
        plan_id = await _locked_plan(superuser_conn)
        with pytest.raises(asyncpg.RaiseError, match="Locked"):
            await superuser_conn.execute("UPDATE plan_version SET plan_code = 'EDITED' WHERE id = $1", plan_id)

    async def test_it_cannot_move_back_to_an_earlier_state(self, superuser_conn):
        plan_id = await _locked_plan(superuser_conn)
        with pytest.raises(asyncpg.RaiseError):
            await superuser_conn.execute("UPDATE plan_version SET state = 'Approved' WHERE id = $1", plan_id)

    async def test_the_controller_cannot_flip_the_covenant_flag(self, superuser_conn, controller_conn):
        plan_id = await _locked_plan(superuser_conn)
        with pytest.raises(asyncpg.RaiseError):
            await controller_conn.execute("UPDATE plan_version SET covenant_breach = true WHERE id = $1", plan_id)

    async def test_it_cannot_be_deleted(self, superuser_conn):
        plan_id = await _locked_plan(superuser_conn)
        with pytest.raises(asyncpg.RaiseError):
            await superuser_conn.execute("DELETE FROM plan_version WHERE id = $1", plan_id)

    async def test_it_can_be_superseded_and_then_nothing_else(self, superuser_conn):
        plan_id = await _locked_plan(superuser_conn)
        await superuser_conn.execute("UPDATE plan_version SET state = 'Superseded' WHERE id = $1", plan_id)
        with pytest.raises(asyncpg.RaiseError, match="Superseded"):
            await superuser_conn.execute("UPDATE plan_version SET state = 'Locked' WHERE id = $1", plan_id)


async def test_transitions_name_the_role_that_may_make_them(superuser_conn):
    rows = await superuser_conn.fetch(
        "SELECT role FROM state_transition "
        "WHERE entity_type = 'plan_version' AND from_state = 'In-Review' AND to_state = 'Approved'"
    )
    assert {r["role"] for r in rows} == {"controller"}


class TestDriverProposal:
    async def test_it_cannot_be_resolved_by_its_proposer(self, superuser_conn):
        proposal_id = await _proposal(superuser_conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await superuser_conn.execute(
                "UPDATE plan_driver_proposal SET state = 'Approved', resolved_by = 'pl-planner', resolved_at = now() "
                "WHERE id = $1",
                proposal_id,
            )

    async def test_a_resolved_proposal_is_immutable(self, superuser_conn):
        proposal_id = await _proposal(superuser_conn)
        await superuser_conn.execute(
            "UPDATE plan_driver_proposal SET state = 'Approved', resolved_by = 'bob', resolved_at = now() WHERE id = $1",
            proposal_id,
        )
        with pytest.raises(asyncpg.RaiseError, match="immutable"):
            await superuser_conn.execute("UPDATE plan_driver_proposal SET state = 'Rejected' WHERE id = $1", proposal_id)

    async def test_the_app_role_cannot_rewrite_a_proposed_formula(self, superuser_conn, app_conn):
        proposal_id = await _proposal(superuser_conn)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute("UPDATE plan_driver_proposal SET formula = '1' WHERE id = $1", proposal_id)
