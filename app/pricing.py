from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _ModelPrice:
    input_per_1m: float
    output_per_1m: float


# Unit: USD per 1M tokens.
# Source: https://openai.com/api/pricing/ (checked on 2026-02-12)
_MODEL_PRICES: dict[str, _ModelPrice] = {
    "gpt-5": _ModelPrice(input_per_1m=1.25, output_per_1m=10.00),
    "gpt-5-mini": _ModelPrice(input_per_1m=0.25, output_per_1m=2.00),
    "gpt-5-nano": _ModelPrice(input_per_1m=0.05, output_per_1m=0.40),
    "gpt-5-codex": _ModelPrice(input_per_1m=1.25, output_per_1m=10.00),
    "gpt-5-codex-mini": _ModelPrice(input_per_1m=0.25, output_per_1m=2.00),
    "gpt-4.1": _ModelPrice(input_per_1m=2.00, output_per_1m=8.00),
    "gpt-4.1-mini": _ModelPrice(input_per_1m=0.40, output_per_1m=1.60),
    "gpt-4.1-nano": _ModelPrice(input_per_1m=0.10, output_per_1m=0.40),
}


_ALIAS_PREFIXES: list[tuple[str, str]] = [
    ("gpt-5.1-codex-mini", "gpt-5-codex-mini"),
    ("gpt-5-codex-mini", "gpt-5-codex-mini"),
    ("gpt-5.1-codex", "gpt-5-codex"),
    ("gpt-5-codex", "gpt-5-codex"),
    ("gpt-5.1-chat", "gpt-5"),
    ("gpt-5.1", "gpt-5"),
    ("gpt-5-chat", "gpt-5"),
    ("gpt-4.1-mini", "gpt-4.1-mini"),
    ("gpt-4.1-nano", "gpt-4.1-nano"),
    ("gpt-4.1", "gpt-4.1"),
]


def _normalize_model_name(model: str | None) -> str:
    return (model or "").strip().lower()


def _candidate_names(model: str | None) -> list[str]:
    raw = _normalize_model_name(model)
    if not raw:
        return []

    parts: set[str] = {raw}
    for sep in ("/", ":"):
        for item in list(parts):
            if sep in item:
                parts.add(item.split(sep)[-1])

    names: list[str] = []
    for item in parts:
        if item not in names:
            names.append(item)
    return names


def _resolve_price(model: str | None) -> tuple[str | None, _ModelPrice | None]:
    for name in _candidate_names(model):
        exact = _MODEL_PRICES.get(name)
        if exact is not None:
            return name, exact

        for prefix, canonical in _ALIAS_PREFIXES:
            if name.startswith(prefix):
                return canonical, _MODEL_PRICES[canonical]
    return None, None


def estimate_usage_cost(
    model: str | None,
    input_tokens: int | float | None,
    output_tokens: int | float | None,
) -> dict[str, Any]:
    in_tokens = max(0, int(input_tokens or 0))
    out_tokens = max(0, int(output_tokens or 0))
    pricing_model, price = _resolve_price(model)

    if price is None:
        return {
            "estimated_cost_usd": 0.0,
            "pricing_known": False,
            "pricing_model": pricing_model,
            "input_price_per_1m": None,
            "output_price_per_1m": None,
        }

    cost = (in_tokens / 1_000_000.0) * price.input_per_1m + (out_tokens / 1_000_000.0) * price.output_per_1m
    return {
        "estimated_cost_usd": round(cost, 8),
        "pricing_known": True,
        "pricing_model": pricing_model,
        "input_price_per_1m": price.input_per_1m,
        "output_price_per_1m": price.output_per_1m,
    }
