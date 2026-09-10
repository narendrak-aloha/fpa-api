"""Phase 13's PII gate: classification is by dimension name (never by
sniffing the value), personal columns are tokenized consistently, and every
call writes a `llm_disclosure_log` row -- classes/methods/scope/hash only,
never the payload -- before the caller gets anything back.
"""

import json

import asyncpg
import pytest

from fpa_be.compiler.security import SecurityContext
from fpa_be.db import app_dsn
from fpa_be.masking.gate import PERSONAL_COLUMNS, classify_and_mask, mask_and_disclose

PL_SCOPE = SecurityContext(allowed_companies=frozenset({"RTPL1"}))


class TestClassifyAndMask:
    def test_non_personal_columns_pass_through_unchanged(self):
        masked, classes = classify_and_mask(["services_revenue"], [[100.0], [200.0]])
        assert masked == [[100.0], [200.0]]
        assert classes == set()

    def test_customer_column_is_tokenized(self):
        masked, classes = classify_and_mask(["customer", "services_revenue"], [["CUST-001", 100.0]])
        assert classes == {"customer_identity"}
        assert masked[0][0].startswith("[customer_identity:")
        assert masked[0][1] == 100.0

    def test_resource_employee_column_is_tokenized(self):
        masked, classes = classify_and_mask(["resource_employee"], [["EMP-42"]])
        assert classes == {"employee_identity"}
        assert masked[0][0].startswith("[employee_identity:")

    def test_masking_is_consistent_for_the_same_raw_value(self):
        masked, _ = classify_and_mask(["customer"], [["CUST-001"], ["CUST-001"], ["CUST-002"]])
        assert masked[0][0] == masked[1][0]
        assert masked[0][0] != masked[2][0]

    def test_all_personal_columns_are_classified(self):
        columns = list(PERSONAL_COLUMNS)
        masked, classes = classify_and_mask(columns, [["x"] * len(columns)])
        assert classes == set(PERSONAL_COLUMNS.values())
        assert all(str(v).startswith("[") for v in masked[0])


class TestMaskAndDisclose:
    @pytest.mark.asyncio
    async def test_writes_disclosure_log_row_with_hash_not_payload(self):
        masked = await mask_and_disclose("test_tool", PL_SCOPE, ["customer", "services_revenue"], [["CUST-001", 100.0]])
        assert masked[0][0].startswith("[customer_identity:")

        conn = await asyncpg.connect(app_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT classes, methods, scope, payload_hash FROM llm_disclosure_log "
                "WHERE tool_name = 'test_tool' ORDER BY id DESC LIMIT 1"
            )
        finally:
            await conn.close()
        assert list(row["classes"]) == ["customer_identity"]
        assert list(row["methods"]) == ["tokenize"]
        assert json.loads(row["scope"])["allowed_companies"] == ["RTPL1"]
        assert len(row["payload_hash"]) == 64
        assert "CUST-001" not in row["payload_hash"]

    @pytest.mark.asyncio
    async def test_no_personal_columns_still_logs_with_none_markers(self):
        await mask_and_disclose("test_tool_clean", PL_SCOPE, ["services_revenue"], [[100.0]])

        conn = await asyncpg.connect(app_dsn())
        try:
            row = await conn.fetchrow(
                "SELECT classes, methods FROM llm_disclosure_log WHERE tool_name = 'test_tool_clean' "
                "ORDER BY id DESC LIMIT 1"
            )
        finally:
            await conn.close()
        assert list(row["classes"]) == ["none"]
        assert list(row["methods"]) == ["none"]
