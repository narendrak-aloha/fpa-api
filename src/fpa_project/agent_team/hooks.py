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


class ArithmeticVerificationPostHook:
    """Rejects numerical narration claims not present in returned result rows."""

    _number = re.compile(r"(?<![A-Za-z0-9_])-?(?:\d+(?:\.\d+)?|\.\d+)(?![A-Za-z0-9_])")

    def verify(self, narrative: str | None, rows: list[dict[str, Any]]) -> tuple[bool, str | None]:
        if not narrative:
            return True, None
        # Collect numeric evidence from returned rows, then require every
        # number in the narrative to be traceable to that evidence.
        available: set[Decimal] = set()
        for row in rows:
            for value in row.values():
                self._collect(value, available)
        claims = {Decimal(match.group(0)) for match in self._number.finditer(narrative)}
        missing = sorted(claims - available)
        if missing:
            return False, "narrative contains untraceable numeric claims: " + ", ".join(map(str, missing))
        return True, None

    def _collect(self, value: Any, output: set[Decimal]) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float, Decimal)):
            try:
                output.add(Decimal(str(value)))
            except InvalidOperation:
                pass
        elif isinstance(value, dict):
            for item in value.values():
                self._collect(item, output)
