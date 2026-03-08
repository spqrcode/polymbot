"""
Tracker metriche: PnL, fill rate, rewards.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class LatencyStats:
    """Statistiche aggregate per latenze in millisecondi."""
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    last_ms: float = 0.0

    def record_seconds(self, seconds: float) -> None:
        ms = max(0.0, seconds * 1000.0)
        self.count += 1
        self.total_ms += ms
        self.last_ms = ms
        if self.count == 1 or ms < self.min_ms:
            self.min_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms

    @property
    def avg_ms(self) -> float:
        if self.count == 0:
            return 0.0
        return self.total_ms / self.count

    def detailed(self) -> str:
        if self.count == 0:
            return "n=0"
        return (
            f"n={self.count} avg={self.avg_ms:.1f} last={self.last_ms:.1f} "
            f"min={self.min_ms:.1f} max={self.max_ms:.1f}"
        )

    def compact(self) -> str:
        if self.count == 0:
            return "n/a"
        return f"{self.last_ms:.0f}/{self.avg_ms:.0f}/{self.max_ms:.0f}ms"

    def avg_only(self) -> str:
        if self.count == 0:
            return "n/a"
        return f"{self.avg_ms:.0f}ms"


@dataclass
class ValueStats:
    """Statistiche aggregate per valori scalari non temporali."""
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

    @property
    def avg(self) -> float:
        if self.count == 0:
            return 0.0
        return self.total / self.count

    def detailed(self, unit: str = "") -> str:
        if self.count == 0:
            return "n=0"
        suffix = unit
        return (
            f"n={self.count} avg={self.avg:.2f}{suffix} last={self.last_value:.2f}{suffix} "
            f"min={self.min_value:.2f}{suffix} max={self.max_value:.2f}{suffix}"
        )

    def avg_only(self, unit: str = "") -> str:
        if self.count == 0:
            return "n/a"
        return f"{self.avg:.2f}{unit}"


@dataclass
class Metrics:
    """Metriche aggregate della sessione."""
    start_time: float = field(default_factory=time.time)

    # Ordini
    orders_placed: int = 0
    orders_cancelled: int = 0
    orders_filled: int = 0
    open_orders: int = 0

    # Hedge
    hedges_placed: int = 0
    hedges_filled: int = 0

    # PnL
    realized_pnl: float = 0.0
    rewards_earned: float = 0.0
    unrealized_pnl: float = 0.0

    # Mercati
    markets_scanned: int = 0
    markets_traded: int = 0
    markets_resolved: int = 0

    # Scan
    scan_count: int = 0
    last_scan_time: float = 0.0
    _filled_entry_orders: set[str] = field(default_factory=set, repr=False)
    _filled_hedge_orders: set[str] = field(default_factory=set, repr=False)

    # Debug
    ws_market_connected: int = 0
    ws_user_connected: int = 0
    ws_books_used: int = 0
    rest_books_used: int = 0
    ws_trade_updates: int = 0
    ws_order_updates: int = 0
    entry_reprices: int = 0
    skip_competitive_orders: int = 0
    skip_reprice_age: int = 0
    skip_live_hedge_orders: int = 0
    pairs_via_hedge_path: int = 0
    pairs_via_entry_bypass: int = 0

    # Performance / timing
    scan_cycle_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    book_fetch_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    quote_loop_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    fill_check_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    fill_process_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    entry_place_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    hedge_place_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    cancel_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    cancel_all_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    rate_limit_wait_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    entry_fill_age_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    hedge_fill_age_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    fill_to_hedge_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    hedge_submit_to_fill_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    unhedged_window_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    hedge_compute_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    book_age_latency: LatencyStats = field(default_factory=LatencyStats, repr=False)
    hedge_slippage_cents: ValueStats = field(default_factory=ValueStats, repr=False)
    adverse_move_cents: ValueStats = field(default_factory=ValueStats, repr=False)
    hedge_queue_ahead_size: ValueStats = field(default_factory=ValueStats, repr=False)
    hedge_queue_levels_ahead: ValueStats = field(default_factory=ValueStats, repr=False)
    hedge_queue_gap_cents: ValueStats = field(default_factory=ValueStats, repr=False)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.rewards_earned + self.unrealized_pnl

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

    @property
    def entry_fill_rate(self) -> float:
        if self.orders_placed == 0:
            return 0.0
        return len(self._filled_entry_orders) / self.orders_placed

    @property
    def hedge_fill_rate(self) -> float:
        if self.hedges_placed == 0:
            return 0.0
        return len(self._filled_hedge_orders) / self.hedges_placed

    @property
    def total_orders_placed(self) -> int:
        return self.orders_placed + self.hedges_placed

    @property
    def total_fill_events(self) -> int:
        return self.orders_filled + self.hedges_filled

    def record_order(self, is_hedge: bool = False):
        if is_hedge:
            self.hedges_placed += 1
        else:
            self.orders_placed += 1

    def record_cancel(self, count: int = 1):
        self.orders_cancelled += max(0, count)

    def record_fill(self, source_order_id: str = "", is_hedge: bool = False):
        if is_hedge:
            self.hedges_filled += 1
            if source_order_id:
                self._filled_hedge_orders.add(source_order_id)
        else:
            self.orders_filled += 1
            if source_order_id:
                self._filled_entry_orders.add(source_order_id)

    def record_hedge(self):
        self.record_order(is_hedge=True)

    def record_hedge_fill(self):
        self.record_fill(is_hedge=True)

    def record_pnl(self, amount: float):
        self.realized_pnl += amount

    def record_reward(self, amount: float):
        self.rewards_earned += amount

    def update_unrealized_pnl(self, amount: float):
        self.unrealized_pnl = amount

    def record_scan(self):
        self.scan_count += 1
        self.last_scan_time = time.time()

    def update_open_orders(self, count: int):
        self.open_orders = max(0, count)

    def record_resolution(self):
        self.markets_resolved += 1

    def record_ws_started(self, market_channel: bool, user_channel: bool):
        if market_channel:
            self.ws_market_connected = 1
        if user_channel:
            self.ws_user_connected = 1

    def record_book_source(self, ws_count: int = 0, rest_count: int = 0):
        self.ws_books_used += max(0, ws_count)
        self.rest_books_used += max(0, rest_count)

    def record_ws_trade_updates(self, count: int):
        self.ws_trade_updates += max(0, count)

    def record_ws_order_updates(self, count: int):
        self.ws_order_updates += max(0, count)

    def record_reprice(self, count: int = 1):
        self.entry_reprices += max(0, count)

    def record_skip_competitive(self):
        self.skip_competitive_orders += 1

    def record_skip_reprice_age(self):
        self.skip_reprice_age += 1

    def record_skip_live_hedge(self):
        self.skip_live_hedge_orders += 1

    def record_pair_lock_event(self, via_hedge: bool):
        if via_hedge:
            self.pairs_via_hedge_path += 1
        else:
            self.pairs_via_entry_bypass += 1

    def record_scan_cycle_latency(self, seconds: float):
        self.scan_cycle_latency.record_seconds(seconds)

    def record_book_fetch_latency(self, seconds: float):
        self.book_fetch_latency.record_seconds(seconds)

    def record_quote_loop_latency(self, seconds: float):
        self.quote_loop_latency.record_seconds(seconds)

    def record_fill_check_latency(self, seconds: float):
        self.fill_check_latency.record_seconds(seconds)

    def record_fill_process_latency(self, seconds: float):
        self.fill_process_latency.record_seconds(seconds)

    def record_order_place_latency(self, seconds: float, is_hedge: bool = False):
        target = self.hedge_place_latency if is_hedge else self.entry_place_latency
        target.record_seconds(seconds)

    def record_cancel_latency(self, seconds: float):
        self.cancel_latency.record_seconds(seconds)

    def record_cancel_all_latency(self, seconds: float):
        self.cancel_all_latency.record_seconds(seconds)

    def record_rate_limit_wait(self, seconds: float):
        self.rate_limit_wait_latency.record_seconds(seconds)

    def record_fill_age(self, seconds: float, is_hedge: bool = False):
        target = self.hedge_fill_age_latency if is_hedge else self.entry_fill_age_latency
        target.record_seconds(seconds)

    def record_fill_to_hedge_latency(self, seconds: float):
        self.fill_to_hedge_latency.record_seconds(seconds)

    def record_hedge_submit_to_fill_latency(self, seconds: float):
        self.hedge_submit_to_fill_latency.record_seconds(seconds)

    def record_unhedged_window_latency(self, seconds: float):
        self.unhedged_window_latency.record_seconds(seconds)

    def record_hedge_compute_latency(self, seconds: float):
        self.hedge_compute_latency.record_seconds(seconds)

    def record_book_age_samples(self, seconds_values: list[float]):
        for seconds in seconds_values:
            self.book_age_latency.record_seconds(seconds)

    def record_hedge_slippage_cents(self, cents: float):
        self.hedge_slippage_cents.record(cents)

    def record_adverse_move_cents(self, cents: float):
        self.adverse_move_cents.record(cents)

    def record_hedge_queue_estimate(
        self,
        ahead_size: float,
        levels_ahead: int,
        gap_cents: float,
    ):
        self.hedge_queue_ahead_size.record(ahead_size)
        self.hedge_queue_levels_ahead.record(float(levels_ahead))
        self.hedge_queue_gap_cents.record(gap_cents)

    def summary(self) -> dict:
        return {
            "uptime": self.uptime_formatted,
            "entry_orders": self.orders_placed,
            "hedge_orders": self.hedges_placed,
            "orders_total": self.total_orders_placed,
            "open_orders": self.open_orders,
            "orders_cancelled": self.orders_cancelled,
            "entry_fill_events": self.orders_filled,
            "hedge_fill_events": self.hedges_filled,
            "fill_events_total": self.total_fill_events,
            "entry_fill_rate": f"{self.entry_fill_rate:.1%}",
            "hedge_fill_rate": f"{self.hedge_fill_rate:.1%}",
            "pnl": f"${self.realized_pnl:+.2f}",
            "unrealized": f"${self.unrealized_pnl:+.2f}",
            "rewards": f"${self.rewards_earned:.2f}",
            "total": f"${self.total_pnl:+.2f}",
            "resolved": self.markets_resolved,
            "scans": self.scan_count,
            "debug_ws_market": self.ws_market_connected,
            "debug_ws_user": self.ws_user_connected,
            "debug_books_ws": self.ws_books_used,
            "debug_books_rest": self.rest_books_used,
            "debug_ws_trades": self.ws_trade_updates,
            "debug_ws_orders": self.ws_order_updates,
            "debug_reprices": self.entry_reprices,
            "debug_skip_competitive": self.skip_competitive_orders,
            "debug_skip_reprice_age": self.skip_reprice_age,
            "debug_skip_hedge_live": self.skip_live_hedge_orders,
            "debug_pairs_hedge_path": self.pairs_via_hedge_path,
            "debug_pairs_entry_bypass": self.pairs_via_entry_bypass,
            "perf_scan_ms": self.scan_cycle_latency.detailed(),
            "perf_books_ms": self.book_fetch_latency.detailed(),
            "perf_quote_ms": self.quote_loop_latency.detailed(),
            "perf_fill_check_ms": self.fill_check_latency.detailed(),
            "perf_fill_process_ms": self.fill_process_latency.detailed(),
            "perf_entry_place_ms": self.entry_place_latency.detailed(),
            "perf_hedge_place_ms": self.hedge_place_latency.detailed(),
            "perf_cancel_ms": self.cancel_latency.detailed(),
            "perf_cancel_all_ms": self.cancel_all_latency.detailed(),
            "perf_rate_wait_ms": self.rate_limit_wait_latency.detailed(),
            "perf_entry_fill_age_ms": self.entry_fill_age_latency.detailed(),
            "perf_hedge_fill_age_ms": self.hedge_fill_age_latency.detailed(),
            "perf_fill_to_hedge_ms": self.fill_to_hedge_latency.detailed(),
            "perf_hedge_submit_to_fill_ms": self.hedge_submit_to_fill_latency.detailed(),
            "perf_unhedged_window_ms": self.unhedged_window_latency.detailed(),
            "perf_hedge_compute_ms": self.hedge_compute_latency.detailed(),
            "perf_book_age_ms": self.book_age_latency.detailed(),
            "perf_hedge_slippage_c": self.hedge_slippage_cents.detailed(unit="c"),
            "perf_adverse_move_c": self.adverse_move_cents.detailed(unit="c"),
            "perf_queue_ahead_size": self.hedge_queue_ahead_size.detailed(),
            "perf_queue_levels_ahead": self.hedge_queue_levels_ahead.detailed(),
            "perf_queue_gap_c": self.hedge_queue_gap_cents.detailed(unit="c"),
        }
