"""The 19-dimension compound key and the plan/actual join it produces.

Mirrors seed_fpa.py's DIM_COLUMNS and dim_signature() byte-for-byte -- this is
the one join key every governed number in the system has to agree on, so it
is reimplemented here rather than imported from the seed script (which is
frozen and never imported by application code).
"""

import hashlib

# Alphabetical order is load-bearing: it is baked into every stored
# dim_signature_hash. Do not reorder.
DIM_COLUMNS: tuple[str, ...] = (
    "billing_type",
    "business_unit",
    "channel",
    "contract",
    "cost_center",
    "cost_pool",
    "customer",
    "delivery_shore",
    "engine",
    "funding_source",
    "geo_country",
    "geo_region",
    "grade",
    "intercompany_flag",
    "practice",
    "product",
    "project",
    "resource_employee",
    "revenue_type",
)

# company, account and period_month are indexed separately on both fact
# tables; they are not part of the 19-dimension compound key.
SEPARATE_AXES: tuple[str, ...] = ("company", "account", "period_month")


def dim_signature(values: tuple[str, ...]) -> str:
    """The plan-to-actual join key. Must match seed_fpa.py exactly or nothing ties."""
    if len(values) != len(DIM_COLUMNS):
        raise ValueError(f"expected {len(DIM_COLUMNS)} dimension values, got {len(values)}")
    payload = "|".join(f"{dim}={value}" for dim, value in zip(DIM_COLUMNS, values))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
