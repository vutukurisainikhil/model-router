"""Cost manifest: USD per 1M tokens for each unified model id.

Costs are used only when the caller passes `metadata.prefer_cheapest: true`.
The orchestrator will sort the fallback chain cheapest-first before attempting.

Values are illustrative — update from provider pricing pages as needed.
"""
from __future__ import annotations

# (input_per_1m_usd, output_per_1m_usd)
# Tier order cheapest→priciest: mock/echo < llama3.1-8b < mistral-24b < llama3.3-70b < gpt-4o-mini
MODEL_COSTS: dict[str, tuple[float, float]] = {
    "mock/echo":                  (0.00,  0.00),   # free  — in-process
    "do/llama3.1-8b-instruct":    (0.10,  0.10),   # economy tier
    "do/mistral-24b-instruct":    (0.27,  0.27),   # mid tier
    "do/llama3.3-70b-instruct":   (0.60,  0.60),   # standard tier
    "do/openai-gpt-4o-mini":      (0.15,  0.60),   # premium tier (cheap input)
}

_FALLBACK_COST = (999.0, 999.0)  # unknown models sort last


def input_cost(unified_model_id: str) -> float:
    """Return input cost per 1M tokens. Unknown models return 999 so they sort last."""
    return MODEL_COSTS.get(unified_model_id, _FALLBACK_COST)[0]
