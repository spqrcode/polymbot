"""
Per-market KPI tracker and hedge-path telemetry.
Writes an updated JSON snapshot on every scan for runtime/live-readiness analysis.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from data.models import Market, MarketOrderBooks, Order, PositionStatus, Side
from strategy.inventory import InventoryManager


@dataclass
class _SampleStats:
    count: int = 0
    total: float = 0.0
    min_value: float = 0.0
    max_value: float = 0.0
    last_value: float = 0.0

    def record(self, value: float) -> None:
        current = float(value)
        self.count += 1
        self.total += current
        self.last_value = current
        if self.count == 1 or current < self.min_value:
            self.min_value = current
        if current > self.max_value:
            self.max_value = current

    def snapshot(self) -> dict[str, float | int]:
        avg = 0.0 if self.count == 0 else self.total / self.count
        return {
            "count": self.count,
            "avg": round(avg, 3),
            "last": round(self.last_value, 3),
            "min": round(self.min_value, 3),
            "max": round(self.max_value, 3),
        }


@dataclass
class _PendingHedgePath:
    market_id: str
    question: str
    started_at: float
    filled_side: str
    hedge_side: str
    reference_fill_price: float
    required_size: float
    initial_reference_price: Optional[float] = None
    initial_best_bid: Optional[float] = None
    initial_best_ask: Optional[float] = None
    initial_book_age_ms: float = 0.0
    last_submit_at: Optional[float] = None
    last_submit_price: Optional[float] = None
    last_submit_size: float = 0.0
    last_queue_ahead_size: float = 0.0
    last_queue_levels_ahead: int = 0
    last_queue_gap_cents: float = 0.0
    last_order_id: str = ""

    def snapshot(self) -> dict[str, object]:
        return {
            "started_at_epoch": round(self.started_at, 3),
            "filled_side": self.filled_side,
            "hedge_side": self.hedge_side,
            "reference_fill_price_cents": round(self.reference_fill_price * 100.0, 3),
            "required_size": round(self.required_size, 6),
            "initial_reference_cents": None if self.initial_reference_price is None else round(self.initial_reference_price * 100.0, 3),
            "initial_best_bid_cents": None if self.initial_best_bid is None else round(self.initial_best_bid * 100.0, 3),
            "initial_best_ask_cents": None if self.initial_best_ask is None else round(self.initial_best_ask * 100.0, 3),
            "initial_book_age_ms": round(self.initial_book_age_ms, 3),
            "last_submit_at_epoch": None if self.last_submit_at is None else round(self.last_submit_at, 3),
            "last_submit_price_cents": None if self.last_submit_price is None else round(self.last_submit_price * 100.0, 3),
            "last_submit_size": round(self.last_submit_size, 6),
            "last_queue_ahead_size": round(self.last_queue_ahead_size, 6),
            "last_queue_levels_ahead": self.last_queue_levels_ahead,
            "last_queue_gap_cents": round(self.last_queue_gap_cents, 3),
            "last_order_id": self.last_order_id,
        }


@dataclass
class _MarketKPI:
    market_id: str
    question: str = ""
    entry_yes_fills: int = 0
    entry_no_fills: int = 0
    hedge_yes_fills: int = 0
    hedge_no_fills: int = 0
    hedge_orders_submitted: int = 0
    pair_completion_via_hedge: int = 0
    pair_completion_via_entry_bypass: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    status: str = "idle"
    unhedged_cycles: int = 0
    required_hedge_side: Optional[str] = None
    required_hedge_size: float = 0.0
    last_book_age_ms: float = 0.0
    fill_to_hedge_submit_ms: _SampleStats = field(default_factory=_SampleStats)
    hedge_submit_to_fill_ms: _SampleStats = field(default_factory=_SampleStats)
    unhedged_window_ms: _SampleStats = field(default_factory=_SampleStats)
    hedge_slippage_cents: _SampleStats = field(default_factory=_SampleStats)
    adverse_move_cents: _SampleStats = field(default_factory=_SampleStats)
    queue_ahead_size: _SampleStats = field(default_factory=_SampleStats)
    queue_levels_ahead: _SampleStats = field(default_factory=_SampleStats)
    queue_gap_cents: _SampleStats = field(default_factory=_SampleStats)
    pending_hedge: Optional[_PendingHedgePath] = None

    def snapshot(self) -> dict[str, object]:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "entry_yes_fills": self.entry_yes_fills,
            "entry_no_fills": self.entry_no_fills,
            "hedge_yes_fills": self.hedge_yes_fills,
            "hedge_no_fills": self.hedge_no_fills,
            "entry_fill_total": self.entry_yes_fills + self.entry_no_fills,
            "hedge_fill_total": self.hedge_yes_fills + self.hedge_no_fills,
            "hedge_orders_submitted": self.hedge_orders_submitted,
            "pair_completion_via_hedge": self.pair_completion_via_hedge,
            "pair_completion_via_entry_bypass": self.pair_completion_via_entry_bypass,
            "realized_pnl": round(self.realized_pnl, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "status": self.status,
            "unhedged_cycles": self.unhedged_cycles,
            "required_hedge_side": self.required_hedge_side,
            "required_hedge_size": round(self.required_hedge_size, 6),
            "last_book_age_ms": round(self.last_book_age_ms, 3),
            "stats": {
                "fill_to_hedge_submit_ms": self.fill_to_hedge_submit_ms.snapshot(),
                "hedge_submit_to_fill_ms": self.hedge_submit_to_fill_ms.snapshot(),
                "unhedged_window_ms": self.unhedged_window_ms.snapshot(),
                "hedge_slippage_cents": self.hedge_slippage_cents.snapshot(),
                "adverse_move_cents": self.adverse_move_cents.snapshot(),
                "queue_ahead_size": self.queue_ahead_size.snapshot(),
                "queue_levels_ahead": self.queue_levels_ahead.snapshot(),
                "queue_gap_cents": self.queue_gap_cents.snapshot(),
            },
            "pending_hedge": None if self.pending_hedge is None else self.pending_hedge.snapshot(),
        }


class MarketMetricsTracker:
    def __init__(self, report_path: Path, mode_label: str):
        self.report_path = report_path
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.mode_label = mode_label
        self._markets: dict[str, _MarketKPI] = {}
        self._pending_by_order_id: dict[str, str] = {}
        self._global_fill_to_submit_ms = _SampleStats()
        self._global_submit_to_fill_ms = _SampleStats()
        self._global_unhedged_window_ms = _SampleStats()
        self._global_slippage_cents = _SampleStats()
        self._global_adverse_move_cents = _SampleStats()
        self._global_queue_ahead_size = _SampleStats()
        self._global_queue_levels_ahead = _SampleStats()
        self._global_queue_gap_cents = _SampleStats()

    def record_entry_fill(
        self,
        fill: Order,
        market: Optional[Market],
        required_hedge_side: Optional[Side],
        required_hedge_size: float,
        books: Optional[MarketOrderBooks],
        pnl_delta: float,
    ) -> Optional[dict[str, float]]:
        market_id = fill.market_id
        record = self._ensure_record(market_id, market.question if market else "")
        if fill.side == Side.YES:
            record.entry_yes_fills += 1
        else:
            record.entry_no_fills += 1
        record.realized_pnl += pnl_delta

        if required_hedge_side is not None and required_hedge_size > 1e-9:
            hedge_book = books.book_for_side(required_hedge_side) if books is not None else None
            pending = record.pending_hedge
            if pending is None or pending.hedge_side != required_hedge_side.value:
                record.pending_hedge = _PendingHedgePath(
                    market_id=market_id,
                    question=record.question,
                    started_at=fill.filled_at or time.time(),
                    filled_side=fill.side.value,
                    hedge_side=required_hedge_side.value,
                    reference_fill_price=fill.price,
                    required_size=required_hedge_size,
                    initial_reference_price=self._book_reference_price(hedge_book),
                    initial_best_bid=None if hedge_book is None else hedge_book.best_yes_bid,
                    initial_best_ask=None if hedge_book is None else hedge_book.best_yes_ask,
                    initial_book_age_ms=self._book_age_ms(hedge_book),
                )
            else:
                pending.required_size = required_hedge_size
            return None

        pending = record.pending_hedge
        if pending is None or pnl_delta == 0:
            return None

        window_ms = max(0.0, ((fill.filled_at or time.time()) - pending.started_at) * 1000.0)
        adverse_move_cents = self._adverse_move_cents(
            pending,
            books.book_for_side(Side(pending.hedge_side)) if books is not None else None,
        )
        record.pair_completion_via_entry_bypass += 1
        record.unhedged_window_ms.record(window_ms)
        record.adverse_move_cents.record(adverse_move_cents)
        self._global_unhedged_window_ms.record(window_ms)
        self._global_adverse_move_cents.record(adverse_move_cents)
        record.pending_hedge = None
        return {
            "unhedged_window_ms": window_ms,
            "adverse_move_cents": adverse_move_cents,
        }

    def record_hedge_submit(
        self,
        market: Market,
        hedge_side: Side,
        order_id: str,
        price: float,
        size: float,
        books: Optional[MarketOrderBooks],
    ) -> dict[str, float]:
        record = self._ensure_record(market.condition_id, market.question)
        pending = record.pending_hedge
        if pending is None:
            hedge_book = books.book_for_side(hedge_side) if books is not None else None
            pending = _PendingHedgePath(
                market_id=market.condition_id,
                question=market.question,
                started_at=time.time(),
                filled_side=Side.NO.value if hedge_side == Side.YES else Side.YES.value,
                hedge_side=hedge_side.value,
                reference_fill_price=0.0,
                required_size=size,
                initial_reference_price=self._book_reference_price(hedge_book),
                initial_best_bid=None if hedge_book is None else hedge_book.best_yes_bid,
                initial_best_ask=None if hedge_book is None else hedge_book.best_yes_ask,
                initial_book_age_ms=self._book_age_ms(hedge_book),
            )
            record.pending_hedge = pending

        queue_est = self._estimate_queue_ahead(
            books.book_for_side(hedge_side) if books is not None else None,
            price,
        )
        pending.last_submit_at = time.time()
        pending.last_submit_price = price
        pending.last_submit_size = size
        pending.required_size = size
        pending.last_queue_ahead_size = queue_est["ahead_size"]
        pending.last_queue_levels_ahead = queue_est["levels_ahead"]
        pending.last_queue_gap_cents = queue_est["gap_cents"]
        pending.last_order_id = order_id

        record.hedge_orders_submitted += 1
        record.queue_ahead_size.record(queue_est["ahead_size"])
        record.queue_levels_ahead.record(queue_est["levels_ahead"])
        record.queue_gap_cents.record(queue_est["gap_cents"])
        self._global_queue_ahead_size.record(queue_est["ahead_size"])
        self._global_queue_levels_ahead.record(queue_est["levels_ahead"])
        self._global_queue_gap_cents.record(queue_est["gap_cents"])
        self._pending_by_order_id[order_id] = market.condition_id

        fill_to_submit_ms = max(0.0, (pending.last_submit_at - pending.started_at) * 1000.0)
        record.fill_to_hedge_submit_ms.record(fill_to_submit_ms)
        self._global_fill_to_submit_ms.record(fill_to_submit_ms)

        return {
            "fill_to_hedge_submit_ms": fill_to_submit_ms,
            "queue_ahead_size": queue_est["ahead_size"],
            "queue_levels_ahead": float(queue_est["levels_ahead"]),
            "queue_gap_cents": queue_est["gap_cents"],
        }

    def record_hedge_fill(
        self,
        fill: Order,
        market: Optional[Market],
        required_hedge_side: Optional[Side],
        required_hedge_size: float,
        books: Optional[MarketOrderBooks],
        pnl_delta: float,
    ) -> Optional[dict[str, float]]:
        market_id = self._pending_by_order_id.get(fill.source_order_id or fill.order_id, fill.market_id)
        record = self._ensure_record(market_id, market.question if market else "")
        if fill.side == Side.YES:
            record.hedge_yes_fills += 1
        else:
            record.hedge_no_fills += 1
        record.realized_pnl += pnl_delta

        pending = record.pending_hedge
        if pending is None:
            return None

        filled_at = fill.filled_at or time.time()
        submit_to_fill_ms = None
        if pending.last_submit_at is not None:
            submit_to_fill_ms = max(0.0, (filled_at - pending.last_submit_at) * 1000.0)
            record.hedge_submit_to_fill_ms.record(submit_to_fill_ms)
            self._global_submit_to_fill_ms.record(submit_to_fill_ms)

        unhedged_window_ms = max(0.0, (filled_at - pending.started_at) * 1000.0)
        record.unhedged_window_ms.record(unhedged_window_ms)
        self._global_unhedged_window_ms.record(unhedged_window_ms)

        slippage_cents = self._slippage_from_initial_reference_cents(pending, fill.price)
        adverse_move_cents = self._adverse_move_cents(
            pending,
            books.book_for_side(Side(pending.hedge_side)) if books is not None else None,
        )
        record.hedge_slippage_cents.record(slippage_cents)
        record.adverse_move_cents.record(adverse_move_cents)
        self._global_slippage_cents.record(slippage_cents)
        self._global_adverse_move_cents.record(adverse_move_cents)

        if pnl_delta != 0:
            record.pair_completion_via_hedge += 1

        if required_hedge_side is None or required_hedge_size <= 1e-9:
            if pending.last_order_id:
                self._pending_by_order_id.pop(pending.last_order_id, None)
            record.pending_hedge = None
        else:
            pending.required_size = required_hedge_size

        payload = {
            "unhedged_window_ms": unhedged_window_ms,
            "hedge_slippage_cents": slippage_cents,
            "adverse_move_cents": adverse_move_cents,
        }
        if submit_to_fill_ms is not None:
            payload["hedge_submit_to_fill_ms"] = submit_to_fill_ms
        return payload

    def sync_positions(
        self,
        inventory: InventoryManager,
        books_by_market: dict[str, MarketOrderBooks],
    ) -> None:
        seen_market_ids = set()
        for market_id, position in inventory.positions.items():
            record = self._ensure_record(market_id, position.question)
            record.question = position.question or record.question
            record.realized_pnl = position.pnl
            record.status = position.status.value
            record.unhedged_cycles = position.unhedged_scan_cycles
            record.unrealized_pnl = inventory.get_position_unrealized_pnl(
                market_id,
                books_by_market.get(market_id),
            )
            required_side, required_size = inventory.get_required_hedge(market_id)
            record.required_hedge_side = None if required_side is None else required_side.value
            record.required_hedge_size = required_size
            record.last_book_age_ms = self._books_age_ms(books_by_market.get(market_id))
            seen_market_ids.add(market_id)

        for market_id, record in self._markets.items():
            if market_id in seen_market_ids:
                continue
            record.unrealized_pnl = 0.0
            if record.status == PositionStatus.CLOSED.value:
                continue
            if record.pending_hedge is None:
                record.status = "idle"
                record.unhedged_cycles = 0
                record.required_hedge_side = None
                record.required_hedge_size = 0.0
                record.last_book_age_ms = self._books_age_ms(books_by_market.get(market_id))

    def write_snapshot(self) -> None:
        payload = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode_label,
            "summary": {
                "markets_tracked": len(self._markets),
                "open_pending_hedges": sum(1 for record in self._markets.values() if record.pending_hedge is not None),
                "pair_completion_via_hedge": sum(record.pair_completion_via_hedge for record in self._markets.values()),
                "pair_completion_via_entry_bypass": sum(record.pair_completion_via_entry_bypass for record in self._markets.values()),
                "stats": {
                    "fill_to_hedge_submit_ms": self._global_fill_to_submit_ms.snapshot(),
                    "hedge_submit_to_fill_ms": self._global_submit_to_fill_ms.snapshot(),
                    "unhedged_window_ms": self._global_unhedged_window_ms.snapshot(),
                    "hedge_slippage_cents": self._global_slippage_cents.snapshot(),
                    "adverse_move_cents": self._global_adverse_move_cents.snapshot(),
                    "queue_ahead_size": self._global_queue_ahead_size.snapshot(),
                    "queue_levels_ahead": self._global_queue_levels_ahead.snapshot(),
                    "queue_gap_cents": self._global_queue_gap_cents.snapshot(),
                },
            },
            "markets": [
                record.snapshot()
                for _, record in sorted(self._markets.items(), key=lambda item: item[1].question or item[0])
            ],
        }
        self.report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _ensure_record(self, market_id: str, question: str) -> _MarketKPI:
        record = self._markets.get(market_id)
        if record is None:
            record = _MarketKPI(market_id=market_id, question=question)
            self._markets[market_id] = record
        elif question and not record.question:
            record.question = question
        return record

    def _estimate_queue_ahead(self, book, price: float) -> dict[str, float | int]:
        if book is None:
            return {
                "ahead_size": 0.0,
                "levels_ahead": 0,
                "gap_cents": 0.0,
            }

        ahead_size = 0.0
        levels_ahead = 0
        for level in book.yes_bids:
            if level.price > price + 1e-9:
                ahead_size += level.size
                levels_ahead += 1
                continue
            if abs(level.price - price) <= 1e-9:
                ahead_size += level.size
                levels_ahead += 1
            break

        best_bid = book.best_yes_bid or price
        gap_cents = max(0.0, (best_bid - price) * 100.0)
        return {
            "ahead_size": ahead_size,
            "levels_ahead": levels_ahead,
            "gap_cents": gap_cents,
        }

    def _books_age_ms(self, books: Optional[MarketOrderBooks]) -> float:
        if books is None:
            return 0.0
        now = time.time()
        ages = []
        for book in (books.yes_book, books.no_book):
            if book.best_yes_bid is None and book.best_yes_ask is None:
                continue
            ages.append(max(0.0, now - book.timestamp) * 1000.0)
        if not ages:
            return 0.0
        return max(ages)

    def _book_age_ms(self, book) -> float:
        if book is None:
            return 0.0
        if book.best_yes_bid is None and book.best_yes_ask is None:
            return 0.0
        return max(0.0, time.time() - book.timestamp) * 1000.0

    def _book_reference_price(self, book) -> Optional[float]:
        if book is None:
            return None
        return book.best_yes_ask if book.best_yes_ask is not None else book.best_yes_bid

    def _slippage_from_initial_reference_cents(self, pending: _PendingHedgePath, fill_price: float) -> float:
        if pending.initial_reference_price is None:
            return 0.0
        return (fill_price - pending.initial_reference_price) * 100.0

    def _adverse_move_cents(self, pending: _PendingHedgePath, book) -> float:
        current_reference = self._book_reference_price(book)
        if current_reference is None or pending.initial_reference_price is None:
            return 0.0
        return max(0.0, (current_reference - pending.initial_reference_price) * 100.0)
