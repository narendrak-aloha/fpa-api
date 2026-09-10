"""The variance bridge: a sequential (chain-substitution) waterfall from a
matched plan/actual population to a set of named legs that sum, exactly, to
the total gap -- PLAN -> VOLUME -> MIX(dim1) -> MIX(dim2) -> ... -> PRICE ->
OPERATING ACTUAL -> FX -> REPORTED ACTUAL, per the assignment's own diagram.

The core idea is a cascade over an "operative quantity" that starts equal to
plan quantity and is progressively reallocated towards actual quantity, one
grouping dimension at a time:

  - VOLUME scales every row's operative quantity uniformly so the
    population's total matches actual's total -- mix (the relative shares
    across every dimension) is untouched, so this leg isolates pure scale.
  - Each MIX(dim) step re-groups by every mix dimension consumed so far
    (dim1, then dim1+dim2, ...) and redistributes each group's *already
    established* operative total according to that group's ACTUAL share of
    it, holding the *finer* shares (dims not yet consumed, plus anything not
    modeled at all) at whatever they were the step before. This is why
    matched rows are pre-aggregated up to the mix-dims' own grain (see
    aggregate_to_mix_grain) before decomposition -- any dimension *not*
    named as a mix dim has its own mix shift folded into the PRICE leg
    instead of leaking out as an unexplained residual, which is a deliberate
    simplification: only the dimensions the caller names are bridged
    explicitly.
  - PRICE swaps in actual price at the final (== actual) operative
    quantity, still at plan FX -- this state is "operating actual".
  - FX swaps in actual FX -- this state is "reported actual", which is
    exactly the raw actual total by construction (telescoping sum), so
    residual is rounding noise only, never a structural gap.

Every leg is `new_state - prior_state`, so `sum(legs) == reported_actual -
plan` exactly, up to floating-point noise -- see BridgeResult.residual.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MatchedRow:
    """One matched plan/actual key, already collapsed to whatever grain the
    caller wants to bridge over (see aggregate_to_mix_grain). mix_values is
    keyed by mix dimension name (e.g. {"practice": "Cloud Migration",
    "grade": "Senior"}); every row must carry the same set of keys.
    """

    key: str
    mix_values: dict[str, str]
    plan_qty: float
    plan_price: float
    plan_fx: float
    actual_qty: float
    actual_price: float
    actual_fx: float


@dataclass(frozen=True)
class BridgeLeg:
    name: str
    amount: float


@dataclass(frozen=True)
class BridgeResult:
    plan_total: float
    reported_actual_total: float
    legs: tuple[BridgeLeg, ...]
    residual: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "residual",
            (self.reported_actual_total - self.plan_total) - sum(leg.amount for leg in self.legs),
        )

    def leg(self, name: str) -> float:
        for leg in self.legs:
            if leg.name == name:
                return leg.amount
        raise KeyError(name)


def aggregate_to_mix_grain(rows: list, mix_dims: tuple[str, ...]) -> list[MatchedRow]:
    """Collapses raw matched fact rows (one per company/period/account/
    dim_signature_hash) up to the grain the bridge will actually cascade
    over -- exactly the mix_dims requested. Everything finer is summed away;
    its mix shift shows up inside the PRICE leg, not as a separate leg.

    Each input row must expose: plan_qty, plan_price, plan_fx, actual_qty,
    actual_price, actual_fx, and one attribute per name in mix_dims.
    """
    buckets: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(getattr(row, dim) for dim in mix_dims)
        b = buckets.setdefault(
            key,
            {
                "plan_qty": 0.0, "plan_functional_amt": 0.0, "plan_usd_amt": 0.0,
                "actual_qty": 0.0, "actual_functional_amt": 0.0, "actual_usd_amt": 0.0,
            },
        )
        b["plan_qty"] += row.plan_qty
        b["plan_functional_amt"] += row.plan_qty * row.plan_price
        b["plan_usd_amt"] += row.plan_qty * row.plan_price * row.plan_fx
        b["actual_qty"] += row.actual_qty
        b["actual_functional_amt"] += row.actual_qty * row.actual_price
        b["actual_usd_amt"] += row.actual_qty * row.actual_price * row.actual_fx

    result = []
    for key, b in buckets.items():
        plan_price = b["plan_functional_amt"] / b["plan_qty"] if b["plan_qty"] else 0.0
        plan_fx = b["plan_usd_amt"] / b["plan_functional_amt"] if b["plan_functional_amt"] else 0.0
        actual_price = b["actual_functional_amt"] / b["actual_qty"] if b["actual_qty"] else 0.0
        actual_fx = b["actual_usd_amt"] / b["actual_functional_amt"] if b["actual_functional_amt"] else 0.0
        result.append(
            MatchedRow(
                key="|".join(key),
                mix_values=dict(zip(mix_dims, key, strict=True)),
                plan_qty=b["plan_qty"],
                plan_price=plan_price,
                plan_fx=plan_fx,
                actual_qty=b["actual_qty"],
                actual_price=actual_price,
                actual_fx=actual_fx,
            )
        )
    return result


def _state(rows: list[MatchedRow], qty: dict[str, float], price: str, fx: str) -> float:
    total = 0.0
    for row in rows:
        p = row.plan_price if price == "plan" else row.actual_price
        x = row.plan_fx if fx == "plan" else row.actual_fx
        total += qty[row.key] * p * x
    return total


def decompose(
    rows: list[MatchedRow],
    mix_dims: tuple[str, ...],
    volume_leg_name: str = "Volume",
    price_leg_name: str = "Price",
    fx_leg_name: str = "FX",
) -> BridgeResult:
    """Runs the PLAN -> VOLUME -> MIX(dim)* -> PRICE -> FX cascade. rows must
    already be aggregated to exactly the mix_dims grain (see
    aggregate_to_mix_grain) -- mixing in a finer grain silently folds that
    finer mix into PRICE instead of raising, since that's the documented,
    intended behavior for any dimension not named in mix_dims.
    """
    plan_qty_total = sum(r.plan_qty for r in rows)
    actual_qty_total = sum(r.actual_qty for r in rows)
    plan_total = _state(rows, {r.key: r.plan_qty for r in rows}, "plan", "plan")

    legs: list[BridgeLeg] = []
    qty = {r.key: r.plan_qty for r in rows}
    prior_state = plan_total

    # VOLUME: uniform scale, mix untouched. When the whole population has
    # zero plan quantity (e.g. every row is a brand-new line with no plan
    # basis at all), there's no ratio to scale by -- fall back to splitting
    # the actual total evenly, same rationale as the mix-step fallback
    # below, so the cascade still reaches actual_qty by construction
    # instead of getting stuck at zero.
    if plan_qty_total:
        qty = {r.key: r.plan_qty * (actual_qty_total / plan_qty_total) for r in rows}
    else:
        qty = {r.key: actual_qty_total / len(rows) for r in rows}
    new_state = _state(rows, qty, "plan", "plan")
    legs.append(BridgeLeg(volume_leg_name, new_state - prior_state))
    prior_state = new_state

    # MIX(dim1), MIX(dim1+dim2), ... -- one leg per mix dimension, nested.
    cumulative: list[str] = []
    for dim in mix_dims:
        cumulative.append(dim)
        actual_group_totals: dict[tuple, float] = {}
        operative_group_totals: dict[tuple, float] = {}
        group_members: dict[tuple, list[str]] = {}
        for row in rows:
            g = tuple(row.mix_values[d] for d in cumulative)
            actual_group_totals[g] = actual_group_totals.get(g, 0.0) + row.actual_qty
            operative_group_totals[g] = operative_group_totals.get(g, 0.0) + qty[row.key]
            group_members.setdefault(g, []).append(row.key)

        new_qty = {}
        for row in rows:
            g = tuple(row.mix_values[d] for d in cumulative)
            denom = operative_group_totals[g]
            if denom:
                new_qty[row.key] = actual_group_totals[g] * (qty[row.key] / denom)
            else:
                # No plan-derived share to redistribute by (e.g. a
                # brand-new practice/grade combination with zero plan
                # quantity) -- split the group's actual total evenly
                # across its members rather than dropping it to zero,
                # which would silently break the exact-identity guarantee.
                new_qty[row.key] = actual_group_totals[g] / len(group_members[g])
        qty = new_qty
        new_state = _state(rows, qty, "plan", "plan")
        legs.append(BridgeLeg(f"Mix({dim})", new_state - prior_state))
        prior_state = new_state

    # PRICE: swap to actual price at the now-actual quantity, still plan FX.
    new_state = _state(rows, qty, "actual", "plan")
    legs.append(BridgeLeg(price_leg_name, new_state - prior_state))
    prior_state = new_state

    # FX: swap to actual FX -- this is exactly reported_actual_total.
    new_state = _state(rows, qty, "actual", "actual")
    legs.append(BridgeLeg(fx_leg_name, new_state - prior_state))
    prior_state = new_state

    reported_actual_total = sum(r.actual_qty * r.actual_price * r.actual_fx for r in rows)
    return BridgeResult(plan_total=plan_total, reported_actual_total=reported_actual_total, legs=tuple(legs))


def revenue_bridge(rows: list) -> BridgeResult:
    """Price, Volume, Practice Mix, Grade-within-Practice Mix, FX -- the
    assignment's own diagram order, implemented as
    PLAN -> Volume -> Mix(practice) -> Mix(grade) -> Price -> FX ->
    REPORTED ACTUAL (price is measured last among the operational legs, at
    actual quantity/mix but plan rate, so its interaction lands in the price
    leg rather than the residual)."""
    aggregated = aggregate_to_mix_grain(rows, ("practice", "grade"))
    return decompose(aggregated, mix_dims=("practice", "grade"))


def cost_bridge(rows: list) -> BridgeResult:
    """Efficiency, Rate, FX -- cost's coarser two-leg cousin of the revenue
    waterfall (no practice/grade mix split named in the assignment for cost),
    reusing the same cascade with zero mix dimensions: Efficiency is exactly
    the Volume step (a pure quantity/scale variance at plan rate), Rate is
    the Price step renamed, and FX is added for the same reason revenue
    needs it -- cost is booked in functional currency and translated too."""
    aggregated = aggregate_to_mix_grain(rows, ())
    return decompose(aggregated, mix_dims=(), volume_leg_name="Efficiency", price_leg_name="Rate")
