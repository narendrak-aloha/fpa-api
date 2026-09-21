"""The recompute arithmetic. No I/O, no clock, no randomness.

Everything here is a pure function of its arguments, which is why the workflow
is allowed to call it directly and why the unit tests need no database. The
activities in ``activities.py`` do the reading and writing around it.

How a driver reaches a plan line
--------------------------------
``planning_model.calc_order_dag`` says which drivers depend on which. It does
not say which plan lines a driver moves, so ``plan_driver_binding`` carries
that: a binding names an account, the column it scales (``quantity`` or
``unit_price``) and an elasticity.

A shock gives a driver an absolute old and new value, so its ratio is
``to / from``. Every driver downstream of it in the DAG is stale and is
recomputed too. The ratio a downstream driver inherits is the shock's ratio:
this codebase has the dependency edges but not the numeric form of each
dependent's formula, so the strength of the response lives in the binding's
elasticity rather than being invented here. That assumption is written into
every line's derivation trace, so an auditor reads it rather than guessing it.

A binding turns a ratio into a factor:

    factor = 1 + elasticity * (ratio - 1)

Elasticity 1.0 is a proportional move, 0.0 is no response, and anything in
between damps it. Factors for the same account and column compose by
multiplication, so two stale drivers hitting one account both count.
"""

from __future__ import annotations

from collections import deque
from decimal import ROUND_HALF_UP, Decimal

from .models import AccountFactor, DriverBinding, DriverShock

# Matches plan_version_line.quantity / unit_price, which are Numeric(20, 6).
QUANTUM_6 = Decimal("0.000001")
# Matches amount_functional, Numeric(20, 2), and the check constraint that ties
# it to round(quantity * unit_price, 2).
QUANTUM_2 = Decimal("0.01")


def merge_shocks(committed: list[DriverShock], requested: list[DriverShock]) -> list[DriverShock]:
    """The cumulative shock set a run applies: what is already published, plus this request.

    Every run recomputes from the frozen baseline, so it has to apply every
    driver move that is currently in the published plan, not just its own --
    otherwise approving heads after utilisation publishes a plan without the
    utilisation move, and the earlier approval is silently undone.

    A driver is carried as (baseline value, latest target). A request for a
    driver that is already moved keeps the baseline and replaces the target.
    A move back to the baseline stays in the set as a ratio of one: dropping
    it would leave nothing to recompute while the cube still showed the old
    move. The result is sorted, because it is hashed into the idempotency key
    and written into workflow history.
    """
    merged: dict[str, DriverShock] = {s.driver_code: s for s in committed}
    for shock in requested:
        earlier = merged.get(shock.driver_code)
        baseline = earlier.from_value if earlier else shock.from_value
        merged[shock.driver_code] = DriverShock(shock.driver_code, baseline, shock.to_value)
    return [merged[code] for code in sorted(merged)]


def dependents(calc_order_dag: list[dict], driver_code: str) -> list[str]:
    """Every driver downstream of ``driver_code``, in dependency order.

    The DAG arrives as ``[{"driver": "x", "depends_on": ["y"]}, ...]``, which
    is the *upstream* direction, so this walks it backwards. The order matters
    only for readability in the trace, but a deterministic order is worth
    having when the result ends up in workflow history.
    """
    children: dict[str, list[str]] = {}
    order: list[str] = []
    for node in calc_order_dag:
        code = node["driver"]
        order.append(code)
        for parent in node.get("depends_on") or []:
            children.setdefault(parent, []).append(code)

    rank = {code: index for index, code in enumerate(order)}
    seen: set[str] = set()
    queue = deque([driver_code])
    while queue:
        current = queue.popleft()
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return sorted(seen, key=lambda code: rank.get(code, len(rank)))


def dirty_drivers(calc_order_dag: list[dict], shocks: list[DriverShock]) -> dict[str, float]:
    """The stale drivers and the ratio each of them carries.

    A shocked driver carries its own ratio. A downstream driver inherits the
    ratio of whatever shocked it; when two shocks reach the same driver, the
    ratios compose, because both moves are real.
    """
    ratios: dict[str, float] = {}
    for shock in shocks:
        for code in [shock.driver_code, *dependents(calc_order_dag, shock.driver_code)]:
            ratios[code] = ratios.get(code, 1.0) * shock.ratio()
    return ratios


def account_factors(ratios: dict[str, float], bindings: list[DriverBinding]) -> list[AccountFactor]:
    """Collapse the stale drivers onto the accounts and columns they move.

    The workflow sends this list to every child, so it is sorted: an unordered
    map would make two identical runs produce different workflow histories.
    """
    composed: dict[tuple[str, str], float] = {}
    for binding in bindings:
        ratio = ratios.get(binding.driver_code)
        if ratio is None:
            continue
        factor = 1.0 + binding.elasticity * (ratio - 1.0)
        key = (binding.account_code, binding.target)
        composed[key] = composed.get(key, 1.0) * factor
    return [
        AccountFactor(account_code=account, target=target, factor=factor)
        for (account, target), factor in sorted(composed.items())
    ]


def affected_accounts(factors: list[AccountFactor]) -> list[str]:
    """The accounts whose lines are dirty. A factor of exactly 1.0 still counts:
    it means a binding matched and the trace should say so."""
    return sorted({factor.account_code for factor in factors})


def recompute_line(quantity: float, unit_price: float, factors: list[AccountFactor], account_code: str) -> tuple[Decimal, Decimal, Decimal]:
    """One line's new quantity, unit price and amount.

    Returns ``Decimal`` at the column's own precision rather than floats,
    because ``plan_version_line`` has a check constraint tying the amount to
    ``round(quantity * unit_price, 2)``. Rounding the inputs first and then
    multiplying is what makes the constraint hold; multiplying the unrounded
    floats and rounding once does not.
    """
    quantity_factor = 1.0
    price_factor = 1.0
    for factor in factors:
        if factor.account_code != account_code:
            continue
        if factor.target == "quantity":
            quantity_factor *= factor.factor
        elif factor.target == "unit_price":
            price_factor *= factor.factor

    new_quantity = Decimal(str(quantity * quantity_factor)).quantize(QUANTUM_6, rounding=ROUND_HALF_UP)
    new_price = Decimal(str(unit_price * price_factor)).quantize(QUANTUM_6, rounding=ROUND_HALF_UP)
    amount = (new_quantity * new_price).quantize(QUANTUM_2, rounding=ROUND_HALF_UP)
    return new_quantity, new_price, amount


def derivation_trace(
    account_code: str,
    factors: list[AccountFactor],
    shock_trace: dict[str, dict[str, float]],
) -> dict:
    """What went into this line, in the shape the JSONB column expects.

    ``plan_version_line`` has a check constraint that this is a non-empty
    object, which is the schema insisting a line can always say where it came
    from.
    """
    applied = {
        factor.target: round(factor.factor, 10)
        for factor in factors
        if factor.account_code == account_code
    }
    return {
        "method": "driver_elasticity",
        "account": account_code,
        "applied_factors": applied,
        "drivers": shock_trace,
    }


def partition_plan(
    scenario_codes: list[str],
    period_months: list[str],
    row_counts: dict[tuple[str, str], int],
    target_size: int,
) -> list[tuple[str, list[str], int]]:
    """Group (scenario, month) cells into partitions of roughly ``target_size``.

    Months are the natural seam: a plan line belongs to exactly one, so no two
    partitions can write the same row and the children never contend. A cell
    bigger than the target becomes its own partition rather than being split
    further, because splitting inside a month would mean two children sharing
    a key.
    """
    partitions: list[tuple[str, list[str], int]] = []
    for scenario in scenario_codes:
        current: list[str] = []
        current_rows = 0
        for month in period_months:
            rows = row_counts.get((scenario, month), 0)
            if rows == 0:
                continue
            if current and current_rows + rows > target_size:
                partitions.append((scenario, current, current_rows))
                current, current_rows = [], 0
            current.append(month)
            current_rows += rows
        if current:
            partitions.append((scenario, current, current_rows))
    return partitions
