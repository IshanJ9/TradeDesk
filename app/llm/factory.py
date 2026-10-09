"""Chooses the LLM provider from LLM_PROVIDER.

To add a provider (Bedrock, OpenAI, ...): write one class with `async complete(system, messages,
tools) -> LLMTurn` that translates app/llm/types.py to and from the provider's API, and add a branch
here. Nothing else in the app changes.
"""

from collections.abc import Callable

from app.config import Settings
from app.llm.rules import RuleBasedLLM
from app.llm.types import LLMClient


def make_llm(settings: Settings, renderers: dict[str, Callable[[dict], str]]) -> LLMClient:
    provider = settings.llm_provider
    if provider in ("", "rules"):
        return RuleBasedLLM(renderers)
    raise NotImplementedError(
        f"LLM_PROVIDER={provider!r} is not wired yet. Use LLM_PROVIDER=rules (the built-in keyword "
        "parser) until the provider class is added in app/llm/factory.py."
    )
