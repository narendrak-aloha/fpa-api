"""AS OF -> vintage resolution.

A timestamp string resolves to the latest vintage whose dim_ledger_vintage
.closed_at is at or before it -- "what the books said as of that instant."
A bare number (`AS OF 2`) already *is* a vintage id, so it's validated but
not looked up by time. No AS OF clause at all means resolved_vintage=None,
which the compiler documents and treats as "latest" (FINAL) -- a default
that's written down here, not left implicit.
"""

from fpa_be.cube.errors import NoVintageClosedYetError, UnknownVintageError
from fpa_be.dsl import ast_nodes as ast


def resolve_vintage(as_of: ast.AsOf | None, client) -> int | None:
    if as_of is None:
        return None

    if isinstance(as_of.value, (int, float)):
        vintage = int(as_of.value)
        exists = client.query(
            "SELECT count() FROM dim_ledger_vintage WHERE vintage = {v:UInt64}",
            parameters={"v": vintage},
        ).result_rows[0][0]
        if not exists:
            raise UnknownVintageError(vintage)
        return vintage

    result = client.query(
        "SELECT vintage FROM dim_ledger_vintage WHERE closed_at <= {ts:DateTime} ORDER BY closed_at DESC LIMIT 1",
        parameters={"ts": as_of.value},
    )
    if not result.result_rows:
        raise NoVintageClosedYetError(as_of.value)
    return int(result.result_rows[0][0])
