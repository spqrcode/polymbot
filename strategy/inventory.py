"""Tracking for positions and net exposure by market."""

from __future__ import annotations
from typing import Optional

from data.models import MarketOrderBooks, Position, PositionStatus, Side, TradeRecord
from observability import logger as log


class InventoryManager:
    """Manage open positions and track exposure."""

    def __init__(self, max_per_market: float, max_capital: float):
        self.max_per_market = max_per_market
        self.max_capital = max_capital
        self._positions: dict[str, Position] = {}  # market_id -> Position
        self._trades: list[TradeRecord] = []

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    @property
    def total_exposure(self) -> float:
        """Total exposure in dollars."""
        return sum(p.total_cost for p in self._positions.values()
                   if p.status != PositionStatus.CLOSED)

    @property
    def active_positions_count(self) -> int:
        return sum(1 for p in self._positions.values()
                   if p.status != PositionStatus.CLOSED)

    def get_position(self, market_id: str) -> Optional[Position]:
        return self._positions.get(market_id)

    def get_market_exposure(self, market_id: str) -> float:
        pos = self._positions.get(market_id)
        return pos.total_cost if pos else 0.0

    def get_net_exposure(self, market_id: str) -> float:
        """
        Net exposure for a market.
        Positive = long YES, negative = long NO.
        """
        pos = self._positions.get(market_id)
        if not pos:
            return 0.0
        yes_value = pos.yes_price * pos.yes_size
        no_value = pos.no_price * pos.no_size
        return yes_value - no_value

    def get_required_hedge(self, market_id: str) -> tuple[Optional[Side], float]:
        """
        Return the side and size still needed to hedge the position.
        If we are long YES, we need a NO hedge; if long NO, we need a YES hedge.
        """
        pos = self._positions.get(market_id)
        if not pos or pos.status == PositionStatus.CLOSED:
            return None, 0.0

        if pos.yes_size > pos.no_size:
            return Side.NO, pos.yes_size - pos.no_size
        if pos.no_size > pos.yes_size:
            return Side.YES, pos.no_size - pos.yes_size
        return None, 0.0

    def get_reference_price_for_hedge(self, market_id: str, hedge_side: Side) -> Optional[float]:
        """
        Average price of the already-exposed side we are trying to hedge.
        If we need to hedge NO, the reference is the average YES price, and vice versa.
        """
        pos = self._positions.get(market_id)
        if not pos:
            return None

        if hedge_side == Side.NO:
            return pos.yes_price if pos.yes_size > 0 else None
        return pos.no_price if pos.no_size > 0 else None

    def can_open_position(self, market_id: str, additional_cost: float) -> bool:
        """Check whether we can open or extend a position."""
        current_pos = self._positions.get(market_id)
        current_cost = current_pos.total_cost if current_pos else 0.0

        if current_cost + additional_cost > self.max_per_market:
            return False

        if self.total_exposure + additional_cost > self.max_capital:
            return False

        return True

    def restore_market_side(
        self,
        market_id: str,
        side: Side,
        size: float,
        price: float,
        question: str = "",
    ) -> None:
        """Restore an existing position without counting new session PnL."""
        if size <= 0:
            return

        if market_id not in self._positions:
            self._positions[market_id] = Position(
                market_id=market_id,
                question=question,
            )

        pos = self._positions[market_id]
        if question and not pos.question:
            pos.question = question

        if side == Side.YES:
            total_yes = pos.yes_size + size
            if total_yes > 0:
                pos.yes_price = (pos.yes_price * pos.yes_size + price * size) / total_yes
            pos.yes_size = total_yes
        else:
            total_no = pos.no_size + size
            if total_no > 0:
                pos.no_price = (pos.no_price * pos.no_size + price * size) / total_no
            pos.no_size = total_no

        pos.hedged_size = min(pos.yes_size, pos.no_size)
        if pos.yes_size > 0 and pos.no_size > 0:
            pos.status = PositionStatus.HEDGED
        elif pos.yes_size > 0 or pos.no_size > 0:
            pos.status = PositionStatus.COLLECTING
        pos.unhedged_scan_cycles = 0

    def record_fill(self, market_id: str, side: Side, price: float,
                    size: float, question: str = "", is_hedge: bool = False) -> float:
        """
        Record a fill and update the position.
        Return incremental realized PnL when a new YES+NO hedge pair forms.
        """
        if market_id not in self._positions:
            self._positions[market_id] = Position(
                market_id=market_id,
                question=question,
            )

        pos = self._positions[market_id]

        if side == Side.YES:
            # Weighted average if we already hold YES
            total_yes = pos.yes_size + size
            if total_yes > 0:
                pos.yes_price = (pos.yes_price * pos.yes_size + price * size) / total_yes
            pos.yes_size = total_yes
        else:
            total_no = pos.no_size + size
            if total_no > 0:
                pos.no_price = (pos.no_price * pos.no_size + price * size) / total_no
            pos.no_size = total_no

        # Update status
        if pos.yes_size > 0 and pos.no_size > 0:
            pos.status = PositionStatus.HEDGED
        elif pos.yes_size > 0 or pos.no_size > 0:
            pos.status = PositionStatus.COLLECTING
        pos.unhedged_scan_cycles = 0

        # Record trade
        self._trades.append(TradeRecord(
            market_id=market_id,
            side=side,
            price=price,
            size=size,
            is_hedge=is_hedge,
        ))

        tag = "HEDG" if is_hedge else "FILL"
        log_fn = log.hedg if is_hedge else log.fill
        log_fn(f"{side.value} filled @ {price*100:.0f}c x{size:.1f} | "
               f"pos Y:{pos.yes_price_cents:.0f}c N:{pos.no_price_cents:.0f}c "
               f"sum={pos.sum_cents:.0f}c [{pos.status.value}]")

        # Realized PnL on the newly hedged portion:
        # every YES+NO pair pays $1 at resolution, so edge = 1 - (YES + NO)
        hedgeable_size = min(pos.yes_size, pos.no_size)
        newly_hedged = max(0.0, hedgeable_size - pos.hedged_size)
        if newly_hedged <= 0:
            return 0.0

        pnl_delta = newly_hedged * (1.0 - (pos.yes_price + pos.no_price))
        pos.hedged_size += newly_hedged
        pos.pnl += pnl_delta
        return pnl_delta

    def close_position(self, market_id: str, resolved_side: Optional[Side],
                       payout_per_share: float = 1.0) -> float:
        """
        Close a resolved position.
        Return only the incremental PnL still to be accounted for at close.
        If the market resolves YES, YES shares pay $1 and NO shares pay $0, and vice versa.
        """
        pos = self._positions.get(market_id)
        if not pos:
            return 0.0

        if resolved_side is None:
            log.warn(f"skip close {pos.question[:30]}...: outcome could not be determined")
            return 0.0

        hedged_size = min(pos.hedged_size, pos.yes_size, pos.no_size)
        residual_yes = max(0.0, pos.yes_size - hedged_size)
        residual_no = max(0.0, pos.no_size - hedged_size)
        pnl_delta = 0.0

        if resolved_side == Side.YES:
            pnl_delta = residual_yes * (payout_per_share - pos.yes_price) - (residual_no * pos.no_price)
        elif resolved_side == Side.NO:
            pnl_delta = residual_no * (payout_per_share - pos.no_price) - (residual_yes * pos.yes_price)

        pos.pnl += pnl_delta
        pos.status = PositionStatus.CLOSED

        log.clos(f"resolved {pos.question[:30]}... -> {resolved_side.value if resolved_side else '?'} "
                 f"PnL: {'+'if pos.pnl>=0 else ''}{pos.pnl:.2f}")

        return pnl_delta

    def get_all_active(self) -> list[Position]:
        """Return all open positions."""
        return [p for p in self._positions.values()
                if p.status != PositionStatus.CLOSED]

    def note_unhedged_scan(self, market_id: str, is_unhedged: bool) -> int:
        """Update the counter of consecutive scans with an unhedged position."""
        pos = self._positions.get(market_id)
        if pos is None:
            return 0

        if not is_unhedged:
            pos.unhedged_scan_cycles = 0
            return 0

        pos.unhedged_scan_cycles += 1
        return pos.unhedged_scan_cycles

    def get_position_unrealized_pnl(
        self,
        market_id: str,
        books: Optional[MarketOrderBooks],
    ) -> float:
        """Mark-to-market only the still-unhedged leg for a market."""
        pos = self._positions.get(market_id)
        if pos is None or pos.status == PositionStatus.CLOSED or books is None:
            return 0.0
        if books.yes_mid_price is None or books.no_mid_price is None:
            return 0.0

        residual_yes = max(0.0, pos.yes_size - pos.hedged_size)
        residual_no = max(0.0, pos.no_size - pos.hedged_size)

        unrealized = 0.0
        unrealized += residual_yes * (books.yes_mid_price - pos.yes_price)
        unrealized += residual_no * (books.no_mid_price - pos.no_price)
        return unrealized

    def get_display_data(self) -> list[dict]:
        """Dati per la dashboard."""
        data = []
        for pos in self._positions.values():
            if pos.status == PositionStatus.CLOSED:
                continue
            data.append({
                "market": pos.question[:35],
                "yes": f"{pos.yes_price_cents:.0f}c",
                "no": f"{pos.no_price_cents:.0f}c",
                "status": pos.status.value,
                "cost": f"${pos.total_cost:.2f}",
            })
        return data

    def get_unrealized_pnl(self, market_books_by_id: dict[str, MarketOrderBooks]) -> float:
        """
        Mark-to-market solo sulla parte non ancora coperta, per evitare doppio conteggio
        del PnL gia' lockato sulle coppie YES+NO.
        """
        unrealized = 0.0
        for market_id, pos in self._positions.items():
            if pos.status == PositionStatus.CLOSED:
                continue

            books = market_books_by_id.get(market_id)
            unrealized += self.get_position_unrealized_pnl(market_id, books)

        return unrealized
