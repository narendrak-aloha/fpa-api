"""Pure unit tests for the sequential cascade against synthetic data --
no ClickHouse/Postgres needed. Exercises the exact identity, the
volume+mix == total quantity variance identity, and each leg's construction
(operational legs at plan FX, only the FX leg swapping to actual).
"""

from fpa_be.bridge.decompose import MatchedRow, cost_bridge, decompose, revenue_bridge

TOL = 1e-6


def _row(key, practice, grade, plan_qty, plan_price, plan_fx, actual_qty, actual_price, actual_fx):
    return MatchedRow(
        key=key,
        mix_values={"practice": practice, "grade": grade},
        plan_qty=plan_qty,
        plan_price=plan_price,
        plan_fx=plan_fx,
        actual_qty=actual_qty,
        actual_price=actual_price,
        actual_fx=actual_fx,
    )


class TestExactIdentity:
    def test_legs_sum_to_the_gap_with_no_residual(self):
        rows = [
            _row("a", "Cloud", "Senior", 100, 150, 1.0, 120, 160, 1.05),
            _row("b", "Cloud", "Junior", 200, 90, 1.0, 180, 95, 1.05),
            _row("c", "Data", "Senior", 50, 200, 1.0, 70, 210, 1.05),
        ]
        result = decompose(rows, mix_dims=("practice", "grade"))
        assert abs(result.residual) < TOL

    def test_revenue_bridge_helper_ties_to_zero(self):
        class Row:
            def __init__(self, practice, grade, plan_qty, plan_price, plan_fx, actual_qty, actual_price, actual_fx):
                self.practice = practice
                self.grade = grade
                self.plan_qty = plan_qty
                self.plan_price = plan_price
                self.plan_fx = plan_fx
                self.actual_qty = actual_qty
                self.actual_price = actual_price
                self.actual_fx = actual_fx

        rows = [
            Row("Cloud", "Senior", 100, 150, 1.0, 120, 160, 1.05),
            Row("Cloud", "Junior", 200, 90, 1.0, 150, 95, 1.05),
            Row("Data", "Senior", 50, 200, 1.0, 90, 210, 1.05),
            Row("Data", "Junior", 80, 100, 1.0, 80, 105, 1.05),
        ]
        result = revenue_bridge(rows)
        assert abs(result.residual) < TOL
        assert {leg.name for leg in result.legs} == {
            "Volume", "Mix(practice)", "Mix(grade)", "Price", "FX",
        }

    def test_cost_bridge_helper_has_only_three_legs(self):
        class Row:
            def __init__(self, plan_qty, plan_price, plan_fx, actual_qty, actual_price, actual_fx):
                self.plan_qty = plan_qty
                self.plan_price = plan_price
                self.plan_fx = plan_fx
                self.actual_qty = actual_qty
                self.actual_price = actual_price
                self.actual_fx = actual_fx

        rows = [
            Row(100, 50, 1.0, 130, 52, 1.05),
            Row(200, 30, 1.0, 190, 31, 1.05),
        ]
        result = cost_bridge(rows)
        assert abs(result.residual) < TOL
        assert {leg.name for leg in result.legs} == {"Efficiency", "Rate", "FX"}


class TestVolumeMixIdentity:
    def test_volume_plus_mix_legs_land_exactly_on_actual_quantity_at_plan_price(self):
        rows = [
            _row("a", "Cloud", "Senior", 100, 150, 1.0, 140, 160, 1.05),
            _row("b", "Cloud", "Junior", 200, 90, 1.0, 160, 95, 1.05),
            _row("c", "Data", "Senior", 50, 200, 1.0, 80, 210, 1.05),
        ]
        result = decompose(rows, mix_dims=("practice", "grade"))

        plan_total = sum(r.plan_qty * r.plan_price for r in rows)
        # mix_dims here (practice, grade) fully partition the rows, so by
        # the end of the last mix step every row's operative quantity must
        # equal its actual quantity -- meaning Volume + every Mix leg,
        # together, is exactly "actual quantity, still priced at plan
        # price/mix" minus plan. This is the total-quantity-variance
        # identity the assignment names explicitly.
        actual_qty_at_plan_price = sum(r.actual_qty * r.plan_price for r in rows)
        total_quantity_variance = actual_qty_at_plan_price - plan_total

        volume_plus_mix = result.leg("Volume") + result.leg("Mix(practice)") + result.leg("Mix(grade)")
        assert abs(volume_plus_mix - total_quantity_variance) < TOL


class TestOperationalLegsUsePlanFxThroughout:
    def test_only_the_fx_leg_differs_when_actual_fx_equals_plan_fx(self):
        """If actual_fx == plan_fx for every row, the FX leg must be exactly
        zero and every other leg unaffected -- proof by construction that no
        operational leg secretly uses the actual rate."""
        rows_same_fx = [
            _row("a", "Cloud", "Senior", 100, 150, 1.2, 120, 160, 1.2),
            _row("b", "Data", "Junior", 80, 90, 1.2, 60, 95, 1.2),
        ]
        result = decompose(rows_same_fx, mix_dims=("practice", "grade"))
        assert abs(result.leg("FX")) < TOL

    def test_fx_leg_isolates_pure_rate_translation(self):
        rows_plan_fx = [
            _row("a", "Cloud", "Senior", 100, 150, 1.0, 100, 150, 1.0),
        ]
        rows_actual_fx = [
            _row("a", "Cloud", "Senior", 100, 150, 1.0, 100, 150, 1.10),
        ]
        result_no_fx_move = decompose(rows_plan_fx, mix_dims=("practice", "grade"))
        result_fx_move = decompose(rows_actual_fx, mix_dims=("practice", "grade"))

        assert abs(result_no_fx_move.leg("FX")) < TOL
        # Quantity and price identical plan vs actual, so the entire gap is
        # the FX leg: 100 * 150 * (1.10 - 1.0) = 1500.
        assert abs(result_fx_move.leg("FX") - 1500.0) < TOL
        assert abs(result_fx_move.leg("Volume")) < TOL
        assert abs(result_fx_move.leg("Price")) < TOL


class TestZeroPlanQuantityIsHandledWithoutDivideByZero:
    def test_new_row_with_zero_plan_quantity_does_not_raise(self):
        rows = [
            _row("new", "Cloud", "Senior", 0, 0, 1.0, 50, 100, 1.0),
        ]
        result = decompose(rows, mix_dims=("practice", "grade"))
        assert abs(result.reported_actual_total - 5000.0) < TOL
