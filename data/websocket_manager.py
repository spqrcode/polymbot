"""
WebSocket bridge for market data and user updates.
Maintains a local book cache and a queue of user events to reduce REST polling.
"""

from __future__ import annotations

import copy
import json
import ssl
import threading
import time
from collections import deque
from typing import Callable, Optional

from config.settings import Settings
from data.models import Market, MarketOrderBooks, OrderBook, OrderBookLevel, Side
from observability import logger as log

try:
    import websocket
except ImportError:  # pragma: no cover - optional runtime dependency
    websocket = None


class PolymarketWebSocketBridge:
    """Manage market and user WebSocket channels with a thread-safe cache."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._asset_to_market: dict[str, tuple[str, Side]] = {}
        self._market_ids: list[str] = []
        self._asset_ids: list[str] = []
        self._books_by_market: dict[str, MarketOrderBooks] = {}
        self._trade_updates: deque[dict] = deque()
        self._order_updates: deque[dict] = deque()
        self._started = False
        self._market_thread: Optional[threading.Thread] = None
        self._user_thread: Optional[threading.Thread] = None
        self._market_app = None
        self._user_app = None
        self._user_auth: Optional[dict] = None
        self._trade_update_handler: Optional[Callable[[dict], None]] = None
        self._order_update_handler: Optional[Callable[[dict], None]] = None

    @property
    def enabled(self) -> bool:
        return self.settings.api.use_websocket

    @property
    def started(self) -> bool:
        return self._started

    def register_markets(self, markets: list[Market]) -> None:
        with self._lock:
            self._market_ids = []
            self._asset_ids = []
            self._asset_to_market.clear()
            self._books_by_market = {}
            for market in markets:
                if not market.token_id_yes or not market.token_id_no:
                    continue
                self._market_ids.append(market.condition_id)
                self._asset_ids.append(market.token_id_yes)
                self._asset_ids.append(market.token_id_no)
                self._asset_to_market[market.token_id_yes] = (market.condition_id, Side.YES)
                self._asset_to_market[market.token_id_no] = (market.condition_id, Side.NO)
                self._books_by_market[market.condition_id] = MarketOrderBooks()

    def start(self, user_auth: Optional[dict] = None) -> bool:
        if not self.enabled:
            return False
        if websocket is None:
            log.warn("websocket-client unavailable, falling back to REST")
            return False

        with self._lock:
            if not self._asset_ids:
                return False
            self._user_auth = user_auth

        if self._started:
            return True

        self._stop_event.clear()
        self._market_thread = threading.Thread(
            target=self._run_market_loop,
            name="polymarket-market-ws",
            daemon=True,
        )
        self._market_thread.start()

        if user_auth and self._market_ids:
            self._user_thread = threading.Thread(
                target=self._run_user_loop,
                name="polymarket-user-ws",
                daemon=True,
            )
            self._user_thread.start()

        self._started = True
        log.info("websocket bridge started")
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._market_app is not None:
            self._market_app.close()
        if self._user_app is not None:
            self._user_app.close()
        for thread in (self._market_thread, self._user_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        self._started = False

    def get_books_snapshot(self, market_ids: Optional[list[str]] = None) -> dict[str, MarketOrderBooks]:
        with self._lock:
            target_ids = market_ids or list(self._books_by_market.keys())
            return {
                market_id: copy.deepcopy(books)
                for market_id, books in self._books_by_market.items()
                if market_id in target_ids and books.has_both_books
            }

    def drain_trade_updates(self) -> list[dict]:
        with self._lock:
            updates = list(self._trade_updates)
            self._trade_updates.clear()
            return updates

    def drain_order_updates(self) -> list[dict]:
        with self._lock:
            updates = list(self._order_updates)
            self._order_updates.clear()
            return updates

    def set_trade_update_handler(self, handler: Optional[Callable[[dict], None]]) -> None:
        with self._lock:
            self._trade_update_handler = handler

    def set_order_update_handler(self, handler: Optional[Callable[[dict], None]]) -> None:
        with self._lock:
            self._order_update_handler = handler

    def ingest_market_message(self, message: dict) -> None:
        event_type = str(message.get("event_type", "")).lower()
        if event_type == "book":
            self._apply_book_message(message)
        elif event_type == "price_change":
            self._apply_price_change(message)
        elif event_type == "best_bid_ask":
            self._apply_best_bid_ask(message)
        elif event_type == "market_resolved":
            market_id = str(message.get("market", ""))
            if market_id:
                log.info(f"market websocket resolved event: {market_id[:12]}...")

    def ingest_user_message(self, message: dict) -> None:
        event_type = str(message.get("event_type", "")).lower()
        if event_type == "trade":
            status = str(message.get("status", "")).upper()
            if status in {"MATCHED", "MINED", "CONFIRMED"}:
                self._dispatch_or_queue_user_update(
                    message=message,
                    handler_getter=lambda: self._trade_update_handler,
                    queue_appender=lambda: self._trade_updates.append(message),
                    label="trade",
                )
        elif event_type == "order":
            self._dispatch_or_queue_user_update(
                message=message,
                handler_getter=lambda: self._order_update_handler,
                queue_appender=lambda: self._order_updates.append(message),
                label="order",
            )

    def _run_market_loop(self) -> None:
        self._run_socket_loop(
            url=self.settings.api.market_ws_url,
            subscribe_builder=self._build_market_subscription,
            on_message=self.ingest_market_message,
            channel_name="market",
            set_app=lambda app: setattr(self, "_market_app", app),
        )

    def _run_user_loop(self) -> None:
        self._run_socket_loop(
            url=self.settings.api.user_ws_url,
            subscribe_builder=self._build_user_subscription,
            on_message=self.ingest_user_message,
            channel_name="user",
            set_app=lambda app: setattr(self, "_user_app", app),
        )

    def _run_socket_loop(self, url: str, subscribe_builder, on_message, channel_name: str, set_app) -> None:
        backoff_sec = 1.0
        while not self._stop_event.is_set():
            app = websocket.WebSocketApp(
                url,
                on_open=lambda ws: ws.send(json.dumps(subscribe_builder())),
                on_message=lambda ws, raw: self._handle_ws_message(raw, on_message, channel_name),
                on_error=lambda ws, err: log.warn(f"ws {channel_name} error: {err}"),
                on_close=lambda ws, status, msg: log.warn(f"ws {channel_name} closed: {status} {msg or ''}".strip()),
            )
            set_app(app)
            try:
                app.run_forever(
                    sslopt={"cert_reqs": ssl.CERT_REQUIRED},
                    ping_interval=self.settings.api.websocket_ping_interval_sec,
                    ping_timeout=self.settings.api.websocket_ping_timeout_sec,
                )
            except Exception as exc:
                log.warn(f"ws {channel_name} crash: {exc}")

            if self._stop_event.is_set():
                break

            time.sleep(backoff_sec)
            backoff_sec = min(backoff_sec * 2, 15.0)

    def _handle_ws_message(self, raw: str, handler, channel_name: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warn(f"ws {channel_name}: non-JSON payload")
            return

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    handler(item)
            return

        if isinstance(payload, dict):
            handler(payload)

    def _build_market_subscription(self) -> dict:
        with self._lock:
            return {
                "assets_ids": list(self._asset_ids),
                "type": "market",
                "custom_feature_enabled": True,
            }

    def _build_user_subscription(self) -> dict:
        with self._lock:
            return {
                "auth": self._user_auth or {},
                "markets": list(self._market_ids),
                "type": "user",
            }

    def _apply_book_message(self, message: dict) -> None:
        asset_id = str(message.get("asset_id", ""))
        with self._lock:
            target = self._resolve_target_book(asset_id)
            if target is None:
                return
            _, book = target
            book.yes_bids = self._parse_levels(message.get("bids") or message.get("buys") or [], reverse=True)
            book.yes_asks = self._parse_levels(message.get("asks") or message.get("sells") or [], reverse=False)
            book.timestamp = time.time()

    def _apply_price_change(self, message: dict) -> None:
        changes = message.get("price_changes") or []
        for change in changes:
            asset_id = str(change.get("asset_id", ""))
            with self._lock:
                target = self._resolve_target_book(asset_id)
                if target is None:
                    continue
                _, book = target
                self._upsert_price_level(
                    book=book,
                    side=str(change.get("side", "")).upper(),
                    price=self._to_float(change.get("price")),
                    size=self._to_float(change.get("size")),
                )
                self._apply_best_prices(
                    book=book,
                    best_bid=self._to_float(change.get("best_bid")),
                    best_ask=self._to_float(change.get("best_ask")),
                )
                book.timestamp = time.time()

    def _apply_best_bid_ask(self, message: dict) -> None:
        asset_id = str(message.get("asset_id", ""))
        with self._lock:
            target = self._resolve_target_book(asset_id)
            if target is None:
                return
            _, book = target
            self._apply_best_prices(
                book=book,
                best_bid=self._to_float(message.get("best_bid")),
                best_ask=self._to_float(message.get("best_ask")),
            )
            book.timestamp = time.time()

    def _resolve_target_book(self, asset_id: str) -> Optional[tuple[str, OrderBook]]:
        mapping = self._asset_to_market.get(asset_id)
        if mapping is None:
            return None
        market_id, side = mapping
        books = self._books_by_market.setdefault(market_id, MarketOrderBooks())
        return market_id, books.book_for_side(side)

    def _dispatch_or_queue_user_update(
        self,
        message: dict,
        handler_getter: Callable[[], Optional[Callable[[dict], None]]],
        queue_appender: Callable[[], None],
        label: str,
    ) -> None:
        with self._lock:
            handler = handler_getter()
        if handler is None:
            with self._lock:
                queue_appender()
            return
        try:
            handler(message)
        except Exception as exc:
            log.warn(f"ws user {label} handler crash: {exc}")
            with self._lock:
                queue_appender()

    def _parse_levels(self, raw_levels: list[dict], reverse: bool) -> list[OrderBookLevel]:
        levels: list[OrderBookLevel] = []
        for raw_level in raw_levels:
            price = self._to_float(raw_level.get("price"))
            size = self._to_float(raw_level.get("size"))
            if price is None or size is None:
                continue
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=reverse)
        return levels

    def _upsert_price_level(self, book: OrderBook, side: str, price: Optional[float], size: Optional[float]) -> None:
        if price is None or size is None:
            return
        levels = book.yes_bids if side == "BUY" else book.yes_asks
        levels[:] = [level for level in levels if abs(level.price - price) > 1e-9]
        if size > 0:
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda level: level.price, reverse=(side == "BUY"))

    def _apply_best_prices(self, book: OrderBook, best_bid: Optional[float], best_ask: Optional[float]) -> None:
        if best_bid is not None:
            book.yes_bids = [OrderBookLevel(price=best_bid, size=book.yes_bids[0].size if book.yes_bids else 0.0)] + [
                level for level in book.yes_bids if abs(level.price - best_bid) > 1e-9
            ]
            book.yes_bids.sort(key=lambda level: level.price, reverse=True)
        if best_ask is not None:
            book.yes_asks = [OrderBookLevel(price=best_ask, size=book.yes_asks[0].size if book.yes_asks else 0.0)] + [
                level for level in book.yes_asks if abs(level.price - best_ask) > 1e-9
            ]
            book.yes_asks.sort(key=lambda level: level.price)

    def _to_float(self, value: object) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
