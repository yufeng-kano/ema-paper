"""Credit arithmetic (docs/billing.md § Credits): what a parse and a turn cost, from the
operator's list prices and the worth of one credit. Pure functions; the ledger that
records the results is `billing.ledger` (a later step of docs/billing.md § Transition).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
PRICE_FIELDS = ("input", "cache_read", "cache_write", "output")
# What an entry may carry beside the prices: the model's context window, in tokens — what
# the chat panel's ring measures a conversation against (docs/chat.md § Compaction).
CONTEXT_FIELD = "context"


@dataclass(frozen=True)
class ModelPrice:
    """List price of one model, USD per million tokens of each kind."""

    input: Decimal
    cache_read: Decimal
    cache_write: Decimal
    output: Decimal
    # The model's context window in tokens, when the operator named one; None otherwise.
    context: int | None = None

    def usd(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> Decimal:
        """The list-price cost of one call's usage. `prompt_tokens` is the uncached input,
        as `usage_log` records it (docs/database.md)."""
        return (
            self.input * prompt_tokens
            + self.cache_read * cache_read_tokens
            + self.cache_write * cache_write_tokens
            + self.output * completion_tokens
        ) / TOKENS_PER_PRICE_UNIT

    def per_thousand(self, credit_usd: Decimal) -> dict[str, Decimal]:
        """Credits per 1K tokens of each kind — what the web app shows as the rate."""
        return {
            field: getattr(self, field) * 1000 / TOKENS_PER_PRICE_UNIT / credit_usd
            for field in PRICE_FIELDS
        }


def parse_model_prices(raw: str) -> dict[str, ModelPrice]:
    """`MODEL_PRICES_JSON`: `{"<model>": {"input": …, "cache_read": …, "cache_write": …,
    "output": …, "context": …}}`, every price a non-negative number and `context` — the
    model's context window in tokens — an optional positive integer. Anything else is
    refused whole, so a typo cannot leave one model priced at nothing."""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"MODEL_PRICES_JSON is not JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("MODEL_PRICES_JSON must be an object keyed by model id")
    prices: dict[str, ModelPrice] = {}
    for model, entry in data.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError("MODEL_PRICES_JSON: a model id must be a non-empty string")
        if not isinstance(entry, dict) or set(entry) - {CONTEXT_FIELD} != set(PRICE_FIELDS):
            raise ValueError(
                f"MODEL_PRICES_JSON: {model!r} must carry exactly {', '.join(PRICE_FIELDS)}"
                f" (and may carry {CONTEXT_FIELD})"
            )
        context = entry.get(CONTEXT_FIELD)
        if context is not None and (
            isinstance(context, bool) or not isinstance(context, int) or context <= 0
        ):
            raise ValueError(
                f"MODEL_PRICES_JSON: {model!r}.{CONTEXT_FIELD} must be a positive integer"
            )
        values: dict[str, Decimal] = {}
        for field in PRICE_FIELDS:
            value = entry[field]
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                raise ValueError(f"MODEL_PRICES_JSON: {model!r}.{field} must be a number")
            try:
                price = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(f"MODEL_PRICES_JSON: {model!r}.{field} must be a number") from exc
            if not price.is_finite() or price < 0:
                raise ValueError(f"MODEL_PRICES_JSON: {model!r}.{field} must be >= 0")
            values[field] = price
        prices[model.strip()] = ModelPrice(**values, context=context)
    return prices


def parse_credits(pages: int, per_page: int) -> int:
    """A parse costs a flat rate per page it parses (docs/billing.md § A parse)."""
    if pages < 0 or per_page < 0:
        raise ValueError("pages and the rate are counts")
    return pages * per_page


def credits_for_usd(usd: Decimal, credit_usd: Decimal, *, minimum: int = 1) -> int:
    """Whole credits for a cost, rounded up — once, over whatever the caller summed — and
    never under `minimum` (docs/billing.md § A chat turn)."""
    if credit_usd <= 0:
        raise ValueError("a credit must be worth something")
    if usd < 0:
        raise ValueError("a cost is not negative")
    return max(minimum, math.ceil(usd / credit_usd))
