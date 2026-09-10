"""The one place that picks which LLM backs the Copilot team, so
`build_team` itself stays provider-agnostic (and trivially testable with a
stub model). Anthropic, OpenAI and Google are all supported: `FPA_COPILOT_
MODEL_ID` names the model outright and its prefix picks the provider,
otherwise the first provider below whose API key is present wins. Claude
stays the fallback so a keyless dev/test environment behaves exactly as it
did before the other two were options.
"""

import os

from agno.models.anthropic import Claude
from agno.models.base import Model
from agno.models.google import Gemini
from agno.models.openai import OpenAIChat

DEFAULT_MODEL_ID_CLAUDE = "claude-sonnet-5"
DEFAULT_MODEL_ID_GPT = "gpt-4o"
DEFAULT_MODEL_ID_GEMINI = "gemini-2.0-flash"

# (credential env var, model class, default model id, model-id prefix).
# Order is the precedence when several credentials are present.
_PROVIDERS = (
    ("ANTHROPIC_API_KEY", Claude, DEFAULT_MODEL_ID_CLAUDE, "claude"),
    ("OPENAI_API_KEY", OpenAIChat, DEFAULT_MODEL_ID_GPT, "gpt"),
    ("GOOGLE_API_KEY", Gemini, DEFAULT_MODEL_ID_GEMINI, "gemini"),
)


def default_model() -> Model:
    model_id = os.environ.get("FPA_COPILOT_MODEL_ID")
    if model_id:
        for _, model_cls, _, prefix in _PROVIDERS:
            if model_id.startswith(prefix):
                return model_cls(id=model_id)
        return Claude(id=model_id)

    for credential, model_cls, default_id, _ in _PROVIDERS:
        if os.environ.get(credential):
            return model_cls(id=default_id)

    return Claude(id=DEFAULT_MODEL_ID_CLAUDE)
