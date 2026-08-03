"""
Provider registry. Initializes both providers at startup
and serves the active one on each request.
"""

from app.ai.providers.anthropic_provider import AnthropicProvider
from app.ai.providers.base import LLMProvider
from app.ai.providers.openai_provider import OpenAIProvider

_providers: dict[str, LLMProvider] = {}

# Models available per provider (shown in admin settings dropdown)
# cost_per_m_tokens: estimated cost in USD per 1M tokens
AVAILABLE_MODELS = {
    "openai": [
        {
            "id": "gpt-5.4-nano",
            "label": "GPT-5.4 Nano (cheap, fast)",
            "default": True,
            "cost_per_m_tokens": {"input": 0.20, "output": 1.25},
        },
        {
            "id": "gpt-5.4-mini",
            "label": "GPT-5.4 Mini",
            "cost_per_m_tokens": {"input": 0.75, "output": 4.50},
        },
        {
            "id": "gpt-5.6-luna",
            "label": "GPT-5.6 Luna",
            "cost_per_m_tokens": {"input": 0.20, "output": 1.20},
        },
        {
            "id": "gpt-5.6-terra",
            "label": "GPT-5.6 Terra",
            "cost_per_m_tokens": {"input": 2.00, "output": 12.00},
        }
    ],
    "anthropic": [
        {
            "id": "claude-haiku-4-5",
            "label": "Claude Haiku 4.5 (warm tone, fast)",
            "default": True,
            "cost_per_m_tokens": {"input": 1.00, "output": 5.00},
        },
        {
            "id": "claude-sonnet-5",
            "label": "Claude Sonnet 5",
            "cost_per_m_tokens": {"input": 2.00, "output": 10.00},
        },
    ],
}


def get_model_costs(model_id: str) -> dict:
    """Look up cost rates for a model. Returns {"input": X, "output": Y} per 1M tokens."""
    for models in AVAILABLE_MODELS.values():
        for m in models:
            if m["id"] == model_id:
                return m.get("cost_per_m_tokens", {"input": 1.0, "output": 3.0})
    return {"input": 1.0, "output": 3.0}


def init_providers(openai_key: str, anthropic_key: str):
    """Called once at app startup. Only initializes providers that have valid keys."""
    if openai_key:
        _providers["openai"] = OpenAIProvider(api_key=openai_key)
    if anthropic_key:
        _providers["anthropic"] = AnthropicProvider(api_key=anthropic_key)

    if not _providers:
        raise RuntimeError(
            "No LLM providers configured. "
            "Set at least OPENAI_API_KEY or ANTHROPIC_API_KEY in your .env"
        )


def get_provider(name: str) -> LLMProvider:
    """Get a provider by name. Raises if the name is unknown."""
    if name not in _providers:
        raise ValueError(
            f"Unknown LLM provider '{name}'. "
            f"Available: {list(_providers.keys())}"
        )
    return _providers[name]


def list_providers() -> list[str]:
    """Return the names of all initialized providers."""
    return list(_providers.keys())
