"""
Post-fill hedging logic.
When one side fills, place the hedge on the opposite side.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from data.models import Market, MarketOrderBooks, Side
from strategy.quoter import Quoter
from observability import logger as log


@dataclass
class HedgeAction:
    market: Market
    side: Side  # The side to hedge (opposite the fill)
    token_id: str
    price: float
    size: float
    valid: bool = True
    reason: str = ""


class Hedger:
    """Handle post-fill hedge logic."""

    def __init__(self, quoter: Quoter):
        self.quoter = quoter

    def compute_hedge(
        self,
        market: Market,
        books: MarketOrderBooks,
        filled_side: Side,
        filled_price: float,
        filled_size: float,
    ) -> HedgeAction:
        """
        Compute the hedge action after a fill.

        If YES filled at 49c, hedge using the real NO book.
        The hedge price must remain within MAX_SUM while using the opposite side of the market.
        """
        # Determine the opposite side
        hedge_side = Side.NO if filled_side == Side.YES else Side.YES

        return self.compute_target_hedge(
            market=market,
            books=books,
            hedge_side=hedge_side,
            reference_price=filled_price,
            hedge_size=filled_size,
        )

    def compute_target_hedge(
        self,
        market: Market,
        books: MarketOrderBooks,
        hedge_side: Side,
        reference_price: float,
        hedge_size: float,
    ) -> HedgeAction:
        """Compute an explicit hedge for a target side and residual size."""
        # Token ID for the hedge
        if hedge_side == Side.YES:
            token_id = market.token_id_yes
        else:
            token_id = market.token_id_no

        if not token_id:
            return HedgeAction(
                market=market, side=hedge_side, token_id="",
                price=0, size=0, valid=False,
                reason="missing token_id for hedge"
            )

        hedge_book = books.book_for_side(hedge_side)
        hedge_price = self.quoter.compute_hedge_price(hedge_book, reference_price)

        if hedge_price is None:
            return HedgeAction(
                market=market, side=hedge_side, token_id=token_id,
                price=0, size=0, valid=False,
                reason=f"unable to compute hedge price from {hedge_side.value} book"
            )

        sum_check = (reference_price + hedge_price) * 100
        book_best_ask = hedge_book.best_yes_ask
        ask_hint = f" ask={book_best_ask*100:.0f}c" if book_best_ask is not None else ""
        log.hedg(f"computing hedge {hedge_side.value} = {hedge_price*100:.0f}c{ask_hint} "
                 f"total={sum_check:.0f}c <= {self.quoter.config.max_sum_cents:.0f} "
                 f"{'OK' if sum_check <= self.quoter.config.max_sum_cents else 'OVER'}")

        return HedgeAction(
            market=market,
            side=hedge_side,
            token_id=token_id,
            price=hedge_price,
            size=hedge_size,
            valid=True,
        )
