"""Commitment Service contract tests: idempotency-key semantics and the
runtime-configurable failure rate that Phase 8's compensation saga has to
be correct against.

The service lives in services/commitment/ as its own standalone package (it
ships and deploys independently of fpa_be), so it's imported here by path
rather than as an installed dependency.
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_MAIN_PATH = Path(__file__).parent.parent.parent / "services" / "commitment" / "main.py"


def _load_app():
    spec = importlib.util.spec_from_file_location("commitment_service_main", _MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["commitment_service_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load_app()


@pytest.fixture
def client(module):
    return TestClient(module.app)


_BODY = {
    "plan_version_id": "11111111-1111-1111-1111-111111111111",
    "scenario_id": "base",
    "revision": 1,
    "amount": 12345.67,
}


class TestIdempotency:
    def test_same_key_twice_returns_first_result_and_creates_nothing_new(self, client, module):
        r1 = client.post("/commitments", json=_BODY, headers={"Idempotency-Key": "k1"})
        r2 = client.post("/commitments", json=_BODY, headers={"Idempotency-Key": "k1"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]
        assert len(module._commitments) == 1

    def test_no_key_creates_a_new_commitment_every_call(self, client, module):
        r1 = client.post("/commitments", json=_BODY)
        r2 = client.post("/commitments", json=_BODY)
        assert r1.json()["id"] != r2.json()["id"]
        assert len(module._commitments) == 2

    def test_different_keys_create_different_commitments(self, client, module):
        r1 = client.post("/commitments", json=_BODY, headers={"Idempotency-Key": "k1"})
        r2 = client.post("/commitments", json=_BODY, headers={"Idempotency-Key": "k2"})
        assert r1.json()["id"] != r2.json()["id"]


class TestDelete:
    def test_delete_reverses_a_known_commitment(self, client):
        created = client.post("/commitments", json=_BODY).json()
        r = client.delete(f"/commitments/{created['id']}")
        assert r.status_code == 204

    def test_delete_unknown_commitment_is_404(self, client):
        r = client.delete("/commitments/does-not-exist")
        assert r.status_code == 404


class TestFailureRate:
    def test_default_failure_rate_is_zero(self, client):
        assert client.get("/_config/failure-rate").json()["failure_rate"] == 0.0

    def test_setting_rate_to_one_forces_every_create_to_fail(self, client):
        client.post("/_config/failure-rate", json={"rate": 1.0})
        r = client.post("/commitments", json=_BODY)
        assert r.status_code == 500

    def test_setting_rate_to_one_forces_every_delete_to_fail(self, client):
        created = client.post("/commitments", json=_BODY).json()
        client.post("/_config/failure-rate", json={"rate": 1.0})
        r = client.delete(f"/commitments/{created['id']}")
        assert r.status_code == 500

    def test_rate_back_to_zero_succeeds_again(self, client):
        client.post("/_config/failure-rate", json={"rate": 1.0})
        client.post("/_config/failure-rate", json={"rate": 0.0})
        r = client.post("/commitments", json=_BODY)
        assert r.status_code == 201

    def test_rate_out_of_bounds_is_rejected(self, client):
        assert client.post("/_config/failure-rate", json={"rate": 1.5}).status_code == 400
        assert client.post("/_config/failure-rate", json={"rate": -0.1}).status_code == 400


class TestTimeoutMode:
    def test_a_timeout_stalls_and_then_the_write_still_lands(self, client, module):
        client.post("/_config/failure-rate", json={"rate": 1.0, "mode": "timeout", "timeout_seconds": 0.05})
        started = time.monotonic()
        r = client.post("/commitments", json=_BODY, headers={"Idempotency-Key": "k-timeout"})
        assert time.monotonic() - started >= 0.05
        assert r.status_code == 201
        assert len(module._commitments) == 1

    def test_config_reports_mode_and_timeout(self, client):
        client.post("/_config/failure-rate", json={"rate": 0.5, "mode": "timeout", "timeout_seconds": 2.0})
        assert client.get("/_config/failure-rate").json() == {
            "failure_rate": 0.5,
            "failure_mode": "timeout",
            "timeout_seconds": 2.0,
        }

    def test_unknown_mode_is_rejected(self, client):
        assert client.post("/_config/failure-rate", json={"rate": 1.0, "mode": "explode"}).status_code == 422


class TestLedgerRead:
    def test_lists_the_ledger_and_filters_by_plan_version(self, client):
        client.post("/commitments", json=_BODY)
        client.post("/commitments", json={**_BODY, "plan_version_id": "22222222-2222-2222-2222-222222222222"})
        assert len(client.get("/commitments").json()) == 2
        only = client.get("/commitments", params={"plan_version_id": _BODY["plan_version_id"]}).json()
        assert [c["plan_version_id"] for c in only] == [_BODY["plan_version_id"]]

    def test_reads_are_subject_to_failure_injection_too(self, client):
        client.post("/_config/failure-rate", json={"rate": 1.0})
        assert client.get("/commitments").status_code == 500


class TestHealth:
    def test_health_ok(self, client):
        assert client.get("/health").json() == {"status": "ok"}
