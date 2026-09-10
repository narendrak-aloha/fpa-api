"""The authenticated HTTP boundary in front of the agent tier: caller
identity (API key) resolves to a SecurityContext server-side, never from
the request body. A live model call needs a real ANTHROPIC_API_KEY, so only
the authentication boundary itself is exercised here.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_missing_api_key_is_rejected(client):
    resp = await client.post("/copilot/query", json={"question": "What was Q2 revenue?"})
    assert resp.status_code == 401


async def test_unknown_api_key_is_rejected(client):
    resp = await client.post(
        "/copilot/query", json={"question": "What was Q2 revenue?"}, headers={"x-api-key": "not-a-real-key"}
    )
    assert resp.status_code == 401
