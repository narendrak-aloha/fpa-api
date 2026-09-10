"""AST node types FinOpsExpr parses to. No node here ever carries a raw SQL
fragment -- that only exists after the compiler (Phase 4) walks these.
"""

from dataclasses import dataclass

Node = "NumberLit | StringLit | Ident | BinOp | Neg | Call"


@dataclass(frozen=True)
class NumberLit:
    value: float


@dataclass(frozen=True)
class StringLit:
    value: str


@dataclass(frozen=True)
class Ident:
    name: str


@dataclass(frozen=True)
class BinOp:
    op: str  # "+" "-" "*" "/" "^"
    left: object
    right: object


@dataclass(frozen=True)
class Neg:
    operand: object


@dataclass(frozen=True)
class Call:
    func: str
    args: tuple


# ---- predicates (shared by query WHERE and in-expr WHERE(x, pred)) -------


@dataclass(frozen=True)
class Comparison:
    dim: str
    op: str  # "=" "!=" "IN" "NOT IN"
    values: tuple


@dataclass(frozen=True)
class BoolPred:
    op: str  # "AND" "OR"
    left: object
    right: object


# ---- query -----------------------------------------------------------------


@dataclass(frozen=True)
class PeriodRange:
    start: str
    end: str | None = None


@dataclass(frozen=True)
class AsOf:
    value: str | float


@dataclass(frozen=True)
class Compare:
    plan_version: str
    scenario: str | None = None


@dataclass(frozen=True)
class Query:
    measures: tuple
    by: tuple[str, ...] = ()
    where: object = None
    period: PeriodRange | None = None
    as_of: AsOf | None = None
    compare: Compare | None = None
    bridge: bool = False
    limit: int | None = None
