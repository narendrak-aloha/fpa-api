"""Turns a FinOpsExpr PERIOD_TOKEN ('2026-Q2', '2026-H1', '2026-01') into a
half-open [start, end) date range on period_month -- the shape ClickHouse
needs to prune fact_gl_actual's toYYYYMM(period_month) partitions.
"""

import datetime

from fpa_be.dsl import ast_nodes as ast


def add_months(d: datetime.date, n: int) -> datetime.date:
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return datetime.date(year, month, 1)


def token_bounds(token: str) -> tuple[datetime.date, datetime.date]:
    """A single PERIOD_TOKEN's own [start, end) range."""
    year_str, part = token.split("-", 1)
    year = int(year_str)
    if part.startswith("Q"):
        quarter = int(part[1])
        start = datetime.date(year, (quarter - 1) * 3 + 1, 1)
        return start, add_months(start, 3)
    if part.startswith("H"):
        half = int(part[1])
        start = datetime.date(year, 1 if half == 1 else 7, 1)
        return start, add_months(start, 6)
    month = int(part)
    start = datetime.date(year, month, 1)
    return start, add_months(start, 1)


def range_bounds(period: ast.PeriodRange) -> tuple[datetime.date, datetime.date]:
    """A PeriodRange's overall [start, end) span, inclusive of both tokens
    when a range ('2026-Q1..2026-Q4') is given."""
    start, single_end = token_bounds(period.start)
    if period.end is None:
        return start, single_end
    _, end = token_bounds(period.end)
    return start, end


def month_count(start: datetime.date, end: datetime.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)
