"""Order management: placement, cancellation, and status tracking."""

from __future__ import annotations
import time
import random
from datetime import datetime, timezone
from typing import Optional

from data.clob_client import PolymarketClient
from data.models import Order, OrderStatus, Side
from execution.rate_limiter import RateLimiter
from observability import logger as log


class OrderManager:
    """Manage the full order lifecycle."""

    def __init__(self, client: PolymarketClient, rate_limiter: RateLimiter,
                 dry_run: bool = True, paper_trading: bool = False,
                 hold_interval_sec: float = 60.0):
        self.client = client
        self.rate_limiter = rate_limiter
        self.dry_run = dry_run
        self.paper_trading = paper_trading
        self.hold_interval_sec = hold_interval_sec
        self._live_orders: dict[str, Order] = {}  # order_id -> Order
        self._fill_history: list[Order] = []
        self._seen_trade_ids: set[str] = set()
        self._placed_orders_count = 0
        self._recent_orders: dict[str, tuple[Order, float]] = {}
        self._recent_order_ttl_sec = max(300.0, hold_interval_sec * 3)
        self._audit_logger = None
        self._metrics = None

    @property
    def live_orders(self) -> dict[str, Order]:
        return self._live_orders

    @property
    def live_order_count(self) -> int:
        return len(self._live_orders)

    def set_audit_logger(self, audit_logger) -> None:
        self._audit_logger = audit_logger

    def set_metrics(self, metrics) -> None:
        self._metrics = metrics

    def restore_live_order(self, order: Order) -> None:
        """Register a pre-existing live order recovered from the exchange."""
        if not order.order_id or order.remaining_size <= 0:
            return
        if not order.source_order_id:
            order.source_order_id = order.order_id
        self._live_orders[order.order_id] = order

    def place_order(self, token_id: str, side: Side, price: float,
                    size: float, market_id: str = "",
                    is_hedge: bool = False) -> Optional[Order]:
        """Place an order with rate limiting."""
        wait_started_at = time.perf_counter()
        self.rate_limiter.wait()
        rate_wait_sec = time.perf_counter() - wait_started_at
        place_started_at = time.perf_counter()

        order = self.client.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            dry_run=self.dry_run,
        )
        place_call_sec = time.perf_counter() - place_started_at
        if self._metrics is not None:
            self._metrics.record_rate_limit_wait(rate_wait_sec)
            self._metrics.record_order_place_latency(place_call_sec, is_hedge=is_hedge)
        rate_wait_ms = round(rate_wait_sec * 1000.0, 3)
        place_call_ms = round(place_call_sec * 1000.0, 3)

        if order:
            order.market_id = market_id
            order.is_hedge = is_hedge
            if not order.source_order_id:
                order.source_order_id = order.order_id
            self._live_orders[order.order_id] = order
            self._placed_orders_count += 1
            self._record_audit_event(
                "order_placed",
                order_id=order.order_id,
                market_id=market_id,
                token_id=token_id,
                side=side.value,
                price=price,
                size=size,
                is_hedge=is_hedge,
                fee_rate_bps=order.fee_rate_bps,
                dry_run=self.dry_run,
                paper_trading=self.paper_trading,
                rate_limit_wait_ms=rate_wait_ms,
                place_call_ms=place_call_ms,
            )
        else:
            self._record_audit_event(
                "order_rejected",
                market_id=market_id,
                token_id=token_id,
                side=side.value,
                price=price,
                size=size,
                is_hedge=is_hedge,
                reason=getattr(self.client, "last_api_error", "") or "place_order returned None",
                dry_run=self.dry_run,
                paper_trading=self.paper_trading,
                rate_limit_wait_ms=rate_wait_ms,
                place_call_ms=place_call_ms,
            )

        return order

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        wait_started_at = time.perf_counter()
        self.rate_limiter.wait()
        rate_wait_sec = time.perf_counter() - wait_started_at
        cancel_started_at = time.perf_counter()
        tracked_order = self._live_orders.get(order_id)
        success = self.client.cancel_order(order_id, dry_run=self.dry_run)
        cancel_call_sec = time.perf_counter() - cancel_started_at
        if self._metrics is not None:
            self._metrics.record_rate_limit_wait(rate_wait_sec)
            self._metrics.record_cancel_latency(cancel_call_sec)
        rate_wait_ms = round(rate_wait_sec * 1000.0, 3)
        cancel_call_ms = round(cancel_call_sec * 1000.0, 3)
        if success and order_id in self._live_orders:
            order = self._live_orders[order_id]
            order.status = OrderStatus.CANCELLED
            self._archive_order(order)
            del self._live_orders[order_id]
            self._record_cancel_event(
                order,
                reason="cancel_order",
                cancel_call_ms=cancel_call_ms,
                rate_limit_wait_ms=rate_wait_ms,
                order_age_ms=round(order.age_seconds * 1000.0, 3),
            )
        elif success:
            self._record_audit_event(
                "order_cancelled",
                order_id=order_id,
                reason="cancel_order",
                dry_run=self.dry_run,
                paper_trading=self.paper_trading,
                cancel_call_ms=cancel_call_ms,
                rate_limit_wait_ms=rate_wait_ms,
            )
        else:
            self._record_audit_event(
                "order_cancel_failed",
                order_id=order_id,
                market_id=tracked_order.market_id if tracked_order else "",
                side=tracked_order.side.value if tracked_order else "",
                reason=getattr(self.client, "last_api_error", "") or "cancel_order returned False",
                dry_run=self.dry_run,
                paper_trading=self.paper_trading,
                cancel_call_ms=cancel_call_ms,
                rate_limit_wait_ms=rate_wait_ms,
            )
        return success

    def cancel_stale_orders(self, market_id: str, max_age_sec: float) -> int:
        """Cancel orders older than max_age_sec for a market."""
        cancelled = 0
        stale = [
            o for o in self._live_orders.values()
            if o.market_id == market_id and o.age_seconds > max_age_sec
        ]
        for order in stale:
            if self.cancel_order(order.order_id):
                cancelled += 1
        if cancelled > 0:
            log.info(f"cancelled {cancelled} stale orders for market {market_id[:10]}...")
        return cancelled

    def cancel_all_for_market(self, market_id: str) -> int:
        """Cancel all orders for a market."""
        cancelled = 0
        orders = [o for o in list(self._live_orders.values()) if o.market_id == market_id]
        for order in orders:
            if self.cancel_order(order.order_id):
                cancelled += 1
        return cancelled

    def cancel_all(self) -> bool:
        """Cancel all orders in an emergency."""
        wait_started_at = time.perf_counter()
        self.rate_limiter.wait()
        rate_wait_sec = time.perf_counter() - wait_started_at
        cancel_started_at = time.perf_counter()
        success = self.client.cancel_all(dry_run=self.dry_run)
        cancel_all_sec = time.perf_counter() - cancel_started_at
        if self._metrics is not None:
            self._metrics.record_rate_limit_wait(rate_wait_sec)
            self._metrics.record_cancel_all_latency(cancel_all_sec)
        rate_wait_ms = round(rate_wait_sec * 1000.0, 3)
        cancel_all_ms = round(cancel_all_sec * 1000.0, 3)
        if success:
            for order in list(self._live_orders.values()):
                order.status = OrderStatus.CANCELLED
                self._archive_order(order)
                self._record_cancel_event(
                    order,
                    reason="cancel_all",
                    cancel_all_call_ms=cancel_all_ms,
                    rate_limit_wait_ms=rate_wait_ms,
                    order_age_ms=round(order.age_seconds * 1000.0, 3),
                )
            self._live_orders.clear()
        else:
            self._record_audit_event(
                "cancel_all_failed",
                reason=getattr(self.client, "last_api_error", "") or "cancel_all returned False",
                dry_run=self.dry_run,
                paper_trading=self.paper_trading,
                cancel_all_call_ms=cancel_all_ms,
                rate_limit_wait_ms=rate_wait_ms,
            )
        return success

    def has_live_orders_for_market(self, market_id: str) -> bool:
        """Return True if there are still open orders for the market."""
        return any(order.market_id == market_id for order in self._live_orders.values())

    def has_hedge_orders_for_market(self, market_id: str) -> bool:
        """Return True if the market has live hedge orders that should not be touched."""
        return any(
            order.market_id == market_id and order.is_hedge
            for order in self._live_orders.values()
        )

    def get_reserved_cost_for_market(self, market_id: str) -> float:
        """Residual capital reserved by live orders in a market."""
        return sum(
            order.remaining_cost for order in self._live_orders.values()
            if order.market_id == market_id
        )

    def get_total_reserved_cost(self) -> float:
        """Residual capital reserved by all live orders."""
        return sum(order.remaining_cost for order in self._live_orders.values())

    def get_live_market_ids(self) -> set[str]:
        return {
            order.market_id
            for order in self._live_orders.values()
            if order.market_id
        }

    def check_fills(
        self,
        market_books_by_id: Optional[dict[str, object]] = None,
        trade_updates: Optional[list[dict]] = None,
    ) -> list[Order]:
        """
        Check whether there are new fills.
        In dry_run, simulate fills.
        """
        started_at = time.perf_counter()
        try:
            if self.dry_run:
                if self.paper_trading:
                    return self._simulate_fills_from_books(market_books_by_id or {})
                return self._simulate_fills()

            self._prune_recent_orders()
            new_fills = []
            try:
                if trade_updates is None:
                    wait_started_at = time.perf_counter()
                    self.rate_limiter.wait()
                    rate_wait_sec = time.perf_counter() - wait_started_at
                    if self._metrics is not None:
                        self._metrics.record_rate_limit_wait(rate_wait_sec)
                    trades = self.client.get_trades()
                else:
                    trades = trade_updates

                for trade in trades:
                    order_id = self._extract_trade_order_id(trade)
                    tracked_order = self._resolve_order_for_fill(order_id)
                    if not order_id or tracked_order is None:
                        continue

                    trade_id = self._extract_trade_id(trade, order_id)
                    if trade_id in self._seen_trade_ids:
                        continue

                    remaining_size = tracked_order.remaining_size
                    fill_size = self._extract_trade_size(trade)
                    if fill_size is None:
                        fill_size = remaining_size

                    fill_size = min(fill_size, remaining_size)
                    if fill_size <= 0:
                        self._seen_trade_ids.add(trade_id)
                        continue

                    fill_price = self._extract_trade_price(trade)
                    if fill_price is None:
                        fill_price = tracked_order.price

                    tracked_order.filled_size += fill_size
                    tracked_order.filled_at = self._extract_trade_timestamp_seconds(trade) or time.time()
                    is_fully_filled = tracked_order.remaining_size <= 1e-9
                    tracked_order.status = OrderStatus.FILLED if is_fully_filled else OrderStatus.PARTIALLY_FILLED

                    fill_event = Order(
                        order_id=trade_id,
                        source_order_id=tracked_order.order_id,
                        market_id=tracked_order.market_id,
                        token_id=tracked_order.token_id,
                        side=tracked_order.side,
                        price=fill_price,
                        size=fill_size,
                        filled_size=fill_size,
                        status=tracked_order.status,
                        created_at=tracked_order.created_at,
                        filled_at=tracked_order.filled_at,
                        is_hedge=tracked_order.is_hedge,
                    )
                    self._seen_trade_ids.add(trade_id)
                    self._fill_history.append(fill_event)
                    new_fills.append(fill_event)

                    if is_fully_filled:
                        self._archive_order(tracked_order)
                        self._live_orders.pop(order_id, None)
            except Exception as e:
                log.err(f"fill check error: {e}")

            self._prune_recent_orders()
            return new_fills
        finally:
            if self._metrics is not None:
                self._metrics.record_fill_check_latency(time.perf_counter() - started_at)

    def _simulate_fills_from_books(self, market_books_by_id: dict[str, object]) -> list[Order]:
        """
        Simulate paper-trading fills using the real books from the current cycle.
        An order fills if the best ask touches our price or, after enough time,
        if we are top-of-book for long enough.
        """
        if not self._live_orders:
            return []

        new_fills: list[Order] = []
        tick = 0.01
        hold_interval = max(self.hold_interval_sec, 1.0)

        for order in list(self._live_orders.values()):
            books = market_books_by_id.get(order.market_id)
            if books is None or not hasattr(books, "book_for_side"):
                continue

            book = books.book_for_side(order.side)
            best_bid = book.best_yes_bid
            best_ask = book.best_yes_ask
            if best_bid is None and best_ask is None:
                continue

            remaining = order.remaining_size or order.size
            if remaining <= 0:
                continue

            fill_size = 0.0
            if best_ask is not None and order.price >= best_ask - 1e-9:
                fill_size = remaining
            elif (
                best_bid is not None
                and order.price >= best_bid - (tick / 2)
                and order.age_seconds >= hold_interval
            ):
                queue_progress = min(1.0, order.age_seconds / (hold_interval * (1.5 if order.is_hedge else 2.5)))
                fill_ratio = min(0.75 if order.is_hedge else 0.5, queue_progress * (0.9 if order.is_hedge else 0.6))
                fill_size = round(max(0.1, remaining * fill_ratio), 4)

            fill_size = min(fill_size, remaining)
            if fill_size <= 0:
                continue

            order.filled_size += fill_size
            order.filled_at = time.time()
            is_fully_filled = order.remaining_size <= 1e-9
            order.status = OrderStatus.FILLED if is_fully_filled else OrderStatus.PARTIALLY_FILLED

            fill_event = Order(
                order_id=f"paper_fill_{order.order_id}_{int(order.filled_at * 1000)}",
                source_order_id=order.order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                price=order.price,
                size=fill_size,
                filled_size=fill_size,
                status=order.status,
                created_at=order.created_at,
                filled_at=order.filled_at,
                is_hedge=order.is_hedge,
            )
            self._fill_history.append(fill_event)
            new_fills.append(fill_event)

            if is_fully_filled:
                self._live_orders.pop(order.order_id, None)

        return new_fills

    def _simulate_fills(self) -> list[Order]:
        """
        Simulate fills in dry run.
        Probability increases with order age, and hedges have higher priority.
        """
        if not self._live_orders:
            return []

        new_fills: list[Order] = []
        max_fills = max(1, min(4, len(self._live_orders) // 3))

        for order in list(self._live_orders.values()):
            age_factor = min(order.age_seconds / 30.0, 1.0) * 0.20
            base_prob = 0.10 + age_factor
            if order.is_hedge:
                base_prob += 0.20

            if random.random() > min(base_prob, 0.80):
                continue

            order.status = OrderStatus.FILLED
            order.filled_at = time.time()
            self._fill_history.append(order)
            new_fills.append(order)
            del self._live_orders[order.order_id]

            if len(new_fills) >= max_fills:
                break

        return new_fills

    def _record_cancel_event(self, order: Order, reason: str, **extra_payload) -> None:
        self._record_audit_event(
            "order_cancelled",
            order_id=order.order_id,
            source_order_id=order.source_order_id or order.order_id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side.value,
            price=order.price,
            size=order.remaining_size or order.size,
            is_hedge=order.is_hedge,
            fee_rate_bps=order.fee_rate_bps,
            reason=reason,
            dry_run=self.dry_run,
            paper_trading=self.paper_trading,
            **extra_payload,
        )

    def _record_audit_event(self, event_type: str, **payload) -> None:
        if self._audit_logger is None:
            return
        self._audit_logger.record(event_type, **payload)

    def get_orders_for_market(self, market_id: str) -> list[Order]:
        """Return all live orders for a market."""
        return [o for o in self._live_orders.values() if o.market_id == market_id]

    def get_live_orders_for_market_side(
        self,
        market_id: str,
        side: Side,
        hedges_only: Optional[bool] = None,
    ) -> list[Order]:
        """Return live orders for market+side, optionally filtered by entry/hedge."""
        return [
            order for order in self._live_orders.values()
            if order.market_id == market_id
            and order.side == side
            and (hedges_only is None or order.is_hedge == hedges_only)
        ]

    def get_live_coverage_for_market(
        self,
        market_id: str,
        side: Side,
        hedges_only: Optional[bool] = None,
    ) -> float:
        """Residual size covered by live orders on the requested side."""
        return sum(
            order.remaining_size
            for order in self.get_live_orders_for_market_side(
                market_id,
                side,
                hedges_only=hedges_only,
            )
        )

    def cancel_orders_for_market_side(
        self,
        market_id: str,
        side: Optional[Side] = None,
        hedges_only: Optional[bool] = None,
    ) -> int:
        """Cancel live orders filtered by market, side, and order type."""
        cancelled = 0
        orders = [
            order for order in list(self._live_orders.values())
            if order.market_id == market_id
            and (side is None or order.side == side)
            and (hedges_only is None or order.is_hedge == hedges_only)
        ]
        for order in orders:
            if self.cancel_order(order.order_id):
                cancelled += 1
        return cancelled

    def get_entry_orders_for_market(self, market_id: str) -> list[Order]:
        """Return entry orders that are still live for a market."""
        return [
            order for order in self._live_orders.values()
            if order.market_id == market_id and not order.is_hedge
        ]

    def get_total_orders_placed(self) -> int:
        return self._placed_orders_count

    def _extract_trade_order_id(self, trade: dict) -> str:
        for key in ("orderID", "order_id", "makerOrderID", "maker_order_id", "orderId"):
            value = trade.get(key)
            if value:
                return str(value)
        maker_orders = trade.get("maker_orders") or trade.get("makerOrders") or []
        if isinstance(maker_orders, list):
            for maker_order in maker_orders:
                if not isinstance(maker_order, dict):
                    continue
                for key in ("order_id", "orderID", "id"):
                    value = maker_order.get(key)
                    if value:
                        return str(value)
        return ""

    def _extract_trade_id(self, trade: dict, order_id: str) -> str:
        for key in ("id", "tradeID", "trade_id", "matchID", "match_id"):
            value = trade.get(key)
            if value:
                return str(value)

        timestamp = trade.get("timestamp") or trade.get("createdAt") or trade.get("matchedAt") or ""
        price = trade.get("price") or trade.get("makerPrice") or trade.get("takerPrice") or ""
        size = trade.get("size") or trade.get("amount") or trade.get("matchedAmount") or ""
        return f"{order_id}:{price}:{size}:{timestamp}"

    def _extract_trade_size(self, trade: dict) -> Optional[float]:
        for key in ("size", "amount", "matchedAmount", "makerAmount", "takerAmount"):
            value = trade.get(key)
            parsed = self._to_float(value)
            if parsed is not None:
                return parsed
        maker_orders = trade.get("maker_orders") or trade.get("makerOrders") or []
        if isinstance(maker_orders, list):
            for maker_order in maker_orders:
                if not isinstance(maker_order, dict):
                    continue
                for key in ("matched_amount", "matchedAmount", "size"):
                    parsed = self._to_float(maker_order.get(key))
                    if parsed is not None:
                        return parsed
        return None

    def _extract_trade_price(self, trade: dict) -> Optional[float]:
        for key in ("price", "makerPrice", "takerPrice"):
            value = trade.get(key)
            parsed = self._to_float(value)
            if parsed is not None:
                return parsed
        maker_orders = trade.get("maker_orders") or trade.get("makerOrders") or []
        if isinstance(maker_orders, list):
            for maker_order in maker_orders:
                if not isinstance(maker_order, dict):
                    continue
                parsed = self._to_float(maker_order.get("price"))
                if parsed is not None:
                    return parsed
        return None

    def _extract_trade_timestamp_seconds(self, trade: dict) -> Optional[float]:
        for key in ("timestamp", "createdAt", "matchedAt", "matched_at"):
            value = trade.get(key)
            parsed = self._to_timestamp_seconds(value)
            if parsed is not None:
                return parsed
        return None

    def apply_order_updates(self, order_updates: list[dict]) -> int:
        """Sync local state with user-channel WebSocket order updates."""
        self._prune_recent_orders()
        cancelled = 0
        for update in order_updates:
            order_id = self._extract_order_update_id(update)
            if not order_id or order_id not in self._live_orders:
                continue

            live_order = self._live_orders[order_id]
            matched_size = self._extract_order_update_matched_size(update)
            if matched_size is not None:
                live_order.filled_size = max(live_order.filled_size, min(matched_size, live_order.size))
                if live_order.remaining_size <= 1e-9:
                    live_order.status = OrderStatus.FILLED
                elif live_order.filled_size > 0:
                    live_order.status = OrderStatus.PARTIALLY_FILLED

            update_type = str(update.get("type", update.get("status", ""))).upper()
            if update_type in {"CANCELLATION", "CANCELED", "CANCELLED"}:
                live_order.status = OrderStatus.CANCELLED
                self._archive_order(live_order)
                self._live_orders.pop(order_id, None)
                cancelled += 1

        return cancelled

    def _archive_order(self, order: Order) -> None:
        self._recent_orders[order.order_id] = (order, time.time())

    def _resolve_order_for_fill(self, order_id: str) -> Optional[Order]:
        live_order = self._live_orders.get(order_id)
        if live_order is not None:
            return live_order

        archived = self._recent_orders.get(order_id)
        if archived is None:
            return None
        return archived[0]

    def _prune_recent_orders(self) -> None:
        cutoff = time.time() - self._recent_order_ttl_sec
        stale_order_ids = [
            order_id
            for order_id, (_, archived_at) in self._recent_orders.items()
            if archived_at < cutoff
        ]
        for order_id in stale_order_ids:
            self._recent_orders.pop(order_id, None)

    def _to_float(self, value: object) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _to_timestamp_seconds(self, value: object) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            current = float(value)
            if current > 1e12:
                return current / 1000.0
            if current > 1e10:
                return current / 1000.0
            return current
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not stripped:
            return None
        numeric = self._to_float(stripped)
        if numeric is not None:
            if numeric > 1e12:
                return numeric / 1000.0
            if numeric > 1e10:
                return numeric / 1000.0
            return numeric
        iso_value = stripped.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(iso_value).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None

    def _extract_order_update_id(self, update: dict) -> str:
        for key in ("id", "order_id", "orderID"):
            value = update.get(key)
            if value:
                return str(value)
        return ""

    def _extract_order_update_matched_size(self, update: dict) -> Optional[float]:
        for key in ("matched_amount", "matchedAmount", "size_matched", "sizeMatched"):
            parsed = self._to_float(update.get(key))
            if parsed is not None:
                return parsed
        return None
