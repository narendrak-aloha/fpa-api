# Data model — the frozen contract

This documents what `src/fpa_be/registry/` freezes into code, and the
reasoning behind the measure choices. It is the contract every later phase
(DSL, compiler, bridge, Temporal) resolves against.

## The cube

Two fact tables, both written by `seed_fpa.py` (never modified):

- `fact_gl_actual` — posted actuals, 24 months (2025-2026), two ledger
  vintages.
- `fact_plan_line` — plan `PV-2026-0001`, three scenarios (`base` /
  `stretch` / `downside`), 2026 only.

Both are on the same grain: `company`, `account`, `period_month` as
separate axes, plus a 19-column compound dimension key collapsed into
`dim_signature_hash` (see `src/fpa_be/registry/dimensions.py`). Plan and
actual join on `(company, period_month, account, dim_signature_hash)` —
about 65k matched keys per the seed's own verification output.

### Four facts that shape every later phase

1. **`amount_functional = round(quantity * unit_price, 2)`** on both fact
   tables — separate stored columns, not derived. This is what makes the
   variance bridge (Phase 6) solvable at all: quantity carries volume/mix,
   unit_price carries rate/price.
2. **`fact_gl_actual` is a `ReplacingMergeTree`** keyed on
   `(company, period_month, account, dim_signature_hash)` with `_version`/
   `_is_deleted`. Must be read with `FINAL` or an explicit aggregation —
   never a naive `SELECT *`.
3. **Two ledger vintages.** Vintage 1 is the July close; vintage 2 is the
   August restatement (subcontractor accrual true-up + reversed journals) on
   the same keys. `AS OF <timestamp>` resolves to the vintage whose
   `closed_at <= timestamp` (latest such one); no `AS OF` clause reads the
   latest vintage — an explicit, documented default, not an accident of
   "whatever the table returns."
4. **Intercompany pairing.** ~6% of trade is internal: a sale to
   `CUST-IC-<company>` with `intercompany_flag='Yes'` has exactly one
   mirrored cost row on the buying entity, account `51500`, in that entity's
   functional currency. The pair nets to zero in USD — group revenue is not
   the sum of entity revenue, and elimination has to happen after
   translation, not before.

## Measure registry (`src/fpa_be/registry/measures.py`)

Every measure declares an aggregation kind, enforced later by the compiler
(Phase 4):

| Kind | Rule |
|---|---|
| `additive` | Sums over every dimension, including time. |
| `semi_additive` | Sums across dimensions, but never across time — takes the closing period instead. |
| `ratio` | Never summed or averaged across groups; recomputed from its own numerator/denominator at whatever grain is asked for. |

Measures actually defined, and why:

- `services_revenue`, `recurring_revenue`, `rebillable_revenue`,
  `total_revenue`, `subcontractor_cost`, `delivery_payroll`,
  `delivery_cost`, `total_cogs`, `total_opex` — straight sums of
  `amount_functional` over named account sets from the chart of accounts.
  `delivery_cost` and `subcontractor_cost` match the assignment's own
  grammar examples verbatim.
- `gross_margin` — additive, `total_revenue - total_cogs`. Still additive:
  a difference of two additive sums is itself additive (sums commute).
- `gross_margin_pct` — ratio, `gross_margin / total_revenue`. The
  assignment's canonical illegal-aggregation example: `SUM(gross_margin_pct)`
  must be a compile error, because the sum of three practices' margin
  percentages is not the group's margin percentage.
- `headcount` — semi_additive, `count(distinct resource_employee)` on
  delivery-payroll (`51000`) rows in the period. Sums across companies/
  practices, but a full year's headcount is the December figure, not the
  sum of twelve months' counts.
- `utilisation`, `realisation` — ratio. The assignment's grammar examples
  use these but assumes a headcount/available-hours feed this seed does not
  generate. What the seed *does* generate: T&M revenue rows (`41000`) and
  delivery-payroll rows (`51000`) both carry an hours-shaped `quantity` and
  a rate-shaped `unit_price`, on the same `resource_employee` dimension.
  That is enough to define both honestly from real data:
  - `utilisation = sum(quantity on 41000) / sum(quantity on 51000)` —
    billed hours over paid hours.
  - `realisation = sum(amount_functional on 41000) / sum(quantity on 41000)` —
    the average bill rate actually realised.
  These are documented, defensible readings of the assignment's vocabulary,
  not the only possible ones — stated here so they don't need re-deriving
  mid-build.

**Left out on purpose:** `bookings`, `open_pipeline`, `heads`,
`available_hours`, `attach_rate`, `attrition`, `win_rate` appear in the
assignment's grammar sketch as illustrative examples, but this seed has no
CRM/pipeline/HR-headcount-plan fact to back them. Inventing numbers for
them would be exactly the kind of quiet, untraceable assumption the
assignment argues against. `heads`, `available_hours`, `bill_rate`,
`attrition`, and `win_rate` instead become **drivers** — named,
effective-dated formulas in `plan_driver` (Phase 2) that planners define
explicitly, rather than measures the cube is asked to answer for.

## Reference data (`src/fpa_be/registry/reference.py`)

20 companies (4 regions, 8 functional currencies), 25 accounts (7 Revenue,
8 COGS including the intercompany account `51500`, 10 OpEx), 2 vintages.
Copied from `out/cube_manifest.json` (a build artifact, not committed) into
code, so the registry is available before the seed has ever run.
