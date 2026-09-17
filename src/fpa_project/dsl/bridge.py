from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from collections import defaultdict
from typing import Iterable, Hashable


@dataclass(frozen=True)
class BridgeLine:
    group: Hashable
    plan_quantity: Decimal
    actual_quantity: Decimal
    plan_unit_price: Decimal
    actual_unit_price: Decimal
    plan_fx: Decimal
    actual_fx: Decimal


@dataclass(frozen=True)
class BridgeResult:
    group: Hashable
    gap: Decimal
    price: Decimal
    volume: Decimal
    mix: Decimal
    fx: Decimal
    residual: Decimal
    quantity_variance: Decimal


def decompose(lines: Iterable[BridgeLine]) -> list[BridgeResult]:
    """Decompose matched plan/actual lines into price, volume, mix and FX.

    The operational legs use the assumed plan FX rate. FX is the actual local
    amount multiplied by the difference between actual and plan FX. Volume is
    the group-level quantity change at the weighted-average plan price; mix is
    the line-level interaction left after that volume convention. This makes
    volume + mix equal the full quantity variance and yields an algebraic tie.
    """
    # Group before calculating legs so results reconcile at reporting grain
    # without losing line-level price and mix effects.
    grouped: dict[Hashable, list[BridgeLine]] = defaultdict(list)
    for line in lines:
        grouped[line.group].append(line)
    results: list[BridgeResult] = []
    for group, group_lines in grouped.items():
        plan_qty = sum((line.plan_quantity for line in group_lines), Decimal(0))
        plan_value = sum((line.plan_quantity * line.plan_unit_price for line in group_lines), Decimal(0))
        # Zero planned quantity has no meaningful weighted price; keeping it
        # at zero makes the residual explicit and deterministic.
        average_price = plan_value / plan_qty if plan_qty else Decimal(0)
        plan_total = Decimal(0)
        actual_total = Decimal(0)
        price = Decimal(0)
        volume = Decimal(0)
        mix = Decimal(0)
        fx = Decimal(0)
        for line in group_lines:
            plan_local = line.plan_quantity * line.plan_unit_price
            actual_local = line.actual_quantity * line.actual_unit_price
            plan_total += plan_local * line.plan_fx
            actual_total += actual_local * line.actual_fx
            price += line.actual_quantity * (line.actual_unit_price - line.plan_unit_price) * line.plan_fx
            quantity_change = line.actual_quantity - line.plan_quantity
            volume += quantity_change * average_price * line.plan_fx
            mix += quantity_change * (line.plan_unit_price - average_price) * line.plan_fx
            fx += actual_local * (line.actual_fx - line.plan_fx)
        gap = actual_total - plan_total
        residual = gap - (price + volume + mix + fx)
        results.append(BridgeResult(group, gap, price, volume, mix, fx, residual, sum(
            (line.actual_quantity - line.plan_quantity for line in group_lines), Decimal(0)
        )))
    return results
