"""Vintage drift reconciliation against the live seeded cube: the two Poland
delivery-cost vintages must actually disagree (that's the whole point of the
seed's two-vintage design), and the structural `drift_flag` must reflect it
regardless of threshold."""

import json

from fpa_be.agents.drift import DEFAULT_DRIFT_THRESHOLD_PCT, check_vintage_drift, drift_result_to_json
from fpa_be.compiler.security import SecurityContext

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))


class TestCheckVintageDrift:
    def test_poland_delivery_cost_disagrees_between_vintages(self):
        result = check_vintage_drift("delivery_cost", "WHERE geo_country = 'PL' FOR PERIOD 2026-Q2", PL_SCOPE)
        assert result.vintage_a == 1
        assert result.vintage_b == 2
        assert result.total_a != result.total_b

    def test_drift_flag_set_when_delta_exceeds_threshold(self):
        result = check_vintage_drift(
            "delivery_cost", "WHERE geo_country = 'PL' FOR PERIOD 2026-Q2", PL_SCOPE, threshold_pct=0.0
        )
        assert result.drift_flag is True

    def test_drift_flag_clear_when_threshold_above_actual_delta(self):
        result = check_vintage_drift(
            "delivery_cost", "WHERE geo_country = 'PL' FOR PERIOD 2026-Q2", PL_SCOPE, threshold_pct=10_000.0
        )
        assert result.drift_flag is False

    def test_default_threshold_is_half_a_percent(self):
        assert DEFAULT_DRIFT_THRESHOLD_PCT == 0.5


class TestDriftResultToJson:
    def test_round_trips_all_fields(self):
        result = check_vintage_drift("delivery_cost", "WHERE geo_country = 'PL' FOR PERIOD 2026-Q2", PL_SCOPE)
        payload = json.loads(drift_result_to_json(result))
        assert payload["metric"] == "delivery_cost"
        assert payload["drift_flag"] == result.drift_flag
        assert payload["vintage_a"] == 1
        assert payload["vintage_b"] == 2
