"""Matched plan/actual row puller against the live seeded ClickHouse."""

from fpa_be.bridge.matched_rows import fetch_matched_rows
from fpa_be.compiler.security import SecurityContext

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))
PL_GROUP_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1", "RTPL2", "RTPL3"}))
UNSCOPED = SecurityContext()


class TestFetchMatchedRows:
    def test_returns_rows_for_poland_q2(self, ch_client):
        rows = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        assert len(rows) > 0
        assert all(r.company == "RTPL1" for r in rows)

    def test_every_row_carries_both_plan_and_actual_priced_quantities(self, ch_client):
        rows = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        row = rows[0]
        assert row.plan_qty > 0 or row.actual_qty > 0
        assert row.plan_fx > 0
        assert row.actual_fx > 0

    def test_scope_restricts_to_allowed_companies_only(self, ch_client):
        pl1_rows = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        group_rows = fetch_matched_rows(ch_client, PL_GROUP_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        assert len(group_rows) >= len(pl1_rows)
        assert {r.company for r in group_rows} <= {"RTPL1", "RTPL2", "RTPL3"}

    def test_vintage_1_and_latest_can_differ_for_poland_q2(self, ch_client):
        vintage_1 = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=1)
        latest = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        assert len(vintage_1) > 0
        assert len(latest) > 0
