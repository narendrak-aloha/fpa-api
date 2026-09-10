"""End-to-end exact-identity check on the live seeded Poland Q2 cut: matched
rows pulled straight from ClickHouse, decomposed, and required to tie to
zero (up to the stated tolerance) at multiple rollup nodes -- not just the
top of the tree.
"""

from fpa_be.bridge.decompose import cost_bridge, revenue_bridge
from fpa_be.bridge.matched_rows import fetch_matched_rows
from fpa_be.compiler.security import SecurityContext

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))
PL_GROUP_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1", "RTPL2", "RTPL3"}))


def _tol(line_count: int) -> float:
    return max(1.00, 0.01 * line_count)


class TestPolandQ2RevenueBridgeTiesToZero:
    def test_root_level_identity(self, ch_client):
        rows = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        revenue_rows = [r for r in rows if r.account.startswith("41")]
        result = revenue_bridge(revenue_rows)
        assert abs(result.residual) < _tol(len(revenue_rows))

    def test_every_leg_is_materially_non_zero(self, ch_client):
        rows = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        revenue_rows = [r for r in rows if r.account.startswith("41")]
        result = revenue_bridge(revenue_rows)
        for leg in result.legs:
            assert abs(leg.amount) > 1.00, f"leg {leg.name} is suspiciously zero: {leg.amount}"

    def test_identity_holds_per_practice_rollup_node(self, ch_client):
        rows = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        revenue_rows = [r for r in rows if r.account.startswith("41")]
        for practice in {r.practice for r in revenue_rows}:
            subset = [r for r in revenue_rows if r.practice == practice]
            result = revenue_bridge(subset)
            assert abs(result.residual) < _tol(len(subset))

    def test_identity_holds_across_a_wider_country_rollup(self, ch_client):
        rows = fetch_matched_rows(ch_client, PL_GROUP_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        revenue_rows = [r for r in rows if r.account.startswith("41")]
        result = revenue_bridge(revenue_rows)
        assert abs(result.residual) < _tol(len(revenue_rows))


class TestPolandQ2CostBridgeTiesToZero:
    def test_root_level_identity(self, ch_client):
        rows = fetch_matched_rows(ch_client, PL_SCOPE, "2026-04-01", "2026-06-30", resolved_vintage=None)
        cogs_rows = [r for r in rows if r.account.startswith("51")]
        result = cost_bridge(cogs_rows)
        assert abs(result.residual) < _tol(len(cogs_rows))
