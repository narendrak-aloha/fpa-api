"""Framework-neutral safety hooks used around Agno calls and tools."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .masking import mask_for_llm


class MaskingGateHook:
    """Masks model/tool payloads and records a minimal disclosure audit event."""

    def __init__(self, disclosure_log: list[dict[str, Any]] | None = None):
        self.disclosure_log = disclosure_log if disclosure_log is not None else []

    def before_model(self, payload: Any, *, user_id: str) -> Any:
        # Mask at the framework boundary so every model call gets the same
        # protection regardless of which caller constructed the payload.
        masked = mask_for_llm(payload)
        self._record("model_input", user_id)
        return masked

    def before_tool(self, payload: Any, *, user_id: str) -> Any:
        masked = mask_for_llm(payload)
        self._record("tool_input", user_id)
        return masked

    def after_tool(self, payload: Any, *, user_id: str) -> Any:
        masked = mask_for_llm(payload)
        self._record("tool_output", user_id)
        return masked

    def _record(self, event: str, user_id: str) -> None:
        self.disclosure_log.append({
            "event": event,
            "user_id": user_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sensitive_values_disclosed": False,
        })


UNTRACEABLE = "narrative contains untraceable numeric claims"


class ArithmeticVerificationPostHook:
    """Rejects numerical narration claims not present in returned result rows.

    ``context`` is the DSL that was executed: its numbers (the year in
    ``FOR PERIOD 2026-Q2``, the plan version) are part of what the answer
    cites, so naming the period is not an invented figure. Found live: a
    narrative saying "Q2 2026" was rejected as untraceable. Numbers from
    anywhere else still have to be in the rows.
    """

    _number = re.compile(
        r"(?<![A-Za-z0-9_])(?P<value>-?(?:\d{1,3}(?:,\d{3})+|\d+|\.\d+)(?:\.\d+)?)"
        r"(?P<scale>\s*(?:billion|million|thousand|[kKmMbB]|%))?(?![A-Za-z0-9_])"
    )

    def verify(self, narrative: str | None, rows: list[dict[str, Any]], context: str = "") -> tuple[bool, str | None]:
        if not narrative:
            return True, None
        # Collect numeric evidence from returned rows, then require every
        # number in the narrative to be traceable to that evidence.
        available: set[Decimal] = set()
        for row in rows:
            for value in row.values():
                self._collect(value, available)
        for match in self._number.finditer(context or ""):
            if not match.group("scale"):
                available.add(abs(Decimal(match.group("value").replace(",", ""))))
        factors = {"k": Decimal(1000), "thousand": Decimal(1000), "m": Decimal(1000000),
                   "million": Decimal(1000000), "b": Decimal(1000000000), "billion": Decimal(1000000000),
                   "%": Decimal("0.01"), "": Decimal(1)}
        claims = {Decimal(match.group("value").replace(",", "")) * factors[(match.group("scale") or "").strip().lower()]
                  for match in self._number.finditer(narrative)}
        missing = sorted(claims - available)
        if missing:
            return False, f"{UNTRACEABLE}: " + ", ".join(map(str, missing))
        return True, None

    def _collect(self, value: Any, output: set[Decimal]) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float, Decimal)):
            try:
                output.add(Decimal(str(value)))
                # "Missed by 289193.15" cites the row's -289193.15: the sign is
                # carried in words. Found live, rejected as untraceable.
                output.add(abs(Decimal(str(value))))
            except InvalidOperation:
                pass
        elif isinstance(value, str):
            # Decimal amounts are serialized as strings by tools/providers.
            if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                output.add(Decimal(value))
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._collect(item, output)
        elif isinstance(value, dict):
            for item in value.values():
                self._collect(item, output)
