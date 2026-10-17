"""Optional LLM integrations (Anthropic). Everything degrades to the template explainer."""

from __future__ import annotations

import structlog

from app.services.explainer import Explainer, TemplateExplainer
from app.services.llm.anthropic_explainer import AnthropicExplainer, ExplainResult, build_client
from app.services.llm.facts import build_facts, facts_from_models
from app.services.llm.regenerate import regenerate_alert, regenerate_message, schedule_regeneration
from app.services.llm.settings import LLMSettings

log = structlog.get_logger("app.services.llm")


def get_explainer(settings: LLMSettings | None = None, client: object | None = None) -> Explainer:
    """AnthropicExplainer when LLM_ENABLED, a key and the `anthropic` package exist.

    Otherwise the template. `client` is for tests (fake client). Call once at startup:
    budget/cache/breaker live on the instance.
    """
    s = settings or LLMSettings()
    if not s.llm_enabled:
        return TemplateExplainer()
    client = client or build_client(s)
    if client is None:
        log.warning("llm_enabled_but_unavailable", reason="missing key or anthropic package")
        return TemplateExplainer()
    return AnthropicExplainer(s, client)


__all__ = [
    "AnthropicExplainer",
    "ExplainResult",
    "LLMSettings",
    "build_facts",
    "facts_from_models",
    "get_explainer",
    "regenerate_alert",
    "regenerate_message",
    "schedule_regeneration",
]
