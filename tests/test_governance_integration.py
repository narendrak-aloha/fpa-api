"""The plan spine against the real governance store.

Every guarantee here is exercised the way a grader would: through the
functions the API calls, and where it matters, through a raw connection that
pretends to be somebody with a database URL.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from db.config import database_url

pytestmark = pytest.mark.integration

try:
    create_engine(database_url(), connect_args={"connect_timeout": 2}).connect().close()
except Exception:  # noqa: BLE001
    pytest.skip("needs Postgres: run `make docker-local-run-d`", allow_module_level=True)

from fpa_project.governance import (  # noqa: E402
    Refused, authenticate, create_plan_version, describe, save_driver, save_planning_model, set_covenant,
    set_plan_fx_rate, transition, verify_audit_chain,
)


@pytest.fixture
def raw():
    return create_engine(database_url())


@pytest.fixture
def version(raw):
    code = f"PV-T-{uuid.uuid4().hex[:6]}"
    create_plan_version(code, "FPA-2026", 2026, "u-planner")
    yield code
    with raw.begin() as conn:
        conn.execute(text("ALTER TABLE fpa_governance.plan_version DISABLE TRIGGER plan_version_lock_guard"))
        conn.execute(text("DELETE FROM fpa_governance.plan_version WHERE plan_version_code = :c"), {"c": code})
        conn.execute(text("ALTER TABLE fpa_governance.plan_version ENABLE TRIGGER plan_version_lock_guard"))


# ---------------------------------------------------------------------------
# Author, review, approve by a second user, lock
# ---------------------------------------------------------------------------
def test_the_full_lifecycle_and_who_may_do_each_step(version):
    assert describe(version)["state"] == "DRAFT"
    with pytest.raises(Refused, match="not a declared transition"):
        transition(version, "APPROVED", "u-controller")
    transition(version, "IN_REVIEW", "u-planner")
    with pytest.raises(Refused, match="needs one of: controller"):
        transition(version, "APPROVED", "u-planner")
    set_covenant(version, True, "covenant reviewed", "u-controller")
    transition(version, "APPROVED", "u-controller", note="covenant reviewed")
    with pytest.raises(Refused, match="needs one of: cfo"):
        transition(version, "LOCKED", "u-controller")
    transition(version, "LOCKED", "u-cfo")
    assert describe(version)["state"] == "LOCKED"


def test_self_approval_is_refused_in_the_application_and_in_the_database(raw, version):
    transition(version, "IN_REVIEW", "u-planner")
    # u-planner is not a controller, so add the role for this test and take it away again:
    # the segregation rule has to hold even for someone who holds the role.
    with raw.begin() as conn:
        conn.execute(text("INSERT INTO fpa_governance.user_role VALUES ('u-planner', 'controller') ON CONFLICT DO NOTHING"))
    try:
        with pytest.raises(Refused, match="segregation of duties"):
            transition(version, "APPROVED", "u-planner")
        # Straight at the table, as the requester: the check constraint says no.
        with raw.begin() as conn, pytest.raises(Exception, match="no_self_approval"):
            conn.execute(text("SELECT set_config('fpa.actor', 'u-planner', true)"))
            conn.execute(text("UPDATE fpa_governance.plan_version SET state = 'APPROVED', approved_by = 'u-planner', covenant_ok = true WHERE plan_version_code = :c"), {"c": version})
    finally:
        with raw.begin() as conn:
            conn.execute(text("DELETE FROM fpa_governance.user_role WHERE user_id = 'u-planner' AND role_code = 'controller'"))


def test_a_covenant_breach_blocks_approval_whatever_the_role(raw, version):
    transition(version, "IN_REVIEW", "u-planner")
    set_covenant(version, False, "recorded breach", "u-controller")
    for actor in ("u-controller", "u-cfo"):
        with pytest.raises(Refused, match="covenant_before_approval"):
            transition(version, "APPROVED", actor)
    assert describe(version)["covenant_ok"] is False
    assert describe(version)["covenant_note"] == "recorded breach"
    with raw.begin() as conn, pytest.raises(Exception, match="covenant_before_approval"):
        conn.execute(text("SELECT set_config('fpa.actor', 'u-cfo', true)"))
        conn.execute(text("UPDATE fpa_governance.plan_version SET state = 'APPROVED', approved_by = 'u-cfo', covenant_ok = false WHERE plan_version_code = :c"), {"c": version})


def test_locked_means_locked_including_from_a_direct_connection(raw, version):
    transition(version, "IN_REVIEW", "u-planner")
    set_covenant(version, True, "reviewed", "u-controller")
    transition(version, "APPROVED", "u-controller")
    transition(version, "LOCKED", "u-cfo")
    with pytest.raises(Refused, match="not a declared transition"):
        transition(version, "DRAFT", "u-cfo")
    # psql, effectively: no API, superuser connection, plain UPDATE.
    with raw.begin() as conn, pytest.raises(Exception, match="plan version is locked"):
        conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_note = 'edited' WHERE plan_version_code = :c"), {"c": version})
    with raw.begin() as conn, pytest.raises(Exception, match="plan version is locked"):
        conn.execute(text("DELETE FROM fpa_governance.plan_version WHERE plan_version_code = :c"), {"c": version})


def test_workflow_approval_preserves_covenant_verdict_and_retries_safely(version):
    from temporalio.exceptions import ApplicationError
    from fpa_project.recompute.activities import record_approval

    transition(version, "IN_REVIEW", "u-planner")
    set_covenant(version, False, "breach", "u-controller")
    target = describe(version)["plan_version_id"]
    with pytest.raises(ApplicationError, match="covenant review must pass"):
        record_approval(target, "u-cfo", "approve", "test-covenant")
    assert describe(version)["state"] == "IN_REVIEW"
    assert describe(version)["covenant_note"] == "breach"
    set_covenant(version, True, "review complete", "u-controller")
    record_approval(target, "u-cfo", "approve", "test-covenant")
    first = describe(version)
    record_approval(target, "u-cfo", "approve", "test-covenant")
    assert describe(version) == first
    assert first["state"] == "LOCKED"
    assert first["covenant_note"] == "review complete"


def test_a_line_without_a_derivation_trace_cannot_be_saved(raw, version):
    pv = describe(version)["plan_version_id"]
    with raw.begin() as conn, pytest.raises(Exception, match="derivation_trace"):
        conn.execute(
            text(
                "INSERT INTO fpa_governance.plan_version_line (plan_version_id, scenario_code, company_code, period_month, "
                "account_code, dim_signature_hash, quantity, unit_price, amount_functional, functional_currency, driver_derivation_trace) "
                "VALUES (CAST(:pv AS uuid), 'base', 'RTPL1', '2026-04-01', '41000', '0123456789abcdef', 1, 2, 2, 'PLN', '{}'::jsonb)"
            ),
            {"pv": pv},
        )


# ---------------------------------------------------------------------------
# Field-level permission and concurrent edits
# ---------------------------------------------------------------------------
def test_covenant_and_fx_rate_are_controller_only_fields(version):
    with pytest.raises(Refused, match="controller"):
        set_covenant(version, True, "planner trying", "u-planner")
    assert set_covenant(version, True, "reviewed", "u-controller")["covenant_ok"] is True
    with pytest.raises(Refused, match="controller"):
        set_plan_fx_rate("PV-2026-0001", "2026-01-01", "PLN", "0.25", "u-analyst-pl")
    with pytest.raises(Refused, match="controller"):
        set_plan_fx_rate("PV-2026-0001", "2026-01-01", "PLN", "0.25", "u-planner")


def test_a_direct_write_to_a_controller_field_without_an_actor_is_refused(raw, version):
    with raw.begin() as conn, pytest.raises(Exception, match="fpa.actor is not set"):
        conn.execute(text("UPDATE fpa_governance.plan_version SET covenant_ok = true WHERE plan_version_code = :c"), {"c": version})


def test_two_writers_on_one_version_one_wins_one_is_told_it_lost(version):
    first_read = describe(version)["row_version"]
    transition(version, "IN_REVIEW", "u-planner", expected_version=first_read)
    with pytest.raises(Refused, match="lost the race"):
        transition(version, "DRAFT", "u-planner", expected_version=first_read)
    assert describe(version)["row_version"] == first_read + 1


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------
def test_the_chain_verifies_and_a_hand_edit_makes_it_fail(raw, version):
    transition(version, "IN_REVIEW", "u-planner")
    assert verify_audit_chain().ok
    with raw.begin() as conn:
        target = conn.execute(text("SELECT audit_event_id FROM fpa_governance.audit_event WHERE entity_id = :c ORDER BY audit_event_id LIMIT 1"), {"c": version}).scalar()
        # The append-only trigger refuses the edit outright...
        with pytest.raises(Exception, match="append-only"):
            with conn.begin_nested():
                conn.execute(text("UPDATE fpa_governance.audit_event SET action = 'STATE_LOCKED' WHERE audit_event_id = :i"), {"i": target})
        # ...so the hand edit needs the trigger off. That is what a superuser
        # with a URL can do, and the verifier is what catches them.
        conn.execute(text("ALTER TABLE fpa_governance.audit_event DISABLE TRIGGER audit_event_no_update"))
        original = conn.execute(text("SELECT action FROM fpa_governance.audit_event WHERE audit_event_id = :i"), {"i": target}).scalar()
        conn.execute(text("UPDATE fpa_governance.audit_event SET action = 'STATE_LOCKED' WHERE audit_event_id = :i"), {"i": target})
    try:
        verdict = verify_audit_chain()
        assert not verdict.ok
        assert verdict.broken_at == target
        assert "altered" in verdict.reason
    finally:
        with raw.begin() as conn:
            conn.execute(text("UPDATE fpa_governance.audit_event SET action = :a WHERE audit_event_id = :i"), {"a": original, "i": target})
            conn.execute(text("ALTER TABLE fpa_governance.audit_event ENABLE TRIGGER audit_event_no_update"))
    assert verify_audit_chain().ok


def test_the_same_event_appended_twice_lands_once(raw):
    from fpa_project.governance import engine, record

    payload = {"probe": uuid.uuid4().hex}
    with engine().begin() as conn:
        record(conn, "u-planner", "probe", "x", "TWICE", payload)
        record(conn, "u-planner", "probe", "x", "TWICE", payload)
    with raw.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM fpa_governance.audit_event WHERE entity_type = 'probe' AND payload->>'probe' = :p"), {"p": payload["probe"]}).scalar()
    assert n == 1


# ---------------------------------------------------------------------------
# Validation at the point of authoring
# ---------------------------------------------------------------------------
def test_a_malformed_formula_gets_a_specific_error_and_saves_nothing():
    with pytest.raises(Refused, match="position"):
        save_driver("FPA-2026", "broken", "Broken", "heads * (", "u-controller")


def test_an_unknown_reference_is_named():
    with pytest.raises(Refused, match="unknown formula reference: secret_value"):
        save_driver("FPA-2026", "leaky", "Leaky", "heads * secret_value", "u-controller")


def test_a_cyclic_model_is_refused_with_its_path():
    with pytest.raises(Refused, match="formula cycle detected: a -> b -> a"):
        save_planning_model("FPA-2026", {"a": "b + 1", "b": "a * 2"}, "u-controller")


def test_the_seeded_model_saves_and_its_dag_is_derived():
    from fpa_project.governance import list_drivers

    formulas = {d["driver_code"]: d["formula"] for d in list_drivers("FPA-2026")}
    result = save_planning_model("FPA-2026", formulas, "u-controller")
    by_driver = {n["driver"]: n["depends_on"] for n in result["calc_order_dag"]}
    assert by_driver["heads"] == ["attrition"]       # PRIOR(heads) is not an edge
    assert by_driver["billable_hours"] == ["available_hours", "utilisation"]


# ---------------------------------------------------------------------------
# Tokens and scope
# ---------------------------------------------------------------------------
def test_tokens_resolve_to_roles_and_entity_scope():
    analyst = authenticate("tok-analyst-pl")
    assert analyst.roles == {"analyst"} and analyst.companies == {"RTPL1", "RTPL2", "RTPL3"} and analyst.is_human
    cfo = authenticate("tok-cfo")
    assert {"cfo", "controller"} <= cfo.roles and len(cfo.companies) == 20
    with pytest.raises(Refused, match="unknown"):
        authenticate("tok-nobody")
    with pytest.raises(Refused, match="no bearer"):
        authenticate("")
