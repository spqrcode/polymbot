"""
Quote generator: computes the YES and NO prices to place.
Strategy: spread lock with the constraint sum <= MAX_SUM.
"""

from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Optional

from config.settings import TradingConfig
from data.models import MarketOrderBooks, OrderBook, Side
from observability import logger as log


@dataclass
class Quote:
    yes_price: float  # YES price in dollars
    no_price: float   # Prezzo NO in dollari
    size: float       # Order size
    valid: bool = True
    reason: str = ""

    @property
    def yes_cents(self) -> float:
        return self.yes_price * 100

    @property
    def no_cents(self) -> float:
        return self.no_price * 100

    @property
    def sum_cents(self) -> float:
        return (self.yes_price + self.no_price) * 100


class Quoter:
    """Compute optimal quotes for liquidity provision."""

    def __init__(self, config: TradingConfig):
        self.config = config

    def compute_quotes(
        self,
        books: MarketOrderBooks,
        net_exposure: float = 0.0,
    ) -> Quote:
        """
        Generate a YES/NO quote pair based on the market's real YES and NO books.

        Logic:
        1. Read the real YES and NO mid/spread
        2. Place passive bids on both sides without using the theoretical complement
        3. Apply inventory skew toward the side that reduces risk
        4. Ensure YES + NO <= MAX_SUM
        5. Adjust if needed

        net_exposure: positive = long YES, negative = long NO.
        Used to skew quotes toward hedging.
        """
        book_age_sec = self._max_book_age_seconds(books)
        if book_age_sec is not None and book_age_sec > self.config.max_book_age_sec:
            return Quote(
                0,
                0,
                0,
                valid=False,
                reason=(
                    f"book stale {book_age_sec:.1f}s > max "
                    f"{self.config.max_book_age_sec:.1f}s"
                ),
            )

        yes_mid = books.yes_mid_price
        no_mid = books.no_mid_price
        min_spread_cents = books.min_spread_cents

        if yes_mid is None or no_mid is None:
            return Quote(0, 0, 0, valid=False, reason="incomplete YES/NO books")

        if min_spread_cents is None or (min_spread_cents + 1e-9) < self.config.min_spread_cents:
            return Quote(0, 0, 0, valid=False,
                         reason=f"minimum spread {min_spread_cents or 0:.1f}c < min {self.config.min_spread_cents}c")

        offset = self.config.quote_offset_cents / 100  # Convert to dollars

        # Inventory skew: if we are exposed on one side, skew toward the other
        skew = self._compute_skew(net_exposure)

        yes_price = self._quote_passive_bid(books.yes_book, desired_price=yes_mid - offset - skew)
        no_price = self._quote_passive_bid(books.no_book, desired_price=no_mid - offset + skew)

        if yes_price is None or no_price is None:
            return Quote(0, 0, 0, valid=False, reason="unable to compute passive quotes")

        # Round to cents
        yes_price = round(yes_price, 2)
        no_price = round(no_price, 2)

        # Minimum price constraint
        yes_price = max(yes_price, 0.01)
        no_price = max(no_price, 0.01)

        # Check sum
        sum_cents = (yes_price + no_price) * 100
        max_sum = self.config.max_sum_cents

        if sum_cents > max_sum:
            # Reduce proportionally
            excess = (sum_cents - max_sum) / 100
            yes_price -= excess / 2
            no_price -= excess / 2
            yes_price = round(max(yes_price, 0.01), 2)
            no_price = round(max(no_price, 0.01), 2)

        # Validate range
        if yes_price < self.config.price_range_min or yes_price > self.config.price_range_max:
            return Quote(0, 0, 0, valid=False,
                         reason=f"YES price {yes_price:.2f} outside range")

        if no_price < self.config.price_range_min or no_price > self.config.price_range_max:
            # NO price outside range is acceptable if the market is heavily imbalanced
            pass

        size = self.config.order_size

        return Quote(
            yes_price=yes_price,
            no_price=no_price,
            size=size,
            valid=True,
        )

    def _compute_skew(self, net_exposure: float) -> float:
        """
        Compute skew based on net exposure.
        Positive = move quotes toward buying more NO (hedging YES exposure).
        """
        if abs(net_exposure) < 0.01:
            return 0.0

        # Linear skew: 0.5c for every $1 of exposure
        skew = net_exposure * 0.005
        # Cap skew at 2c
        skew = max(-0.02, min(0.02, skew))
        return skew

    def compute_hedge_price(
        self,
        hedge_book: OrderBook,
        filled_price: float,
    ) -> Optional[float]:
        """
        Compute the hedge price using the real book on the opposite side.
        If the best ask is below the allowed cap, use it.
        Otherwise, stop at the cap to avoid breaking MAX_SUM.
        """
        max_sum = self.config.max_sum_cents / 100  # In dollars
        max_hedge_price = max_sum - filled_price

        if max_hedge_price <= 0.01:
            return None

        best_ask = hedge_book.best_yes_ask
        if best_ask is None:
            best_bid = hedge_book.best_yes_bid
            if best_bid is None:
                return None
            return round(min(max_hedge_price, best_bid), 2)

        hedge_price = min(max_hedge_price, best_ask)
        return round(min(hedge_price, 0.99), 2)

    def _quote_passive_bid(self, book: OrderBook, desired_price: float) -> Optional[float]:
        best_bid = book.best_yes_bid
        best_ask = book.best_yes_ask
        if best_bid is None or best_ask is None:
            return None

        tick = 0.01
        # Join the top-of-book queue: if desired_price is below best_bid,
        # move up to best_bid to sit at the front of the fill queue
        price = max(desired_price, best_bid)
        # Never cross the best ask
        price = min(price, best_ask - tick)
        if price < 0.01:
            return None

        return max(0.01, price)

    def _max_book_age_seconds(self, books: MarketOrderBooks) -> Optional[float]:
        now = time.time()
        ages = []
        for book in (books.yes_book, books.no_book):
            if book.best_yes_bid is None and book.best_yes_ask is None:
                continue
            ages.append(max(0.0, now - book.timestamp))
        if not ages:
            return None
        return max(ages)
