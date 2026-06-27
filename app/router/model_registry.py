"""Model registry: maps unified model ids to provider routing entries."""
from __future__ import annotations


def build_registry(do_default_model: str) -> dict[str, dict]:
    """Build the registry dict, seeding the default DO model as primary."""
    base: dict[str, dict] = {
        "do/llama3.3-70b-instruct": {
            "primary": {"provider": "do", "model": "llama3.3-70b-instruct"},
            "fallbacks": [{"provider": "mock", "model": "echo"}],
        },
        "do/openai-gpt-4o-mini": {
            "primary": {"provider": "do", "model": "openai-gpt-4o-mini"},
            "fallbacks": [
                {"provider": "do", "model": "llama3.3-70b-instruct"},
                {"provider": "mock", "model": "echo"},
            ],
        },
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
