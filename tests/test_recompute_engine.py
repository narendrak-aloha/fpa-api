"""The recompute arithmetic, tested without a database.

Everything in ``engine.py`` is a pure function, which is what makes these
tests cheap and what lets the workflow call them directly without breaking
determinism.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fpa_project.recompute import engine as calc
from fpa_project.recompute.models import AccountFactor, DriverBinding, DriverShock

# The seeded model's DAG, in the shape planning_model.calc_order_dag stores it.
DAG = [
    {"driver": "heads", "depends_on": []},
    {"driver": "available_hours", "depends_on": ["heads"]},
    {"driver": "utilisation", "depends_on": ["available_hours"]},
    {"driver": "bill_rate", "depends_on": []},
    {"driver": "realisation", "depends_on": ["bill_rate"]},
    {"driver": "attach_rate", "depends_on": ["heads"]},
]


class TestDependents:
    def test_walks_transitively(self):
        # heads -> available_hours -> utilisation, and heads -> attach_rate.
        assert calc.dependents(DAG, "heads") == ["available_hours", "utilisation", "attach_rate"]

    def test_a_leaf_has_none(self):
        # Nothing depends on utilisation, so shocking it moves only its own lines.
        assert calc.dependents(DAG, "utilisation") == []

    def test_unknown_driver_is_not_an_error_here(self):
        # Validation belongs in the snapshot activity, which can see the model.
        assert calc.dependents(DAG, "nonsense") == []

    def test_order_follows_the_declared_calc_order(self):
        # The result ends up in workflow history, so it has to be stable, and
        # it follows calc_order_dag's own order rather than discovery order:
        # attach_rate is reached in one hop but declared last, so it comes last.
        assert calc.dependents(DAG, "heads") == ["available_hours", "utilisation", "attach_rate"]
        assert calc.dependents(DAG, "heads") == calc.dependents(DAG, "heads")


class TestDirtyDrivers:
    def test_shocked_driver_carries_its_own_ratio(self):
        shock = DriverShock("utilisation", 0.75, 0.70)
        ratios = calc.dirty_drivers(DAG, [shock])
        assert ratios == {"utilisation": pytest.approx(0.70 / 0.75)}

    def test_downstream_drivers_inherit_the_ratio(self):
        ratios = calc.dirty_drivers(DAG, [DriverShock("heads", 100, 110)])
        assert set(ratios) == {"heads", "available_hours", "utilisation", "attach_rate"}
        assert all(value == pytest.approx(1.1) for value in ratios.values())

    def test_two_shocks_reaching_one_driver_compose(self):
        # Both moves are real; the driver they meet at sees both.
        ratios = calc.dirty_drivers(DAG, [DriverShock("heads", 100, 110), DriverShock("utilisation", 0.8, 0.6)])
        assert ratios["utilisation"] == pytest.approx(1.1 * 0.75)


class TestAccountFactors:
    def test_elasticity_damps_the_move(self):
        factors = calc.account_factors(
            {"utilisation": 0.8},
            [DriverBinding("utilisation", "41000", "quantity", 0.5)],
        )
        # A 20% fall, half absorbed, is a 10% fall.
        assert factors[0].factor == pytest.approx(0.9)

    def test_elasticity_one_is_proportional(self):
        factors = calc.account_factors(
            {"utilisation": 0.8},
            [DriverBinding("utilisation", "41000", "quantity", 1.0)],
        )
        assert factors[0].factor == pytest.approx(0.8)

    def test_elasticity_zero_means_no_response(self):
        factors = calc.account_factors(
            {"utilisation": 0.5},
            [DriverBinding("utilisation", "63100", "quantity", 0.0)],
        )
        assert factors[0].factor == pytest.approx(1.0)

    def test_bindings_for_clean_drivers_are_ignored(self):
        factors = calc.account_factors(
            {"utilisation": 0.8},
            [DriverBinding("bill_rate", "41000", "unit_price", 1.0)],
        )
        assert factors == []

    def test_two_drivers_on_one_account_compose(self):
        factors = calc.account_factors(
            {"heads": 1.1, "utilisation": 0.9},
            [
                DriverBinding("heads", "51000", "quantity", 1.0),
                DriverBinding("utilisation", "51000", "quantity", 1.0),
            ],
        )
        assert len(factors) == 1
        assert factors[0].factor == pytest.approx(1.1 * 0.9)

    def test_output_is_sorted(self):
        factors = calc.account_factors(
            {"heads": 1.1},
            [
                DriverBinding("heads", "63100", "quantity", 1.0),
                DriverBinding("heads", "51000", "unit_price", 1.0),
                DriverBinding("heads", "51000", "quantity", 1.0),
            ],
        )
        assert [(f.account_code, f.target) for f in factors] == [
            ("51000", "quantity"), ("51000", "unit_price"), ("63100", "quantity"),
        ]


class TestRecomputeLine:
    def test_quantity_factor_applies_to_quantity_only(self):
        factors = [AccountFactor("41000", "quantity", 0.9)]
        quantity, price, amount = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity == Decimal("90.000000")
        assert price == Decimal("200.000000")
        assert amount == Decimal("18000.00")

    def test_price_factor_applies_to_price_only(self):
        factors = [AccountFactor("41000", "unit_price", 1.05)]
        quantity, price, amount = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity == Decimal("100.000000")
        assert price == Decimal("210.000000")

    def test_another_accounts_factor_is_not_applied(self):
        factors = [AccountFactor("51000", "quantity", 0.5)]
        quantity, _, _ = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity == Decimal("100.000000")

    def test_amount_ties_to_the_rounded_inputs(self):
        # plan_version_line has a check constraint that amount_functional
        # equals round(quantity * unit_price, 2). Rounding the inputs first is
        # what makes that hold; multiplying the raw floats does not.
        factors = [AccountFactor("41000", "quantity", 0.9333333333)]
        quantity, price, amount = calc.recompute_line(133.337, 187.774, factors, "41000")
        assert amount == (quantity * price).quantize(Decimal("0.01"))

    def test_precision_matches_the_columns(self):
        factors = [AccountFactor("41000", "quantity", 1.0 / 3.0)]
        quantity, price, _ = calc.recompute_line(100.0, 200.0, factors, "41000")
        assert quantity.as_tuple().exponent == -6
        assert price.as_tuple().exponent == -6

    def test_no_factors_leaves_the_line_alone(self):
        quantity, price, amount = calc.recompute_line(12.5, 4.0, [], "41000")
        assert (quantity, price, amount) == (Decimal("12.500000"), Decimal("4.000000"), Decimal("50.00"))

    def test_is_a_pure_function_of_its_inputs(self):
        # The idempotence guarantee rests on this: same baseline, same shock,
        # same numbers, however many times it runs.
        factors = [AccountFactor("41000", "quantity", 0.9333)]
        first = calc.recompute_line(133.337, 187.774, factors, "41000")
        second = calc.recompute_line(133.337, 187.774, factors, "41000")
        assert first == second


class TestDerivationTrace:
    def test_records_the_factors_that_were_applied(self):
        factors = [AccountFactor("41000", "quantity", 0.9), AccountFactor("51000", "quantity", 0.5)]
        trace = calc.derivation_trace("41000", factors, {"utilisation": {"ratio": 0.9}})
        assert trace["applied_factors"] == {"quantity": 0.9}
        assert trace["drivers"] == {"utilisation": {"ratio": 0.9}}

    def test_is_never_empty(self):
        # The column has a check constraint refusing '{}'::jsonb, which is the
        # schema insisting a line can always say where it came from.
        assert calc.derivation_trace("41000", [], {}) != {}


class TestPartitionPlan:
    def test_groups_months_up_to_the_target(self):
        counts = {("base", f"2026-{m:02d}-01"): 100 for m in range(1, 13)}
        months = sorted({month for _, month in counts})
        plan = calc.partition_plan(["base"], months, counts, target_size=350)
        assert [rows for _, _, rows in plan] == [300, 300, 300, 300]

    def test_a_month_bigger_than_the_target_is_its_own_partition(self):
        # Splitting inside a month would let two children write the same row.
        counts = {("base", "2026-01-01"): 9_000, ("base", "2026-02-01"): 100}
        plan = calc.partition_plan(["base"], ["2026-01-01", "2026-02-01"], counts, target_size=5_000)
        assert plan == [("base", ["2026-01-01"], 9_000), ("base", ["2026-02-01"], 100)]

    def test_empty_months_are_skipped(self):
        counts = {("base", "2026-01-01"): 10, ("base", "2026-03-01"): 10}
        plan = calc.partition_plan(["base"], ["2026-01-01", "2026-02-01", "2026-03-01"], counts, 5_000)
        assert plan == [("base", ["2026-01-01", "2026-03-01"], 20)]

    def test_scenarios_never_share_a_partition(self):
        counts = {("base", "2026-01-01"): 10, ("stretch", "2026-01-01"): 10}
        plan = calc.partition_plan(["base", "stretch"], ["2026-01-01"], counts, 5_000)
        assert [scenario for scenario, _, _ in plan] == ["base", "stretch"]

    def test_no_dirty_rows_means_no_partitions(self):
        assert calc.partition_plan(["base"], ["2026-01-01"], {}, 5_000) == []


class TestMergeShocks:
    def test_adds_new_drivers_and_keeps_published_ones(self):
        merged = calc.merge_shocks([DriverShock("utilisation", 0.75, 0.70)], [DriverShock("heads", 100, 110)])
        assert [(s.driver_code, s.from_value, s.to_value) for s in merged] == [("heads", 100, 110), ("utilisation", 0.75, 0.70)]

    def test_a_driver_moved_again_keeps_its_baseline(self):
        merged = calc.merge_shocks([DriverShock("utilisation", 0.75, 0.70)], [DriverShock("utilisation", 0.70, 0.65)])
        assert [(s.from_value, s.to_value) for s in merged] == [(0.75, 0.65)]

    def test_a_move_back_to_baseline_stays_as_a_ratio_of_one(self):
        merged = calc.merge_shocks([DriverShock("utilisation", 0.75, 0.70)], [DriverShock("utilisation", 0.70, 0.75)])
        assert merged[0].ratio() == 1.0

    def test_merging_is_idempotent_and_ordered(self):
        once = calc.merge_shocks([DriverShock("b", 1, 2)], [DriverShock("a", 1, 3)])
        assert calc.merge_shocks(once, once) == once
        assert [s.driver_code for s in once] == ["a", "b"]
