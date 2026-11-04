"""Turn token counts into dollars, so every trace and eval report shows cost.

Prices are USD per 1M tokens, taken from each provider's pricing page on
2026-11-04. They change; update them here when they do.
"""

from __future__ import annotations

# model -> (input $/MTok, output $/MTok)
PRICES: dict[str, tuple[float, float]] = {
    # Up to 100K-token prompts; ours are far smaller.
    "claude-haiku-5-5": (0.10, 0.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    # DeepSeek is half price off-peak. We use the peak price so estimates are
    # never optimistic.
    "deepseek-flash": (0.30, 1.20),
    "gpt-6-luna": (0.10, 0.50),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Return the cost of one call, or None if we don't know the model's price."""
    price = PRICES.get(model)
    if price is None:
        return None
    price_in, price_out = price
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000
