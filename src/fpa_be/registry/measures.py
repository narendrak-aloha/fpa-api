"""The measure registry: every metric the DSL/compiler can resolve, and how
each is legally allowed to aggregate.

Three kinds, per the assignment's own table:

- additive: sums over every dimension, including time.
- semi_additive: sums across dimensions, but never across time -- takes the
  closing period instead.
- ratio: never summed and never averaged across groups; recomputed from its
  own numerator and denominator at whatever grain it is asked for.

Everything here is built only from what fact_gl_actual / fact_plan_line
actually carry (amount_functional, quantity, unit_price, and the 19+3 grain
columns). The assignment's own grammar examples name a few measures this
seed has no fact for -- bookings and open_pipeline need a CRM/pipeline feed
this cube does not have. Rather than invent ungrounded numbers for them,
they are left out; see docs/DATA_MODEL.md for the full reasoning.
"""

from dataclasses import dataclass
from enum import Enum


class MeasureKind(str, Enum):
    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    RATIO = "ratio"


@dataclass(frozen=True)
class AccountAmountMeasure:
    """additive: sum(column) over a fixed set of accounts.

    column is "amount_functional" for every user-facing money measure; the
    "_billed_hours"/"_paid_hours" internal helpers sum "quantity" instead.
    """

    name: str
    description: str
    accounts: tuple[str, ...]
    column: str = "amount_functional"
    kind: MeasureKind = MeasureKind.ADDITIVE


@dataclass(frozen=True)
class ComputedAdditiveMeasure:
    """additive: a signed sum of other additive measures (e.g. margin = revenue - cogs)."""

    name: str
    description: str
    terms: tuple[tuple[str, str], ...]  # (sign, referenced measure name), sign in {"+", "-"}
    kind: MeasureKind = MeasureKind.ADDITIVE


@dataclass(frozen=True)
class DistinctCountMeasure:
    """semi_additive: count(distinct dim) over a fixed set of accounts.

    Sums across dimensions other than time (e.g. across companies), but
    across time takes the closing period's count rather than summing months.
    """

    name: str
    description: str
    accounts: tuple[str, ...]
    distinct_dim: str
    kind: MeasureKind = MeasureKind.SEMI_ADDITIVE


@dataclass(frozen=True)
class RatioMeasure:
    """ratio: numerator / denominator, recomputed at whatever grain is asked for.

    Both sides name another measure in this registry. Never legal inside
    SUM/AVG -- the compiler must reject that at typecheck time.
    """

    name: str
    description: str
    numerator: str
    denominator: str
    kind: MeasureKind = MeasureKind.RATIO


Measure = AccountAmountMeasure | ComputedAdditiveMeasure | DistinctCountMeasure | RatioMeasure

_REVENUE_ACCOUNTS = ("41000", "41010", "41020", "41100", "41200", "41300", "41400")
_COGS_ACCOUNTS = ("51000", "51050", "51100", "51200", "51250", "51300", "51400", "51500")
_OPEX_ACCOUNTS = ("61000", "61100", "61200", "62000", "62100", "63000", "63100", "63200", "63300", "64000")

MEASURES: dict[str, Measure] = {
    m.name: m
    for m in (
        AccountAmountMeasure(
            "services_revenue",
            "T&M, fixed-fee, and change-order revenue.",
            ("41000", "41010", "41020"),
        ),
        AccountAmountMeasure(
            "recurring_revenue",
            "Subscription, usage, and support/maintenance revenue.",
            ("41100", "41200", "41300"),
        ),
        AccountAmountMeasure(
            "rebillable_revenue",
            "Client-reimbursed expense revenue.",
            ("41400",),
        ),
        AccountAmountMeasure("total_revenue", "All revenue accounts.", _REVENUE_ACCOUNTS),
        AccountAmountMeasure(
            "subcontractor_cost",
            "Subcontractor cost (account 51100).",
            ("51100",),
        ),
        AccountAmountMeasure(
            "delivery_payroll",
            "Delivery payroll cost (account 51000) -- doubles as the utilisation denominator.",
            ("51000",),
        ),
        AccountAmountMeasure(
            "delivery_cost",
            "Fully-loaded delivery cost: payroll, bonus, subcontractor, rebillable travel.",
            ("51000", "51050", "51100", "51300"),
        ),
        AccountAmountMeasure("total_cogs", "All COGS accounts, including intercompany.", _COGS_ACCOUNTS),
        AccountAmountMeasure("total_opex", "All OpEx accounts.", _OPEX_ACCOUNTS),
        ComputedAdditiveMeasure(
            "gross_margin",
            "total_revenue - total_cogs.",
            (("+", "total_revenue"), ("-", "total_cogs")),
        ),
        RatioMeasure(
            "gross_margin_pct",
            "gross_margin / total_revenue.",
            numerator="gross_margin",
            denominator="total_revenue",
        ),
        RatioMeasure(
            "utilisation",
            "Billed T&M hours (qty on 41000) / paid delivery hours (qty on 51000). "
            "Both accounts store quantity in hours; see docs/DATA_MODEL.md.",
            numerator="_billed_hours",
            denominator="_paid_hours",
        ),
        RatioMeasure(
            "realisation",
            "Average realised bill rate: T&M revenue $ / billed T&M hours.",
            numerator="services_revenue",
            denominator="_billed_hours",
        ),
        DistinctCountMeasure(
            "headcount",
            "Distinct delivery resources with a payroll line in the period.",
            ("51000",),
            distinct_dim="resource_employee",
        ),
        # Internal quantity-only helpers backing utilisation/realisation --
        # not user-facing metric names, so not exposed by list_metrics.
        AccountAmountMeasure("_billed_hours", "sum(quantity) on 41000.", ("41000",), column="quantity"),
        AccountAmountMeasure("_paid_hours", "sum(quantity) on 51000.", ("51000",), column="quantity"),
    )
}

# Names hidden from list_metrics/the agent tools but still resolvable by the
# compiler when referenced as another measure's numerator/denominator.
INTERNAL_MEASURES = frozenset({"_billed_hours", "_paid_hours"})


def public_measures() -> dict[str, Measure]:
    return {name: m for name, m in MEASURES.items() if name not in INTERNAL_MEASURES}
