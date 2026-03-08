"""Dataclasses for the bot data model."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class MarketStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class PositionStatus(str, Enum):
    COLLECTING = "collecting"
    HEDGED = "hedged"
    CLOSED = "closed"


@dataclass
class OrderBookLevel:
    price: float  # In dollars (0.xx)
    size: float


@dataclass
class OrderBook:
    yes_bids: list[OrderBookLevel] = field(default_factory=list)
    yes_asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def best_yes_bid(self) -> Optional[float]:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Optional[float]:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_yes_bid is not None and self.best_yes_ask is not None:
            return (self.best_yes_bid + self.best_yes_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_yes_bid is not None and self.best_yes_ask is not None:
            return self.best_yes_ask - self.best_yes_bid
        return None

    @property
    def spread_cents(self) -> Optional[float]:
        s = self.spread
        return s * 100 if s is not None else None


@dataclass
class MarketOrderBooks:
    yes_book: OrderBook = field(default_factory=OrderBook)
    no_book: OrderBook = field(default_factory=OrderBook)

    @property
    def yes_mid_price(self) -> Optional[float]:
        return self.yes_book.mid_price

    @property
    def no_mid_price(self) -> Optional[float]:
        return self.no_book.mid_price

    @property
    def yes_spread_cents(self) -> Optional[float]:
        return self.yes_book.spread_cents

    @property
    def no_spread_cents(self) -> Optional[float]:
        return self.no_book.spread_cents

    @property
    def min_spread_cents(self) -> Optional[float]:
        spreads = [spread for spread in (self.yes_spread_cents, self.no_spread_cents) if spread is not None]
        if not spreads:
            return None
        return min(spreads)

    @property
    def has_both_books(self) -> bool:
        return self.yes_mid_price is not None and self.no_mid_price is not None

    def book_for_side(self, side: Side) -> OrderBook:
        return self.yes_book if side == Side.YES else self.no_book


@dataclass
class Market:
    condition_id: str
    question: str
    token_id_yes: str = ""
    token_id_no: str = ""
    status: MarketStatus = MarketStatus.ACTIVE
    active: bool = True
    competition: str = "medium"
    volume: float = 0.0
    liquidity: float = 0.0
    end_date: Optional[str] = None
    # Derived from books
    mid_price: Optional[float] = None
    spread_cents: Optional[float] = None
    yes_mid_price: Optional[float] = None
    no_mid_price: Optional[float] = None
    yes_spread_cents: Optional[float] = None
    no_spread_cents: Optional[float] = None
    fee_rate_bps_yes: Optional[int] = None
    fee_rate_bps_no: Optional[int] = None
    fee_enabled: bool = False
    # Resolution
    resolved_outcome: Optional[Side] = None

    @property
    def short_question(self) -> str:
        return self.question[:50] + "..." if len(self.question) > 50 else self.question

    @property
    def max_fee_rate_bps(self) -> Optional[int]:
        fee_rates = [
            fee_rate
            for fee_rate in (self.fee_rate_bps_yes, self.fee_rate_bps_no)
            if fee_rate is not None
        ]
        if not fee_rates:
            return None
        return max(fee_rates)


@dataclass
class Order:
    order_id: str = ""
    source_order_id: str = ""
    market_id: str = ""
    token_id: str = ""
    side: Side = Side.YES
    price: float = 0.0
    size: float = 0.0
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    is_hedge: bool = False
    fee_rate_bps: int = 0

    @property
    def price_cents(self) -> float:
        return self.price * 100

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.size - self.filled_size)

    @property
    def remaining_cost(self) -> float:
        return self.price * self.remaining_size


@dataclass
class Position:
    market_id: str
    question: str = ""
    yes_price: float = 0.0
    no_price: float = 0.0
    yes_size: float = 0.0
    no_size: float = 0.0
    status: PositionStatus = PositionStatus.COLLECTING
    pnl: float = 0.0
    hedged_size: float = 0.0
    opened_at: float = field(default_factory=time.time)
    unhedged_scan_cycles: int = 0

    @property
    def yes_price_cents(self) -> float:
        return self.yes_price * 100

    @property
    def no_price_cents(self) -> float:
        return self.no_price * 100

    @property
    def total_cost(self) -> float:
        return (self.yes_price * self.yes_size) + (self.no_price * self.no_size)

    @property
    def sum_cents(self) -> float:
        return (self.yes_price + self.no_price) * 100


@dataclass
class TradeRecord:
    market_id: str
    side: Side
    price: float
    size: float
    is_hedge: bool
    timestamp: float = field(default_factory=time.time)
    pnl: Optional[float] = None


@dataclass
class SessionStats:
    start_time: float = field(default_factory=time.time)
    total_orders: int = 0
    total_fills: int = 0
    total_hedges: int = 0
    total_rewards: float = 0.0
    total_pnl: float = 0.0
    resolved_markets: int = 0

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def uptime_formatted(self) -> str:
        s = int(self.uptime_seconds)
        h, remainder = divmod(s, 3600)
        m, sec = divmod(remainder, 60)
        if h > 0:
            return f"{h}h {m}m {sec}s"
        return f"{m}m {sec}s"
