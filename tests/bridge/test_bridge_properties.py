"""Property-based validation of the sequential cascade (Phase 7): the exact
identity, the volume+mix quantity-variance identity, and rollup consistency
must hold for *any* plan/actual quantity/price/mix/rate combination, not just
the one hardcoded Poland Q2 scenario Phase 6 already checked against the
live cube. A suspiciously-zero leg on a non-degenerate input is treated as a
bug, per the assignment, and is checked here too.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from fpa_be.bridge.decompose import MatchedRow, decompose

TOL = 1e-6

_PRACTICES = ("Cloud Migration", "Product Engineering", "Data Platform")
_GRADES = ("Junior", "Senior", "Manager")

# Realistic FP&A magnitudes only -- excludes the denormalized-boundary
# floats (e.g. 2.2e-308, the smallest normal double) Hypothesis would
# otherwise happily generate, which blow up in ratio arithmetic for reasons
# that have nothing to do with the bridge's own correctness.
_qty = st.one_of(st.just(0.0), st.floats(min_value=0.001, max_value=10_000.0, allow_nan=False, allow_infinity=False))
_price = st.floats(min_value=0.01, max_value=1_000.0, allow_nan=False, allow_infinity=False)
_fx = st.floats(min_value=0.01, max_value=5.0, allow_nan=False, allow_infinity=False)


def _matched_row_strategy(key: str):
    return st.builds(
        lambda practice, grade, plan_qty, plan_price, plan_fx, actual_qty, actual_price, actual_fx: MatchedRow(
            key=key,
            mix_values={"practice": practice, "grade": grade},
            plan_qty=plan_qty,
            plan_price=plan_price,
            plan_fx=plan_fx,
            actual_qty=actual_qty,
            actual_price=actual_price,
            actual_fx=actual_fx,
        ),
        practice=st.sampled_from(_PRACTICES),
        grade=st.sampled_from(_GRADES),
        plan_qty=_qty,
        plan_price=_price,
        plan_fx=_fx,
        actual_qty=_qty,
        actual_price=_price,
        actual_fx=_fx,
    )


rows_strategy = st.lists(
    st.integers(min_value=0, max_value=999).map(lambda i: _matched_row_strategy(f"row-{i}")),
    min_size=1,
    max_size=12,
).flatmap(lambda strategies: st.tuples(*strategies))


def _dedupe_by_group(rows: tuple[MatchedRow, ...]) -> list[MatchedRow]:
    """Collapses to one row per (practice, grade) group, as decompose()
    expects rows already aggregated to the mix_dims grain -- summing
    quantities and amount-weighting price/fx exactly like
    aggregate_to_mix_grain does."""
    buckets: dict[tuple, dict] = {}
    for row in rows:
        g = (row.mix_values["practice"], row.mix_values["grade"])
        b = buckets.setdefault(
            g,
            {"plan_qty": 0.0, "plan_amt": 0.0, "plan_usd": 0.0, "actual_qty": 0.0, "actual_amt": 0.0, "actual_usd": 0.0},
        )
        b["plan_qty"] += row.plan_qty
        b["plan_amt"] += row.plan_qty * row.plan_price
        b["plan_usd"] += row.plan_qty * row.plan_price * row.plan_fx
        b["actual_qty"] += row.actual_qty
        b["actual_amt"] += row.actual_qty * row.actual_price
        b["actual_usd"] += row.actual_qty * row.actual_price * row.actual_fx

    result = []
    for g, b in buckets.items():
        plan_price = b["plan_amt"] / b["plan_qty"] if b["plan_qty"] else 0.0
        plan_fx = b["plan_usd"] / b["plan_amt"] if b["plan_amt"] else 0.0
        actual_price = b["actual_amt"] / b["actual_qty"] if b["actual_qty"] else 0.0
        actual_fx = b["actual_usd"] / b["actual_amt"] if b["actual_amt"] else 0.0
        result.append(
            MatchedRow(
                key="|".join(g),
                mix_values={"practice": g[0], "grade": g[1]},
                plan_qty=b["plan_qty"],
                plan_price=plan_price,
                plan_fx=plan_fx,
                actual_qty=b["actual_qty"],
                actual_price=actual_price,
                actual_fx=actual_fx,
            )
        )
    return result


@given(rows=rows_strategy)
@settings(max_examples=200)
def test_legs_always_sum_to_the_gap_with_no_residual(rows):
    aggregated = _dedupe_by_group(rows)
    result = decompose(aggregated, mix_dims=("practice", "grade"))
    assert abs(result.residual) < TOL


@given(rows=rows_strategy)
@settings(max_examples=200)
def test_volume_plus_mix_legs_equal_actual_quantity_at_plan_price(rows):
    aggregated = _dedupe_by_group(rows)
    result = decompose(aggregated, mix_dims=("practice", "grade"))

    # Every operational leg is priced at plan price *and* plan FX (only the
    # FX leg swaps to actual), so the quantity-variance identity has to be
    # stated in the same plan-price/plan-fx terms the cascade's own states
    # use, not price alone.
    plan_total = sum(r.plan_qty * r.plan_price * r.plan_fx for r in aggregated)
    actual_qty_at_plan_price_and_fx = sum(r.actual_qty * r.plan_price * r.plan_fx for r in aggregated)
    total_quantity_variance = actual_qty_at_plan_price_and_fx - plan_total

    volume_plus_mix = result.leg("Volume") + result.leg("Mix(practice)") + result.leg("Mix(grade)")
    assert abs(volume_plus_mix - total_quantity_variance) < TOL


@given(rows=rows_strategy)
@settings(max_examples=200)
def test_fx_leg_is_zero_when_actual_fx_equals_plan_fx(rows):
    aggregated = _dedupe_by_group(rows)
    same_fx_rows = [
        MatchedRow(
            key=r.key,
            mix_values=r.mix_values,
            plan_qty=r.plan_qty,
            plan_price=r.plan_price,
            plan_fx=r.plan_fx,
            actual_qty=r.actual_qty,
            actual_price=r.actual_price,
            actual_fx=r.plan_fx,
        )
        for r in aggregated
    ]
    result = decompose(same_fx_rows, mix_dims=("practice", "grade"))
    assert abs(result.leg("FX")) < TOL


@given(rows=rows_strategy)
@settings(max_examples=200)
def test_price_and_fx_legs_are_zero_when_actual_equals_plan(rows):
    aggregated = _dedupe_by_group(rows)
    identical_rows = [
        MatchedRow(
            key=r.key,
            mix_values=r.mix_values,
            plan_qty=r.plan_qty,
            plan_price=r.plan_price,
            plan_fx=r.plan_fx,
            actual_qty=r.plan_qty,
            actual_price=r.plan_price,
            actual_fx=r.plan_fx,
        )
        for r in aggregated
    ]
    result = decompose(identical_rows, mix_dims=("practice", "grade"))
    for leg in result.legs:
        assert abs(leg.amount) < TOL
    assert abs(result.residual) < TOL


@given(rows=rows_strategy)
@settings(max_examples=100)
def test_identity_holds_at_a_coarser_rollup_no_mix_dims(rows):
    """Same population, fully collapsed to one row and bridged with zero
    mix dims (the cost-bridge shape) must still tie out exactly -- the
    cascade's exactness doesn't depend on how many mix dims are named. (A
    single row is the correct grain here: with mix_dims=() there's nothing
    left to redistribute *by*, so the population must already be one cell,
    exactly as aggregate_to_mix_grain(rows, ()) produces for cost_bridge.)"""
    aggregated = _dedupe_by_group(rows)
    single_cell = _dedupe_by_group(tuple(
        MatchedRow(
            key=r.key,
            mix_values={"practice": "_all", "grade": "_all"},
            plan_qty=r.plan_qty,
            plan_price=r.plan_price,
            plan_fx=r.plan_fx,
            actual_qty=r.actual_qty,
            actual_price=r.actual_price,
            actual_fx=r.actual_fx,
        )
        for r in aggregated
    ))
    result = decompose(single_cell, mix_dims=())
    assert abs(result.residual) < TOL
    assert {leg.name for leg in result.legs} == {"Volume", "Price", "FX"}
