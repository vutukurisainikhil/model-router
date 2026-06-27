"""Model registry: maps unified model ids to provider routing entries."""
from __future__ import annotations


def build_registry(do_default_model: str) -> dict[str, dict]:
    """Build the registry dict, seeding the default DO model as primary.

    Fallback chain design (4 levels, cheapest last):
      1. Primary   — the model the caller asked for
      2. Fallback 1 — mid-tier: llama3.3-70b-instruct  ($0.60/1M)
      3. Fallback 2 — economy:  llama3.1-8b-instruct   ($0.10/1M)
      4. Fallback 3 — free:     mock/echo              ($0.00, in-process)
    """
    base: dict[str, dict] = {
        # ── Premium tier (GPT-4o-mini via DO)
        # Chain: gpt-4o-mini → llama3.3-70b → llama3.1-8b → mock/echo
        "do/openai-gpt-4o-mini": {
            "primary": {"provider": "do", "model": "openai-gpt-4o-mini"},
            "fallbacks": [
                {"provider": "do", "model": "llama3.3-70b-instruct"},
                {"provider": "do", "model": "llama3.1-8b-instruct"},
                {"provider": "mock", "model": "echo"},
            ],
        },
        # ── Standard tier (Llama 3.3 70B)
        # Chain: llama3.3-70b → mistral-24b → llama3.1-8b → mock/echo
        "do/llama3.3-70b-instruct": {
            "primary": {"provider": "do", "model": "llama3.3-70b-instruct"},
            "fallbacks": [
                {"provider": "do", "model": "mistral-24b-instruct"},
                {"provider": "do", "model": "llama3.1-8b-instruct"},
                {"provider": "mock", "model": "echo"},
            ],
        },
        # ── Mid tier (Mistral 24B)
        # Chain: mistral-24b → llama3.1-8b → mock/echo
        "do/mistral-24b-instruct": {
            "primary": {"provider": "do", "model": "mistral-24b-instruct"},
            "fallbacks": [
                {"provider": "do", "model": "llama3.1-8b-instruct"},
                {"provider": "mock", "model": "echo"},
            ],
        },
        # ── Economy tier (Llama 3.1 8B — fastest, cheapest DO model)
        # Chain: llama3.1-8b → mock/echo
        "do/llama3.1-8b-instruct": {
            "primary": {"provider": "do", "model": "llama3.1-8b-instruct"},
            "fallbacks": [
                {"provider": "mock", "model": "echo"},
            ],
        },
        # ── Last resort (in-process, no network)
        "mock/echo": {
            "primary": {"provider": "mock", "model": "echo"},
            "fallbacks": [],
        },
    }

    # Always register the configured default model
    default_key = f"do/{do_default_model}"
    if default_key not in base:
        base[default_key] = {
            "primary": {"provider": "do", "model": do_default_model},
            "fallbacks": [
                {"provider": "do", "model": "llama3.3-70b-instruct"},
                {"provider": "do", "model": "llama3.1-8b-instruct"},
                {"provider": "mock", "model": "echo"},
            ],
        }

    return base


class ModelRegistry:
    def __init__(self, registry: dict[str, dict]) -> None:
        self._registry = registry

    def resolve(self, unified_model_id: str) -> dict | None:
        """Return the routing entry for the given unified model id, or None."""
        return self._registry.get(unified_model_id)

    def known_models(self) -> list[str]:
        return list(self._registry.keys())
