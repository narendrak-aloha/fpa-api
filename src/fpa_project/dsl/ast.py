from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Union


@dataclass(frozen=True)
class Literal:
    value: Union[str, Decimal]
    is_string: bool


@dataclass(frozen=True)
class TimeFunction:
    name: str
    metric: str | TimeFunction
    offset: Decimal | None = None


@dataclass(frozen=True)
class Measure:
    name: str | TimeFunction
    alias: str | None = None


@dataclass(frozen=True)
class Comparison:
    field: str
    operator: str
    values: tuple[Literal, ...]


@dataclass(frozen=True)
class Period:
    start: str
    end: str


@dataclass(frozen=True)
class PlanRef:
    version: str
    scenario: str = "base"


@dataclass(frozen=True)
class Query:
    measures: tuple[Measure, ...]
    dimensions: tuple[str, ...] = ()
    predicates: tuple[Comparison, ...] = ()
    predicate_connectors: tuple[str, ...] = ()
    period: Period | None = None
    as_of: str | None = None
    plan: PlanRef | None = None
    bridge: bool = False
    limit: int | None = None
