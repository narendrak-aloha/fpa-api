"""Tests for LLM provider selection in agents/model.py.

Ensures that default_model() correctly selects between Claude, GPT, and Gemini
based on available API keys in the documented precedence order.
"""

import os

import pytest
from agno.models.anthropic import Claude
from agno.models.google import Gemini
from agno.models.openai import OpenAIChat

from fpa_be.agents.model import (
    DEFAULT_MODEL_ID_CLAUDE,
    DEFAULT_MODEL_ID_GEMINI,
    DEFAULT_MODEL_ID_GPT,
    default_model,
)


@pytest.fixture
def clear_env():
    """Clear all LLM-related env vars before each test."""
    keys_to_clear = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "FPA_COPILOT_MODEL_ID"]
    original = {key: os.environ.get(key) for key in keys_to_clear}
    for key in keys_to_clear:
        os.environ.pop(key, None)
    yield
    # Restore original values
    for key, value in original.items():
        if value is not None:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def test_default_model_uses_claude_when_anthropic_key_set(clear_env):
    """When ANTHROPIC_API_KEY is set, should return Claude model."""
    os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
    model = default_model()
    assert isinstance(model, Claude)
    assert model.id == DEFAULT_MODEL_ID_CLAUDE


def test_default_model_uses_gpt_when_openai_key_set(clear_env):
    """When OPENAI_API_KEY is set (and no Anthropic key), should return OpenAIChat model."""
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    model = default_model()
    assert isinstance(model, OpenAIChat)
    assert model.id == DEFAULT_MODEL_ID_GPT


def test_default_model_uses_gemini_when_google_key_set(clear_env):
    """When GOOGLE_API_KEY is set (and no other keys), should return Gemini model."""
    os.environ["GOOGLE_API_KEY"] = "test-google-key"
    model = default_model()
    assert isinstance(model, Gemini)
    assert model.id == DEFAULT_MODEL_ID_GEMINI


def test_default_model_respects_precedence_anthropic_over_openai(clear_env):
    """ANTHROPIC_API_KEY takes precedence over OPENAI_API_KEY."""
    os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    model = default_model()
    assert isinstance(model, Claude)
    assert model.id == DEFAULT_MODEL_ID_CLAUDE


def test_default_model_respects_precedence_openai_over_gemini(clear_env):
    """OPENAI_API_KEY takes precedence over GOOGLE_API_KEY."""
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    os.environ["GOOGLE_API_KEY"] = "test-google-key"
    model = default_model()
    assert isinstance(model, OpenAIChat)
    assert model.id == DEFAULT_MODEL_ID_GPT


def test_default_model_respects_full_precedence_order(clear_env):
    """All three keys set: ANTHROPIC > OPENAI > GOOGLE."""
    os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    os.environ["GOOGLE_API_KEY"] = "test-google-key"
    model = default_model()
    assert isinstance(model, Claude)
    assert model.id == DEFAULT_MODEL_ID_CLAUDE


def test_default_model_fallback_to_claude_when_no_keys(clear_env):
    """When no API keys are set, should fallback to Claude (for dev/testing)."""
    model = default_model()
    assert isinstance(model, Claude)
    assert model.id == DEFAULT_MODEL_ID_CLAUDE


def test_default_model_respects_explicit_fpa_copilot_model_id_claude(clear_env):
    """FPA_COPILOT_MODEL_ID can override model ID for Claude."""
    os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
    os.environ["FPA_COPILOT_MODEL_ID"] = "claude-opus-5"
    model = default_model()
    assert isinstance(model, Claude)
    assert model.id == "claude-opus-5"


def test_default_model_respects_explicit_fpa_copilot_model_id_gpt(clear_env):
    """FPA_COPILOT_MODEL_ID can override model ID for GPT."""
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    os.environ["FPA_COPILOT_MODEL_ID"] = "gpt-4-turbo"
    model = default_model()
    assert isinstance(model, OpenAIChat)
    assert model.id == "gpt-4-turbo"


def test_default_model_respects_explicit_fpa_copilot_model_id_gemini(clear_env):
    """FPA_COPILOT_MODEL_ID can override model ID for Gemini."""
    os.environ["GOOGLE_API_KEY"] = "test-google-key"
    os.environ["FPA_COPILOT_MODEL_ID"] = "gemini-1.5-pro"
    model = default_model()
    assert isinstance(model, Gemini)
    assert model.id == "gemini-1.5-pro"


def test_default_model_explicit_model_id_overrides_precedence(clear_env):
    """Explicit FPA_COPILOT_MODEL_ID with claude ID uses Claude regardless of other keys."""
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    os.environ["GOOGLE_API_KEY"] = "test-google-key"
    os.environ["FPA_COPILOT_MODEL_ID"] = "claude-sonnet-5"
    model = default_model()
    assert isinstance(model, Claude)
    assert model.id == "claude-sonnet-5"


def test_default_model_explicit_model_id_infers_gpt(clear_env):
    """Explicit FPA_COPILOT_MODEL_ID with gpt- prefix infers OpenAI."""
    os.environ["FPA_COPILOT_MODEL_ID"] = "gpt-4o-mini"
    model = default_model()
    assert isinstance(model, OpenAIChat)
    assert model.id == "gpt-4o-mini"


def test_default_model_explicit_model_id_infers_gemini(clear_env):
    """Explicit FPA_COPILOT_MODEL_ID with gemini prefix infers Google."""
    os.environ["FPA_COPILOT_MODEL_ID"] = "gemini-ultra"
    model = default_model()
    assert isinstance(model, Gemini)
    assert model.id == "gemini-ultra"
