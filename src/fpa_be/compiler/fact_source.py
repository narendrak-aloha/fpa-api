"""Turns a resolved vintage into the FROM-clause source for fact_gl_actual --
shared by the compiler (Phase 4) and the bridge's matched-row puller
(Phase 6), since both need the same "what did the ledger say as of this
vintage" reconstruction of the ReplacingMergeTree table.
"""

from fpa_be.registry.dimensions import DIM_COLUMNS


def actual_fact_source(resolved_vintage: int | None) -> str:
    if resolved_vintage is None:
        return "fact_gl_actual FINAL"
    # Reconstructs the table's own ReplacingMergeTree(_version, _is_deleted)
    # dedup by hand, scoped to versions <= resolved_vintage, since ClickHouse
    # has no "FINAL as of version N" primitive. Known limitation: on the rare
    # key where two genuinely distinct GL lines collide on the full
    # (company, period_month, account, dim_signature_hash) grain *and* share
    # the same _version, argMax's tie-break isn't guaranteed to match the
    # table engine's own internal merge order for a plain FINAL read -- an
    # inherent ambiguity in ReplacingMergeTree itself when a version isn't
    # unique per key, not something this query can resolve more precisely.
    dim_cols = ", ".join(f"any({d}) AS {d}" for d in DIM_COLUMNS)
    return (
        "(\n"
        "    SELECT company, period_month, account, dim_signature_hash,\n"
        f"           {dim_cols},\n"
        "           argMax(quantity, _version) AS quantity,\n"
        "           argMax(unit_price, _version) AS unit_price,\n"
        "           argMax(amount_functional, _version) AS amount_functional,\n"
        "           argMax(functional_currency, _version) AS functional_currency\n"
        "    FROM fact_gl_actual\n"
        f"    WHERE _version <= {int(resolved_vintage)}\n"
        "    GROUP BY company, period_month, account, dim_signature_hash\n"
        "    HAVING argMax(_is_deleted, _version) = 0\n"
        ")"
    )
