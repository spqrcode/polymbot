"""Filters for selecting markets to trade."""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data.models import Market
    from config.settings import TradingConfig


@dataclass
class FilterResult:
    passed: bool
    reason: str = ""


def filter_by_spread(market: "Market", min_spread_cents: float) -> FilterResult:
    """Filter markets with too little spread."""
    if market.spread_cents is None:
        return FilterResult(False, "spread unavailable")
    if market.spread_cents < min_spread_cents:
        return FilterResult(False, f"spread {market.spread_cents:.1f}c < min {min_spread_cents:.1f}c")
    return FilterResult(True)


def filter_by_price_range(market: "Market", price_min: float, price_max: float) -> FilterResult:
    """Filter markets whose mid price is outside the allowed range."""
    if market.mid_price is None:
        return FilterResult(False, "mid price unavailable")
    if market.mid_price < price_min or market.mid_price > price_max:
        return FilterResult(False, f"mid {market.mid_price:.2f} outside range [{price_min:.2f}, {price_max:.2f}]")
    return FilterResult(True)


def filter_by_competition(market: "Market", max_competition: str) -> FilterResult:
    """Filter markets that are too competitive."""
    levels = {"low": 0, "medium": 1, "high": 2}
    market_level = levels.get(market.competition, 1)
    max_level = levels.get(max_competition, 0)
    if market_level > max_level:
        return FilterResult(False, f"competition {market.competition} > {max_competition}")
    return FilterResult(True)


def filter_by_active(market: "Market") -> FilterResult:
    """Filter inactive markets."""
    if not market.active:
        return FilterResult(False, "market is not active")
    return FilterResult(True)


def apply_all_filters(market: "Market", config: "TradingConfig") -> FilterResult:
    """Apply all filters in sequence and stop at the first failure."""
    filters = [
        lambda m: filter_by_active(m),
        lambda m: filter_by_spread(m, config.min_spread_cents),
        lambda m: filter_by_price_range(m, config.price_range_min, config.price_range_max),
        lambda m: filter_by_competition(m, config.competition),
    ]
    for f in filters:
        result = f(market)
        if not result.passed:
            return result
    return FilterResult(True)
