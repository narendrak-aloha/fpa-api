"""The one place that picks which LLM backs the Copilot team, so
`build_team` itself stays provider-agnostic (and trivially testable with a
stub model)."""

import os

from agno.models.anthropic import Claude
from agno.models.base import Model

DEFAULT_MODEL_ID = "claude-sonnet-5"


def default_model() -> Model:
    return Claude(id=os.environ.get("FPA_COPILOT_MODEL_ID", DEFAULT_MODEL_ID))
