import json
import logging
import pathlib
import sys
import time
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import Mock, patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import polymarketbot as main
from config.settings import settings
from data.models import Market, MarketOrderBooks, MarketStatus, Order, OrderBook, OrderBookLevel, OrderStatus, PositionStatus, Side
from data.resolution_checker import ResolutionChecker
from data.clob_client import PolymarketClient
from data.metrics_tracker import MarketMetricsTracker
from data.websocket_manager import PolymarketWebSocketBridge
from config.settings import TradingConfig
from execution.order_manager import OrderManager
from execution.rate_limiter import RateLimiter
from observability.audit import AuditLogger
from observability.logger import TradingFormatter
from observability.reporting import SessionReporter, compute_target_end
from risk.process_lock import ProcessLock, ProcessLockError
from risk.risk_manager import RiskCheck
from risk.preflight import LivePreflight
from strategy.hedger import Hedger
from strategy.inventory import InventoryManager
from strategy.quoter import Quoter


class _EmptyScanner:
    def get_cached_market(self, condition_id: str):
        return None


class _NoopResolutionChecker:
    def tick(self):
        return []

    def mock_check_positions(self, market_ids: list[str]):
        return []

    def check_markets(self, markets):
        return []


class _NoopRewardsChecker:
    def tick(self, active_market_ids: list[str]) -> float:
        return 0.0


class _NoopDashboard:
    pass


class _RiskStub:
    def __init__(self, max_open_orders: int):
        self.max_open_orders = max_open_orders

    def check_global_limits(self, open_orders: int) -> RiskCheck:
        if open_orders >= self.max_open_orders:
            return RiskCheck(False, "max open orders")
        return RiskCheck(True)

    def check_can_place_order(
        self,
        market_id: str,
        cost: float,
        reserved_market_cost: float = 0.0,
        reserved_total_cost: float = 0.0,
    ) -> RiskCheck:
        return RiskCheck(True)

    def update_pnl(self, pnl_delta: float):
        pass

    def update_unrealized_pnl(self, pnl_amount: float):
        pass


class _OrderManagerPreCleanupStub:
    def __init__(self):
        self._live_order_count = 10
        self.cancelled_markets: list[str] = []
        self.placed_orders: list[tuple[str, Side, float, float, str, bool]] = []

    @property
    def live_order_count(self) -> int:
        return self._live_order_count

    def cancel_stale_orders(self, market_id: str, max_age_sec: float) -> int:
        self.cancelled_markets.append(market_id)
        self._live_order_count = 8
        return 2

    def has_live_orders_for_market(self, market_id: str) -> bool:
        return False

    def has_hedge_orders_for_market(self, market_id: str) -> bool:
        return False

    def get_entry_orders_for_market(self, market_id: str) -> list[Order]:
        return []

    def get_reserved_cost_for_market(self, market_id: str) -> float:
        return 0.0

    def get_total_reserved_cost(self) -> float:
        return 0.0

    def get_live_market_ids(self) -> set[str]:
        return set()

    def place_order(self, token_id: str, side: Side, price: float, size: float,
                    market_id: str = "", is_hedge: bool = False):
        self.placed_orders.append((token_id, side, price, size, market_id, is_hedge))
        return Order(
            order_id=f"{market_id}:{side.value}",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.LIVE,
            is_hedge=is_hedge,
        )

    def check_fills(self, market_books_by_id=None, trade_updates=None):
        return []

    def cancel_all_for_market(self, market_id: str) -> int:
        return 0


class _OrderManagerDuplicateGuardStub(_OrderManagerPreCleanupStub):
    def __init__(self):
        super().__init__()
        self._live_order_count = 2

    def cancel_stale_orders(self, market_id: str, max_age_sec: float) -> int:
        self.cancelled_markets.append(market_id)
        return 0

    def has_live_orders_for_market(self, market_id: str) -> bool:
        return market_id == "market-live"


class _OrderManagerPaperFillOrderingStub(_OrderManagerPreCleanupStub):
    def __init__(self):
        super().__init__()
        self.events: list[str] = []

    def cancel_stale_orders(self, market_id: str, max_age_sec: float) -> int:
        self.events.append("cancel")
        return super().cancel_stale_orders(market_id, max_age_sec)

    def check_fills(self, market_books_by_id=None, trade_updates=None):
        self.events.append("check")
        return []


class _OrderManagerRepriceStub(_OrderManagerPreCleanupStub):
    def __init__(self, yes_price: float, no_price: float, order_age_sec: float = 0.0):
        super().__init__()
        created_at = time.time() - order_age_sec
        self.live_orders = {
            "ord-yes": Order(
                order_id="ord-yes",
                market_id="market-reprice",
                token_id="yes",
                side=Side.YES,
                price=yes_price,
                size=1.0,
                status=OrderStatus.LIVE,
                created_at=created_at,
            ),
            "ord-no": Order(
                order_id="ord-no",
                market_id="market-reprice",
                token_id="no",
                side=Side.NO,
                price=no_price,
                size=1.0,
                status=OrderStatus.LIVE,
                created_at=created_at,
            ),
        }
        self._live_order_count = len(self.live_orders)

    @property
    def live_order_count(self) -> int:
        return len(self.live_orders)

    def cancel_stale_orders(self, market_id: str, max_age_sec: float) -> int:
        self.cancelled_markets.append(market_id)
        return 0

    def has_live_orders_for_market(self, market_id: str) -> bool:
        return any(order.market_id == market_id for order in self.live_orders.values())

    def has_hedge_orders_for_market(self, market_id: str) -> bool:
        return any(order.market_id == market_id and order.is_hedge for order in self.live_orders.values())

    def get_entry_orders_for_market(self, market_id: str) -> list[Order]:
        return [
            order for order in self.live_orders.values()
            if order.market_id == market_id and not order.is_hedge
        ]

    def cancel_all_for_market(self, market_id: str) -> int:
        matching_ids = [order_id for order_id, order in self.live_orders.items() if order.market_id == market_id]
        for order_id in matching_ids:
            self.live_orders.pop(order_id, None)
        return len(matching_ids)

    def place_order(self, token_id: str, side: Side, price: float, size: float,
                    market_id: str = "", is_hedge: bool = False):
        placed = super().place_order(token_id, side, price, size, market_id, is_hedge)
        if placed:
            self.live_orders[placed.order_id] = placed
        return placed


class _TradesClient:
    def __init__(self, trades: list[dict]):
        self._trades = trades

    def get_trades(self) -> list[dict]:
        return list(self._trades)

    def place_order(self, *args, **kwargs):
        raise AssertionError("place_order non atteso in questo test")

    def cancel_order(self, order_id: str, dry_run: bool = False) -> bool:
        return True

    def cancel_all(self, dry_run: bool = False) -> bool:
        return True


class _PaperBooksClient:
    def get_market_books(self, token_id_yes: str, token_id_no: str) -> MarketOrderBooks:
        return MarketOrderBooks(
            yes_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.50, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.54, size=10.0)],
            ),
            no_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.44, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.48, size=10.0)],
            ),
        )


class _FeeAwareScannerClient(_PaperBooksClient):
    def __init__(self, fee_rates: dict[str, Optional[int]]):
        self._fee_rates = fee_rates
        self.last_api_error = ""

    def get_fee_rate(self, token_id: str, use_cache: bool = True):
        return self._fee_rates.get(token_id)

    def get_market_books(self, token_id_yes: str, token_id_no: str) -> MarketOrderBooks:
        return MarketOrderBooks(
            yes_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.49, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.54, size=10.0)],
            ),
            no_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.43, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.48, size=10.0)],
            ),
        )


class _RollbackPairClient(_PaperBooksClient):
    def __init__(self):
        self.place_calls = 0
        self.cancelled_order_ids: list[str] = []

    def get_trades(self) -> list[dict]:
        return []

    def place_order(self, token_id: str, side: Side, price: float, size: float, dry_run: bool = False):
        self.place_calls += 1
        if self.place_calls == 1:
            return Order(
                order_id="ord-yes",
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status=OrderStatus.LIVE,
            )
        return None

    def cancel_order(self, order_id: str, dry_run: bool = False) -> bool:
        self.cancelled_order_ids.append(order_id)
        return True

    def cancel_all(self, dry_run: bool = False) -> bool:
        return True


class _DrawdownBooksClient(_PaperBooksClient):
    def get_market_books(self, token_id_yes: str, token_id_no: str) -> MarketOrderBooks:
        return MarketOrderBooks(
            yes_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.38, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.42, size=10.0)],
            ),
            no_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.58, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.62, size=10.0)],
            ),
        )


class _RecoveryClientStub:
    def __init__(self, open_orders, positions):
        self._open_orders = open_orders
        self._positions = positions

    def get_open_orders(self):
        return list(self._open_orders)

    def get_positions(self):
        return list(self._positions)


class _LiveRestFallbackClient(_PaperBooksClient):
    def __init__(self, trades=None):
        self._trades = trades or []
        self.get_trades_calls = 0

    def get_trades(self) -> list[dict]:
        self.get_trades_calls += 1
        return list(self._trades)

    def place_order(self, *args, **kwargs):
        return None

    def cancel_order(self, order_id: str, dry_run: bool = False) -> bool:
        return True

    def cancel_all(self, dry_run: bool = False) -> bool:
        return True


class _ImmediateHedgeClient(_PaperBooksClient):
    def __init__(self):
        self.placed_orders: list[tuple[str, Side, float, float, bool]] = []

    def place_order(self, token_id: str, side: Side, price: float, size: float, dry_run: bool = False):
        self.placed_orders.append((token_id, side, price, size, dry_run))
        return Order(
            order_id=f"live-{len(self.placed_orders)}",
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.LIVE,
        )

    def cancel_order(self, order_id: str, dry_run: bool = False) -> bool:
        return True

    def cancel_all(self, dry_run: bool = False) -> bool:
        return True


class _WsTradeOrderManagerStub(_OrderManagerPreCleanupStub):
    def __init__(self):
        super().__init__()
        self.trade_updates_seen: list[list[dict] | None] = []
        self.cancel_updates_seen = 0

    def apply_order_updates(self, order_updates: list[dict]) -> int:
        self.cancel_updates_seen += len(order_updates)
        return len(order_updates)

    def check_fills(self, market_books_by_id=None, trade_updates=None):
        self.trade_updates_seen.append(trade_updates)
        return []


class _HedgeCoverageOrderManagerStub:
    def __init__(self, live_orders=None):
        self.live_orders = {
            order.order_id: order for order in (live_orders or [])
        }
        self.placed_orders: list[tuple[str, Side, float, float, str, bool]] = []
        self.cancelled_order_ids: list[str] = []

    def place_order(self, token_id: str, side: Side, price: float, size: float,
                    market_id: str = "", is_hedge: bool = False):
        order = Order(
            order_id=f"placed-{len(self.placed_orders)+1}",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.LIVE,
            is_hedge=is_hedge,
        )
        self.placed_orders.append((token_id, side, price, size, market_id, is_hedge))
        self.live_orders[order.order_id] = order
        return order

    def get_live_coverage_for_market(self, market_id: str, side: Side, hedges_only=None) -> float:
        return sum(
            order.remaining_size
            for order in self.live_orders.values()
            if order.market_id == market_id
            and order.side == side
            and (hedges_only is None or order.is_hedge == hedges_only)
        )

    def cancel_orders_for_market_side(self, market_id: str, side=None, hedges_only=None) -> int:
        matching_ids = [
            order_id for order_id, order in self.live_orders.items()
            if order.market_id == market_id
            and (side is None or order.side == side)
            and (hedges_only is None or order.is_hedge == hedges_only)
        ]
        for order_id in matching_ids:
            self.cancelled_order_ids.append(order_id)
            self.live_orders.pop(order_id, None)
        return len(matching_ids)


class _PreflightClientStub:
    def __init__(self, collateral, conditional, open_orders, positions):
        self._collateral = collateral
        self._conditional = conditional
        self._open_orders = open_orders
        self._positions = positions
        self.last_api_error = ""

    def get_address(self) -> str:
        return "0xabc"

    def get_balance_allowance(self, asset_type: str):
        if asset_type == "COLLATERAL":
            return self._collateral
        if asset_type == "CONDITIONAL":
            return self._conditional
        return None

    def get_open_orders(self):
        return self._open_orders

    def get_positions(self):
        return self._positions


class _ResolutionBranchStub:
    def __init__(self):
        self.should_poll_now = True
        self.mock_calls = 0
        self.real_calls = 0

    def tick(self):
        return []

    def mock_check_positions(self, market_ids: list[str]):
        self.mock_calls += 1
        return []

    def check_markets(self, markets):
        self.real_calls += 1
        return []


class DryRunHedgeLookupTests(unittest.TestCase):
    def test_get_market_for_fill_falls_back_to_active_markets(self):
        market = Market(
            condition_id="mock_market",
            question="Mock market",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )

        resolved = main._get_market_for_fill([market], _EmptyScanner(), market.condition_id)

        self.assertIs(resolved, market)


class InventoryResolutionTests(unittest.TestCase):
    def test_close_position_does_not_double_count_locked_pnl(self):
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)

        self.assertAlmostEqual(
            inventory.record_fill("m1", Side.YES, 0.49, 1.0, question="Market 1"),
            0.0,
        )
        self.assertAlmostEqual(
            inventory.record_fill("m1", Side.NO, 0.49, 1.0, question="Market 1"),
            0.02,
        )

        close_delta = inventory.close_position("m1", Side.YES)
        pos = inventory.get_position("m1")

        self.assertAlmostEqual(close_delta, 0.0)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.pnl, 0.02)

    def test_close_position_realizes_only_residual_unhedged_size(self):
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)

        self.assertAlmostEqual(
            inventory.record_fill("m2", Side.YES, 0.49, 2.0, question="Market 2"),
            0.0,
        )
        self.assertAlmostEqual(
            inventory.record_fill("m2", Side.NO, 0.49, 1.0, question="Market 2"),
            0.02,
        )

        close_delta = inventory.close_position("m2", Side.YES)
        pos = inventory.get_position("m2")

        self.assertAlmostEqual(close_delta, 0.51)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.pnl, 0.53)

    def test_close_position_with_unknown_outcome_keeps_position_open(self):
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)

        self.assertAlmostEqual(
            inventory.record_fill("m3", Side.YES, 0.49, 1.0, question="Market 3"),
            0.0,
        )
        self.assertAlmostEqual(
            inventory.record_fill("m3", Side.NO, 0.49, 1.0, question="Market 3"),
            0.02,
        )

        close_delta = inventory.close_position("m3", None)
        pos = inventory.get_position("m3")

        self.assertAlmostEqual(close_delta, 0.0)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.status, PositionStatus.HEDGED)
        self.assertAlmostEqual(pos.pnl, 0.02)

    def test_get_required_hedge_returns_side_and_size(self):
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        inventory.record_fill("m4", Side.YES, 0.49, 2.0, question="Market 4")
        inventory.record_fill("m4", Side.NO, 0.48, 0.5, question="Market 4")

        hedge_side, hedge_size = inventory.get_required_hedge("m4")

        self.assertEqual(hedge_side, Side.NO)
        self.assertAlmostEqual(hedge_size, 1.5)


class ResolutionCheckerRegressionTests(unittest.TestCase):
    def test_closed_market_without_outcome_does_not_emit_resolution_event(self):
        checker = ResolutionChecker(settings, dry_run=False)
        market = Market(
            condition_id="market-closed-no-outcome",
            question="Closed market without outcome",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [{
            "resolved": False,
            "closed": True,
            "outcomePrices": [],
        }]

        with patch("data.resolution_checker.requests.get", return_value=response):
            events = checker.check_markets([market])

        self.assertEqual(events, [])

    def test_resolution_checker_emits_real_resolution_once_per_market(self):
        checker = ResolutionChecker(settings, dry_run=False)
        market = Market(
            condition_id="market-resolved-once",
            question="Resolved once",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [{
            "resolved": True,
            "closed": True,
            "outcomePrices": ["1", "0"],
        }]

        with patch("data.resolution_checker.requests.get", return_value=response):
            first = checker.check_markets([market])
            second = checker.check_markets([market])

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].resolved_side, Side.YES)
        self.assertEqual(second, [])


class WebSocketBridgeTests(unittest.TestCase):
    def test_market_message_populates_book_cache(self):
        bridge = PolymarketWebSocketBridge(settings)
        market = Market(
            condition_id="m-ws",
            question="WS market",
            token_id_yes="token-yes",
            token_id_no="token-no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        bridge.register_markets([market])

        bridge.ingest_market_message({
            "event_type": "book",
            "asset_id": "token-yes",
            "bids": [{"price": "0.45", "size": "10"}],
            "asks": [{"price": "0.49", "size": "8"}],
        })
        bridge.ingest_market_message({
            "event_type": "book",
            "asset_id": "token-no",
            "bids": [{"price": "0.50", "size": "9"}],
            "asks": [{"price": "0.54", "size": "6"}],
        })

        snapshot = bridge.get_books_snapshot(["m-ws"])

        self.assertIn("m-ws", snapshot)
        self.assertAlmostEqual(snapshot["m-ws"].yes_book.best_yes_bid, 0.45)
        self.assertAlmostEqual(snapshot["m-ws"].no_book.best_yes_ask, 0.54)

    def test_user_message_queues_trade_and_order_updates(self):
        bridge = PolymarketWebSocketBridge(settings)
        bridge.ingest_user_message({
            "event_type": "trade",
            "status": "MATCHED",
            "maker_orders": [{"order_id": "ord-1", "price": "0.48", "matched_amount": "1.0"}],
        })
        bridge.ingest_user_message({
            "event_type": "order",
            "type": "CANCELLATION",
            "id": "ord-1",
        })

        trades = bridge.drain_trade_updates()
        orders = bridge.drain_order_updates()

        self.assertEqual(len(trades), 1)
        self.assertEqual(len(orders), 1)
        self.assertEqual(bridge.drain_trade_updates(), [])
        self.assertEqual(bridge.drain_order_updates(), [])

    def test_user_message_dispatches_immediate_callbacks_without_queueing(self):
        bridge = PolymarketWebSocketBridge(settings)
        seen_trades: list[str] = []
        seen_orders: list[str] = []
        bridge.set_trade_update_handler(lambda update: seen_trades.append(str(update.get("id", ""))))
        bridge.set_order_update_handler(lambda update: seen_orders.append(str(update.get("id", ""))))

        bridge.ingest_user_message({
            "event_type": "trade",
            "status": "MATCHED",
            "id": "trade-now",
            "maker_orders": [{"order_id": "ord-1", "price": "0.48", "matched_amount": "1.0"}],
        })
        bridge.ingest_user_message({
            "event_type": "order",
            "type": "CANCELLATION",
            "id": "ord-1",
        })

        self.assertEqual(seen_trades, ["trade-now"])
        self.assertEqual(seen_orders, ["ord-1"])
        self.assertEqual(bridge.drain_trade_updates(), [])
        self.assertEqual(bridge.drain_order_updates(), [])


class LoggingTests(unittest.TestCase):
    def test_formatter_supports_lock_tag(self):
        formatter = TradingFormatter()
        record = logging.LogRecord(
            name="bot",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="spread locked",
            args=(),
            exc_info=None,
        )
        record.tag = "LOCK"

        formatted = formatter.format(record)

        self.assertIn("[LOCK]", formatted)


class ScanCycleRegressionTests(unittest.TestCase):
    def setUp(self):
        self.prev_dry_run = settings.dry_run
        self.prev_paper_trading = settings.paper_trading
        self.prev_reprice_interval_sec = settings.trading.reprice_interval_sec
        self.prev_reprice_threshold_cents = settings.trading.reprice_threshold_cents
        settings.dry_run = True
        settings.paper_trading = False

    def tearDown(self):
        settings.dry_run = self.prev_dry_run
        settings.paper_trading = self.prev_paper_trading
        settings.trading.reprice_interval_sec = self.prev_reprice_interval_sec
        settings.trading.reprice_threshold_cents = self.prev_reprice_threshold_cents

    def test_scan_cycle_cleans_stale_orders_before_global_limit(self):
        market = Market(
            condition_id="market-cleanup",
            question="Market cleanup",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
            mid_price=0.52,
            spread_cents=5.0,
        )
        order_manager = _OrderManagerPreCleanupStub()

        main._scan_cycle(
            active_markets=[market],
            client=None,
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(order_manager.cancelled_markets, ["market-cleanup"])
        self.assertEqual(len(order_manager.placed_orders), 2)

    def test_scan_cycle_skips_market_with_existing_live_orders(self):
        market = Market(
            condition_id="market-live",
            question="Market duplicate guard",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
            mid_price=0.52,
            spread_cents=5.0,
        )
        order_manager = _OrderManagerDuplicateGuardStub()

        main._scan_cycle(
            active_markets=[market],
            client=None,
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(order_manager.cancelled_markets, ["market-live"])
        self.assertEqual(order_manager.placed_orders, [])

    def test_paper_trading_checks_fills_before_cancelling_stale_orders(self):
        settings.paper_trading = True
        market = Market(
            condition_id="market-paper-ordering",
            question="Market paper ordering",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
            mid_price=0.52,
            spread_cents=5.0,
        )
        order_manager = _OrderManagerPaperFillOrderingStub()

        main._scan_cycle(
            active_markets=[market],
            client=None,
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertGreaterEqual(len(order_manager.events), 2)
        self.assertEqual(order_manager.events[0], "check")
        self.assertIn("cancel", order_manager.events)

    def test_paper_trading_uses_real_resolution_checks(self):
        settings.paper_trading = True
        market = Market(
            condition_id="market-paper-resolution",
            question="Market paper resolution",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
        )
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        inventory.record_fill(market.condition_id, Side.YES, 0.49, 1.0, question=market.question)
        inventory.record_fill(market.condition_id, Side.NO, 0.49, 1.0, question=market.question)
        resolution_checker = _ResolutionBranchStub()

        main._scan_cycle(
            active_markets=[market],
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=inventory,
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=_OrderManagerPreCleanupStub(),
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=resolution_checker,
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(resolution_checker.mock_calls, 0)
        self.assertEqual(resolution_checker.real_calls, 1)

    def test_scan_cycle_skips_inactive_or_resolved_markets(self):
        inactive_market = Market(
            condition_id="market-resolved-skip",
            question="Resolved market should be skipped",
            token_id_yes="yes-skip",
            token_id_no="no-skip",
            status=MarketStatus.RESOLVED,
            active=False,
        )
        active_market = Market(
            condition_id="market-still-active",
            question="Active market should trade",
            token_id_yes="yes-active",
            token_id_no="no-active",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
            mid_price=0.52,
            spread_cents=5.0,
        )
        order_manager = _OrderManagerPreCleanupStub()

        main._scan_cycle(
            active_markets=[inactive_market, active_market],
            client=None,
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(order_manager.cancelled_markets, ["market-still-active"])
        self.assertEqual(
            [placed_order[4] for placed_order in order_manager.placed_orders],
            ["market-still-active", "market-still-active"],
        )

    def test_scan_cycle_reprices_entry_orders_when_quote_drifts(self):
        settings.paper_trading = True
        settings.trading.reprice_interval_sec = 15.0
        settings.trading.reprice_threshold_cents = 1.0
        market = Market(
            condition_id="market-reprice",
            question="Market reprice",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
        )
        order_manager = _OrderManagerRepriceStub(yes_price=0.47, no_price=0.41, order_age_sec=20.0)

        main._scan_cycle(
            active_markets=[market],
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(len(order_manager.placed_orders), 2)
        self.assertEqual(
            [(placed[1], placed[2]) for placed in order_manager.placed_orders],
            [(Side.YES, 0.50), (Side.NO, 0.44)],
        )

    def test_scan_cycle_keeps_competitive_entry_orders_without_repricing(self):
        settings.paper_trading = True
        settings.trading.reprice_threshold_cents = 1.0
        market = Market(
            condition_id="market-reprice",
            question="Market reprice stable",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
        )
        order_manager = _OrderManagerRepriceStub(yes_price=0.50, no_price=0.44)

        main._scan_cycle(
            active_markets=[market],
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(order_manager.placed_orders, [])

    def test_scan_cycle_holds_young_orders_even_when_quote_drifts(self):
        settings.paper_trading = True
        settings.trading.reprice_interval_sec = 15.0
        settings.trading.reprice_threshold_cents = 1.0
        market = Market(
            condition_id="market-reprice",
            question="Market reprice young",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
        )
        order_manager = _OrderManagerRepriceStub(yes_price=0.47, no_price=0.41, order_age_sec=5.0)
        metrics = main.Metrics()

        main._scan_cycle(
            active_markets=[market],
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=metrics,
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(order_manager.placed_orders, [])
        self.assertEqual(metrics.skip_reprice_age, 1)

    def test_scan_cycle_consumes_websocket_user_updates_before_quoting(self):
        market = Market(
            condition_id="market-ws-user",
            question="Market websocket user",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
            mid_price=0.52,
            spread_cents=5.0,
        )
        bridge = PolymarketWebSocketBridge(settings)
        bridge.register_markets([market])
        bridge.ingest_user_message({
            "event_type": "trade",
            "status": "MATCHED",
            "maker_orders": [{"order_id": "ord-1", "price": "0.48", "matched_amount": "1.0"}],
        })
        bridge.ingest_user_message({
            "event_type": "order",
            "type": "CANCELLATION",
            "id": "ord-1",
        })

        order_manager = _WsTradeOrderManagerStub()

        main._scan_cycle(
            active_markets=[market],
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
            ws_bridge=bridge,
        )

        self.assertEqual(order_manager.cancel_updates_seen, 1)
        self.assertTrue(order_manager.trade_updates_seen)
        self.assertEqual(len(order_manager.trade_updates_seen[0] or []), 1)

    def test_scan_cycle_falls_back_to_rest_trades_when_ws_queue_is_empty(self):
        settings.dry_run = False
        market = Market(
            condition_id="market-ws-rest-fallback",
            question="Market websocket rest fallback",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
        )
        bridge = PolymarketWebSocketBridge(settings)
        bridge.register_markets([market])
        client = _LiveRestFallbackClient()
        order_manager = OrderManager(
            client,
            RateLimiter(max_per_second=10_000),
            dry_run=False,
        )

        main._scan_cycle(
            active_markets=[market],
            client=client,
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
            ws_bridge=bridge,
        )

        self.assertEqual(client.get_trades_calls, 1)

    def test_scan_cycle_blocks_new_orders_when_unrealized_drawdown_is_breached(self):
        settings.paper_trading = True
        market = Market(
            condition_id="market-drawdown",
            question="Market drawdown guard",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
        )
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        inventory.record_fill(market.condition_id, Side.YES, 0.60, 1.0, question=market.question)
        risk_manager = main.RiskManager(
            TradingConfig(max_capital=100.0, max_per_market=10.0, drawdown_limit=-0.10),
            inventory,
        )
        order_manager = _OrderManagerPreCleanupStub()

        main._scan_cycle(
            active_markets=[market],
            client=_DrawdownBooksClient(),
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=inventory,
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=risk_manager,
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=main.Metrics(),
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(order_manager.placed_orders, [])
        self.assertAlmostEqual(risk_manager.unrealized_pnl, -0.20)
        self.assertTrue(risk_manager.should_kill())

    def test_scan_cycle_rolls_back_first_leg_when_second_leg_fails(self):
        settings.dry_run = False
        market = Market(
            condition_id="market-pair-rollback",
            question="Market pair rollback",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
        )
        client = _RollbackPairClient()
        order_manager = OrderManager(
            client,
            RateLimiter(max_per_second=10_000),
            dry_run=False,
        )
        metrics = main.Metrics()

        main._scan_cycle(
            active_markets=[market],
            client=client,
            scanner=_EmptyScanner(),
            quoter=Quoter(settings.trading),
            inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            resolution_checker=_NoopResolutionChecker(),
            rewards_checker=_NoopRewardsChecker(),
            metrics=metrics,
            dashboard=_NoopDashboard(),
        )

        self.assertEqual(client.cancelled_order_ids, ["ord-yes"])
        self.assertEqual(order_manager.live_orders, {})
        self.assertEqual(metrics.orders_cancelled, 1)


class OrderFillTrackingTests(unittest.TestCase):
    def test_dry_run_order_ids_are_unique(self):
        client = PolymarketClient(settings)

        first = client.place_order("token-yes", Side.YES, 0.48, 1.0, dry_run=True)
        second = client.place_order("token-no", Side.NO, 0.52, 1.0, dry_run=True)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.order_id, second.order_id)

    def test_check_fills_tracks_partial_and_full_trade_updates(self):
        trades = [
            {"id": "trade-1", "orderID": "ord-1", "size": "0.5", "price": "0.48"},
            {"id": "trade-1", "orderID": "ord-1", "size": "0.5", "price": "0.48"},
            {"id": "trade-2", "orderID": "ord-1", "size": "1.5", "price": "0.47"},
        ]
        manager = OrderManager(_TradesClient(trades), RateLimiter(max_per_second=10_000), dry_run=False)
        manager.live_orders["ord-1"] = Order(
            order_id="ord-1",
            market_id="m1",
            token_id="token-yes",
            side=Side.YES,
            price=0.49,
            size=2.0,
            status=OrderStatus.LIVE,
        )

        fills = manager.check_fills()

        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(fills[0].size, 0.5)
        self.assertEqual(fills[0].status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(fills[1].size, 1.5)
        self.assertEqual(fills[1].status, OrderStatus.FILLED)
        self.assertEqual(manager.live_orders, {})

        duplicate_poll = manager.check_fills()
        self.assertEqual(duplicate_poll, [])

    def test_check_fills_matches_trade_after_order_update_cancellation(self):
        manager = OrderManager(_TradesClient([]), RateLimiter(max_per_second=10_000), dry_run=False)
        manager.live_orders["ord-1"] = Order(
            order_id="ord-1",
            market_id="m1",
            token_id="token-yes",
            side=Side.YES,
            price=0.49,
            size=1.0,
            status=OrderStatus.LIVE,
        )

        cancelled = manager.apply_order_updates([
            {"id": "ord-1", "type": "CANCELLATION"},
        ])
        fills = manager.check_fills(trade_updates=[
            {"id": "trade-1", "orderID": "ord-1", "size": "1.0", "price": "0.48"},
        ])

        self.assertEqual(cancelled, 1)
        self.assertEqual(manager.live_orders, {})
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].source_order_id, "ord-1")
        self.assertEqual(fills[0].status, OrderStatus.FILLED)


class PaperTradingSimulationTests(unittest.TestCase):
    def test_paper_trading_fills_when_best_ask_touches_order_price(self):
        manager = OrderManager(
            _TradesClient([]),
            RateLimiter(max_per_second=10_000),
            dry_run=True,
            paper_trading=True,
            hold_interval_sec=60.0,
        )
        manager.live_orders["ord-yes"] = Order(
            order_id="ord-yes",
            market_id="m-paper",
            token_id="token-yes",
            side=Side.YES,
            price=0.48,
            size=1.0,
            status=OrderStatus.LIVE,
        )

        fills = manager.check_fills({
            "m-paper": MarketOrderBooks(
                yes_book=OrderBook(
                    yes_bids=[OrderBookLevel(price=0.47, size=10.0)],
                    yes_asks=[OrderBookLevel(price=0.48, size=10.0)],
                ),
                no_book=OrderBook(),
            )
        })

        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].size, 1.0)
        self.assertEqual(fills[0].status, OrderStatus.FILLED)
        self.assertEqual(manager.live_orders, {})

    def test_paper_trading_partial_fill_after_resting_top_of_book(self):
        manager = OrderManager(
            _TradesClient([]),
            RateLimiter(max_per_second=10_000),
            dry_run=True,
            paper_trading=True,
            hold_interval_sec=60.0,
        )
        manager.live_orders["ord-no"] = Order(
            order_id="ord-no",
            market_id="m-paper-2",
            token_id="token-no",
            side=Side.NO,
            price=0.45,
            size=1.0,
            status=OrderStatus.LIVE,
            created_at=time.time() - 180.0,
        )

        fills = manager.check_fills({
            "m-paper-2": MarketOrderBooks(
                yes_book=OrderBook(),
                no_book=OrderBook(
                    yes_bids=[OrderBookLevel(price=0.45, size=10.0)],
                    yes_asks=[OrderBookLevel(price=0.49, size=10.0)],
                ),
            )
        })

        self.assertEqual(len(fills), 1)
        self.assertGreater(fills[0].size, 0.0)
        self.assertLess(fills[0].size, 1.0)
        self.assertEqual(fills[0].status, OrderStatus.PARTIALLY_FILLED)
        self.assertIn("ord-no", manager.live_orders)

    def test_paper_trading_partial_fill_when_improving_best_bid(self):
        manager = OrderManager(
            _TradesClient([]),
            RateLimiter(max_per_second=10_000),
            dry_run=True,
            paper_trading=True,
            hold_interval_sec=60.0,
        )
        manager.live_orders["ord-yes-improved"] = Order(
            order_id="ord-yes-improved",
            market_id="m-paper-3",
            token_id="token-yes",
            side=Side.YES,
            price=0.46,
            size=1.0,
            status=OrderStatus.LIVE,
            created_at=time.time() - 180.0,
        )

        fills = manager.check_fills({
            "m-paper-3": MarketOrderBooks(
                yes_book=OrderBook(
                    yes_bids=[OrderBookLevel(price=0.44, size=10.0)],
                    yes_asks=[OrderBookLevel(price=0.49, size=10.0)],
                ),
                no_book=OrderBook(),
            )
        })

        self.assertEqual(len(fills), 1)
        self.assertGreater(fills[0].size, 0.0)
        self.assertLess(fills[0].size, 1.0)
        self.assertEqual(fills[0].status, OrderStatus.PARTIALLY_FILLED)
        self.assertIn("ord-yes-improved", manager.live_orders)


class RealBookStrategyTests(unittest.TestCase):
    def test_quoter_uses_real_no_book_instead_of_yes_complement(self):
        books = MarketOrderBooks(
            yes_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.48, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.52, size=10.0)],
            ),
            no_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.44, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.48, size=10.0)],
            ),
        )

        quote = Quoter(settings.trading).compute_quotes(books)

        self.assertTrue(quote.valid)
        self.assertAlmostEqual(quote.yes_price, 0.48)
        self.assertAlmostEqual(quote.no_price, 0.44)

    def test_hedger_prices_from_opposite_book(self):
        market = Market(
            condition_id="m-hedge",
            question="Hedge market",
            token_id_yes="yes-token",
            token_id_no="no-token",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        books = MarketOrderBooks(
            yes_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.48, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.52, size=10.0)],
            ),
            no_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.45, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.47, size=10.0)],
            ),
        )

        hedge = Hedger(Quoter(settings.trading)).compute_hedge(
            market=market,
            books=books,
            filled_side=Side.YES,
            filled_price=0.49,
            filled_size=1.0,
        )

        self.assertTrue(hedge.valid)
        self.assertEqual(hedge.side, Side.NO)
        self.assertEqual(hedge.token_id, "no-token")
        self.assertAlmostEqual(hedge.price, 0.47)

    def test_inventory_reports_only_unhedged_unrealized_pnl(self):
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        inventory.record_fill("m-u", Side.YES, 0.49, 2.0, question="Market u")
        inventory.record_fill("m-u", Side.NO, 0.49, 1.0, question="Market u")

        unrealized = inventory.get_unrealized_pnl({
            "m-u": MarketOrderBooks(
                yes_book=OrderBook(
                    yes_bids=[OrderBookLevel(price=0.52, size=10.0)],
                    yes_asks=[OrderBookLevel(price=0.54, size=10.0)],
                ),
                no_book=OrderBook(
                    yes_bids=[OrderBookLevel(price=0.44, size=10.0)],
                    yes_asks=[OrderBookLevel(price=0.46, size=10.0)],
                ),
            )
        })

        self.assertAlmostEqual(unrealized, 0.04)


class LivePreflightTests(unittest.TestCase):
    def setUp(self):
        self.prev_buffer = settings.trading.min_usdc_buffer
        self.prev_allow_orders = settings.trading.allow_existing_open_orders
        self.prev_allow_positions = settings.trading.allow_existing_positions
        settings.trading.min_usdc_buffer = 2.0
        settings.trading.allow_existing_open_orders = False
        settings.trading.allow_existing_positions = False

    def tearDown(self):
        settings.trading.min_usdc_buffer = self.prev_buffer
        settings.trading.allow_existing_open_orders = self.prev_allow_orders
        settings.trading.allow_existing_positions = self.prev_allow_positions

    def test_live_preflight_passes_clean_wallet(self):
        client = _PreflightClientStub(
            collateral={"balance": "75", "allowance": "100"},
            conditional={"approved": True},
            open_orders=[],
            positions=[],
        )

        report = LivePreflight(settings).run(client)

        self.assertTrue(report.passed)
        self.assertEqual(report.errors, [])

    def test_live_preflight_blocks_dirty_wallet_and_low_collateral(self):
        client = _PreflightClientStub(
            collateral={"balance": "20", "allowance": "10"},
            conditional={"approved": False},
            open_orders=[{"id": "ord-1"}],
            positions=[{"size": "1.0"}],
        )

        report = LivePreflight(settings).run(client)

        self.assertFalse(report.passed)
        self.assertTrue(any("insufficient USDC balance" in error for error in report.errors))
        self.assertTrue(any("insufficient USDC allowance" in error for error in report.errors))
        self.assertTrue(any("conditional tokens not approved" in error for error in report.errors))
        self.assertTrue(any("open orders" in error for error in report.errors))
        self.assertTrue(any("leftover positions" in error for error in report.errors))


class ApiHealthKillSwitchTests(unittest.TestCase):
    def test_client_triggers_kill_switch_after_repeated_api_errors(self):
        prev_threshold = settings.api.max_consecutive_api_errors
        settings.api.max_consecutive_api_errors = 2
        try:
            client = PolymarketClient(settings)

            client._record_api_error("test-op-1", "boom")
            self.assertFalse(client.should_trigger_kill_switch())

            client._record_api_error("test-op-2", "boom")
            self.assertTrue(client.should_trigger_kill_switch())
            self.assertEqual(client.consecutive_api_errors, 2)
        finally:
            settings.api.max_consecutive_api_errors = prev_threshold


class MetricsReportingTests(unittest.TestCase):
    def test_metrics_summary_distinguishes_open_orders_and_fill_rates(self):
        metrics = main.Metrics()

        metrics.record_order()
        metrics.record_order()
        metrics.record_hedge()
        metrics.record_hedge()
        metrics.record_fill(source_order_id="entry-1")
        metrics.record_fill(source_order_id="entry-1")
        metrics.record_fill(source_order_id="hedge-1", is_hedge=True)
        metrics.record_fill(source_order_id="hedge-1", is_hedge=True)
        metrics.record_cancel(3)
        metrics.update_open_orders(2)

        summary = metrics.summary()

        self.assertEqual(summary["entry_orders"], 2)
        self.assertEqual(summary["hedge_orders"], 2)
        self.assertEqual(summary["orders_total"], 4)
        self.assertEqual(summary["open_orders"], 2)
        self.assertEqual(summary["orders_cancelled"], 3)
        self.assertEqual(summary["entry_fill_events"], 2)
        self.assertEqual(summary["hedge_fill_events"], 2)
        self.assertEqual(summary["entry_fill_rate"], "50.0%")
        self.assertEqual(summary["hedge_fill_rate"], "50.0%")

    def test_metrics_summary_includes_latency_breakdown(self):
        metrics = main.Metrics()

        metrics.record_scan_cycle_latency(1.2)
        metrics.record_book_fetch_latency(0.4)
        metrics.record_quote_loop_latency(0.6)
        metrics.record_fill_check_latency(0.05)
        metrics.record_fill_process_latency(0.02)
        metrics.record_order_place_latency(0.03, is_hedge=False)
        metrics.record_order_place_latency(0.04, is_hedge=True)
        metrics.record_cancel_latency(0.01)
        metrics.record_cancel_all_latency(0.08)
        metrics.record_rate_limit_wait(0.005)
        metrics.record_fill_age(2.5, is_hedge=False)
        metrics.record_fill_age(1.5, is_hedge=True)
        metrics.record_fill_to_hedge_latency(0.12)
        metrics.record_hedge_submit_to_fill_latency(0.45)
        metrics.record_unhedged_window_latency(1.2)
        metrics.record_hedge_compute_latency(0.004)
        metrics.record_book_age_samples([0.2, 0.3])
        metrics.record_hedge_slippage_cents(1.5)
        metrics.record_adverse_move_cents(2.0)
        metrics.record_hedge_queue_estimate(ahead_size=12.0, levels_ahead=2, gap_cents=1.0)

        summary = metrics.summary()

        self.assertIn("n=1", summary["perf_scan_ms"])
        self.assertIn("n=1", summary["perf_books_ms"])
        self.assertIn("n=1", summary["perf_entry_place_ms"])
        self.assertIn("n=1", summary["perf_hedge_place_ms"])
        self.assertIn("n=2", summary["perf_book_age_ms"])
        self.assertIn("n=1", summary["perf_hedge_submit_to_fill_ms"])
        self.assertIn("n=1", summary["perf_unhedged_window_ms"])
        self.assertIn("n=1", summary["perf_hedge_slippage_c"])
        self.assertIn("n=1", summary["perf_queue_ahead_size"])


class MarketMetricsTrackerTests(unittest.TestCase):
    def test_tracker_records_entry_bypass_completion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = MarketMetricsTracker(pathlib.Path(tmpdir) / "market_metrics.json", "[PAPER]")
            inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
            market = Market(
                condition_id="market-bypass",
                question="Market bypass",
                token_id_yes="yes",
                token_id_no="no",
                status=MarketStatus.ACTIVE,
                active=True,
            )
            books = _PaperBooksClient().get_market_books(market.token_id_yes, market.token_id_no)

            fill_yes = Order(
                order_id="fill-yes",
                source_order_id="entry-yes",
                market_id=market.condition_id,
                token_id=market.token_id_yes,
                side=Side.YES,
                price=0.47,
                size=1.0,
                filled_size=1.0,
                status=OrderStatus.FILLED,
                filled_at=1000.0,
            )
            pnl_delta = inventory.record_fill(market.condition_id, Side.YES, 0.47, 1.0, question=market.question)
            required_side, required_size = inventory.get_required_hedge(market.condition_id)
            tracker.record_entry_fill(
                fill=fill_yes,
                market=market,
                required_hedge_side=required_side,
                required_hedge_size=required_size,
                books=books,
                pnl_delta=pnl_delta,
            )

            fill_no = Order(
                order_id="fill-no",
                source_order_id="entry-no",
                market_id=market.condition_id,
                token_id=market.token_id_no,
                side=Side.NO,
                price=0.48,
                size=1.0,
                filled_size=1.0,
                status=OrderStatus.FILLED,
                filled_at=1002.0,
            )
            pnl_delta = inventory.record_fill(market.condition_id, Side.NO, 0.48, 1.0, question=market.question)
            required_side, required_size = inventory.get_required_hedge(market.condition_id)
            tracker.record_entry_fill(
                fill=fill_no,
                market=market,
                required_hedge_side=required_side,
                required_hedge_size=required_size,
                books=books,
                pnl_delta=pnl_delta,
            )

            tracker.sync_positions(inventory, {market.condition_id: books})
            tracker.write_snapshot()
            payload = json.loads((pathlib.Path(tmpdir) / "market_metrics.json").read_text(encoding="utf-8"))

            self.assertEqual(payload["summary"]["pair_completion_via_entry_bypass"], 1)
            self.assertEqual(payload["summary"]["pair_completion_via_hedge"], 0)
            self.assertEqual(payload["markets"][0]["pending_hedge"], None)
            self.assertEqual(payload["markets"][0]["stats"]["unhedged_window_ms"]["count"], 1)

    def test_tracker_records_hedge_path_completion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = MarketMetricsTracker(pathlib.Path(tmpdir) / "market_metrics.json", "[PAPER]")
            inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
            market = Market(
                condition_id="market-hedge-path",
                question="Market hedge path",
                token_id_yes="yes",
                token_id_no="no",
                status=MarketStatus.ACTIVE,
                active=True,
            )
            books = _PaperBooksClient().get_market_books(market.token_id_yes, market.token_id_no)

            fill_yes = Order(
                order_id="fill-yes",
                source_order_id="entry-yes",
                market_id=market.condition_id,
                token_id=market.token_id_yes,
                side=Side.YES,
                price=0.47,
                size=1.0,
                filled_size=1.0,
                status=OrderStatus.FILLED,
                filled_at=1000.0,
            )
            pnl_delta = inventory.record_fill(market.condition_id, Side.YES, 0.47, 1.0, question=market.question)
            required_side, required_size = inventory.get_required_hedge(market.condition_id)
            tracker.record_entry_fill(
                fill=fill_yes,
                market=market,
                required_hedge_side=required_side,
                required_hedge_size=required_size,
                books=books,
                pnl_delta=pnl_delta,
            )

            with patch("data.metrics_tracker.time.time", return_value=1001.0):
                tracker.record_hedge_submit(
                    market=market,
                    hedge_side=Side.NO,
                    order_id="hedge-1",
                    price=0.48,
                    size=1.0,
                    books=books,
                )

            hedge_fill = Order(
                order_id="fill-hedge",
                source_order_id="hedge-1",
                market_id=market.condition_id,
                token_id=market.token_id_no,
                side=Side.NO,
                price=0.48,
                size=1.0,
                filled_size=1.0,
                status=OrderStatus.FILLED,
                filled_at=1003.0,
                is_hedge=True,
            )
            pnl_delta = inventory.record_fill(market.condition_id, Side.NO, 0.48, 1.0, question=market.question, is_hedge=True)
            required_side, required_size = inventory.get_required_hedge(market.condition_id)
            tracker.record_hedge_fill(
                fill=hedge_fill,
                market=market,
                required_hedge_side=required_side,
                required_hedge_size=required_size,
                books=books,
                pnl_delta=pnl_delta,
            )

            tracker.sync_positions(inventory, {market.condition_id: books})
            tracker.write_snapshot()
            payload = json.loads((pathlib.Path(tmpdir) / "market_metrics.json").read_text(encoding="utf-8"))

            self.assertEqual(payload["summary"]["pair_completion_via_hedge"], 1)
            self.assertEqual(payload["markets"][0]["stats"]["hedge_submit_to_fill_ms"]["count"], 1)
            self.assertEqual(payload["markets"][0]["stats"]["fill_to_hedge_submit_ms"]["count"], 1)


class FeeAwareMarketSelectionTests(unittest.TestCase):
    def setUp(self):
        self.prev_allow_fee_enabled = settings.trading.allow_fee_enabled_markets
        self.prev_max_fee_rate = settings.trading.max_allowed_fee_rate_bps
        self.prev_max_markets = settings.trading.max_markets
        self.prev_dry_run = settings.dry_run
        self.prev_paper_trading = settings.paper_trading
        settings.trading.max_markets = 2

    def tearDown(self):
        settings.trading.allow_fee_enabled_markets = self.prev_allow_fee_enabled
        settings.trading.max_allowed_fee_rate_bps = self.prev_max_fee_rate
        settings.trading.max_markets = self.prev_max_markets
        settings.dry_run = self.prev_dry_run
        settings.paper_trading = self.prev_paper_trading

    def test_scan_and_select_skips_fee_enabled_markets_by_default(self):
        settings.trading.allow_fee_enabled_markets = False
        settings.trading.max_allowed_fee_rate_bps = 10_000
        scanner = main.MarketScanner(
            _FeeAwareScannerClient({
                "fee-free-yes": 0,
                "fee-free-no": 0,
                "fee-yes": 20,
                "fee-no": 20,
            }),
            settings,
        )
        raw_markets = [
            {
                "conditionId": "market-fee-free",
                "question": "Fee free market",
                "clobTokenIds": ["fee-free-yes", "fee-free-no"],
                "active": True,
                "closed": False,
                "volume": 1000,
                "liquidity": 1000,
            },
            {
                "conditionId": "market-fee-enabled",
                "question": "Fee enabled market",
                "clobTokenIds": ["fee-yes", "fee-no"],
                "active": True,
                "closed": False,
                "volume": 900,
                "liquidity": 900,
            },
        ]

        with patch.object(scanner, "_fetch_from_gamma", return_value=raw_markets):
            selected = scanner.scan_and_select()

        self.assertEqual([market.condition_id for market in selected], ["market-fee-free"])
        self.assertFalse(selected[0].fee_enabled)

    def test_scan_and_select_keeps_fee_enabled_markets_when_allowed(self):
        settings.trading.allow_fee_enabled_markets = True
        settings.trading.max_allowed_fee_rate_bps = 25
        scanner = main.MarketScanner(
            _FeeAwareScannerClient({
                "fee-free-yes": 0,
                "fee-free-no": 0,
                "fee-yes": 20,
                "fee-no": 20,
            }),
            settings,
        )
        raw_markets = [
            {
                "conditionId": "market-fee-free",
                "question": "Fee free market",
                "clobTokenIds": ["fee-free-yes", "fee-free-no"],
                "active": True,
                "closed": False,
                "volume": 1000,
                "liquidity": 1000,
            },
            {
                "conditionId": "market-fee-enabled",
                "question": "Fee enabled market",
                "clobTokenIds": ["fee-yes", "fee-no"],
                "active": True,
                "closed": False,
                "volume": 900,
                "liquidity": 900,
            },
        ]

        with patch.object(scanner, "_fetch_from_gamma", return_value=raw_markets):
            selected = scanner.scan_and_select()

        self.assertEqual(
            [market.condition_id for market in selected],
            ["market-fee-free", "market-fee-enabled"],
        )
        self.assertTrue(selected[1].fee_enabled)
        self.assertEqual(selected[1].max_fee_rate_bps, 20)

    def test_scan_and_select_keeps_unknown_fee_markets_in_paper_mode(self):
        settings.dry_run = True
        settings.paper_trading = True
        settings.trading.allow_fee_enabled_markets = False
        settings.trading.max_allowed_fee_rate_bps = 10_000
        scanner = main.MarketScanner(
            _FeeAwareScannerClient({
                "unknown-fee-yes": None,
                "unknown-fee-no": None,
            }),
            settings,
        )
        raw_markets = [
            {
                "conditionId": "market-unknown-fee",
                "question": "Unknown fee market",
                "clobTokenIds": ["unknown-fee-yes", "unknown-fee-no"],
                "active": True,
                "closed": False,
                "volume": 1000,
                "liquidity": 1000,
            },
        ]

        with patch.object(scanner, "_fetch_from_gamma", return_value=raw_markets):
            selected = scanner.scan_and_select()

        self.assertEqual([market.condition_id for market in selected], ["market-unknown-fee"])
        self.assertIsNone(selected[0].max_fee_rate_bps)

    def test_scan_and_select_skips_unknown_fee_markets_in_live_mode(self):
        settings.dry_run = False
        settings.paper_trading = False
        settings.trading.allow_fee_enabled_markets = False
        settings.trading.max_allowed_fee_rate_bps = 10_000
        scanner = main.MarketScanner(
            _FeeAwareScannerClient({
                "unknown-fee-yes": None,
                "unknown-fee-no": None,
            }),
            settings,
        )
        raw_markets = [
            {
                "conditionId": "market-unknown-fee",
                "question": "Unknown fee market",
                "clobTokenIds": ["unknown-fee-yes", "unknown-fee-no"],
                "active": True,
                "closed": False,
                "volume": 1000,
                "liquidity": 1000,
            },
        ]

        with patch.object(scanner, "_fetch_from_gamma", return_value=raw_markets):
            selected = scanner.scan_and_select()

        self.assertEqual(selected, [])


class FeeRateParsingTests(unittest.TestCase):
    def test_extract_fee_rate_bps_supports_base_fee(self):
        client = PolymarketClient(settings)

        self.assertEqual(client._extract_fee_rate_bps({"base_fee": 0}), 0)
        self.assertEqual(client._extract_fee_rate_bps({"base_fee": 20}), 20)
        self.assertEqual(client._extract_fee_rate_bps({"base_fee": "0.002"}), 20)


class TradeTimestampParsingTests(unittest.TestCase):
    def test_order_manager_parses_iso_trade_timestamp(self):
        order_manager = OrderManager(
            PolymarketClient(settings),
            RateLimiter(max_per_second=10_000),
            dry_run=True,
            paper_trading=True,
        )

        parsed = order_manager._extract_trade_timestamp_seconds(
            {"createdAt": "2026-03-08T01:02:03+00:00"}
        )

        self.assertAlmostEqual(
            parsed,
            datetime(2026, 3, 8, 1, 2, 3, tzinfo=timezone.utc).timestamp(),
        )


class AuditLogTests(unittest.TestCase):
    def test_audit_log_records_order_fill_and_cancel_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_logger = AuditLogger(pathlib.Path(tmpdir), run_id="run-1", mode_label="[PAPER]")
            order_manager = OrderManager(
                PolymarketClient(settings),
                RateLimiter(max_per_second=10_000),
                dry_run=True,
                paper_trading=True,
            )
            order_manager.set_audit_logger(audit_logger)

            placed_order = order_manager.place_order(
                token_id="token-yes",
                side=Side.YES,
                price=0.48,
                size=1.0,
                market_id="market-audit",
            )
            self.assertIsNotNone(placed_order)
            self.assertTrue(order_manager.cancel_order(placed_order.order_id))

            main._process_new_fills(
                new_fills=[
                    Order(
                        order_id="fill-1",
                        source_order_id="entry-1",
                        market_id="market-audit",
                        token_id="token-yes",
                        side=Side.YES,
                        price=0.48,
                        size=1.0,
                        filled_size=1.0,
                        status=OrderStatus.FILLED,
                    )
                ],
                active_markets=[],
                books_by_market={},
                client=_PaperBooksClient(),
                scanner=_EmptyScanner(),
                inventory=InventoryManager(max_per_market=10.0, max_capital=100.0),
                hedger=Hedger(Quoter(settings.trading)),
                order_manager=_HedgeCoverageOrderManagerStub(),
                risk_manager=_RiskStub(max_open_orders=10),
                metrics=main.Metrics(),
                use_mock_data=False,
                audit_logger=audit_logger,
            )

            audit_entries = [
                json.loads(line)
                for line in (pathlib.Path(tmpdir) / "order_audit.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(
                [entry["event_type"] for entry in audit_entries],
                ["order_placed", "order_cancelled", "order_fill"],
            )
            self.assertEqual(audit_entries[0]["market_id"], "market-audit")
            self.assertEqual(audit_entries[2]["source_order_id"], "entry-1")


class QuoterBookAgeTests(unittest.TestCase):
    def test_compute_quotes_rejects_stale_books(self):
        config = TradingConfig(max_book_age_sec=60.0)
        quoter = Quoter(config)
        stale_ts = time.time() - 61.0
        books = MarketOrderBooks(
            yes_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.47, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.52, size=10.0)],
                timestamp=stale_ts,
            ),
            no_book=OrderBook(
                yes_bids=[OrderBookLevel(price=0.45, size=10.0)],
                yes_asks=[OrderBookLevel(price=0.50, size=10.0)],
                timestamp=stale_ts,
            ),
        )

        quote = quoter.compute_quotes(books)

        self.assertFalse(quote.valid)
        self.assertIn("book stale", quote.reason)


class FillProcessingHedgeTests(unittest.TestCase):
    def test_process_trade_update_immediately_places_hedge_without_waiting_next_scan(self):
        market = Market(
            condition_id="market-ws-hedge",
            question="Immediate websocket hedge",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        bridge = PolymarketWebSocketBridge(settings)
        bridge.register_markets([market])
        bridge.ingest_market_message({
            "event_type": "book",
            "asset_id": "yes",
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.54", "size": "10"}],
        })
        bridge.ingest_market_message({
            "event_type": "book",
            "asset_id": "no",
            "bids": [{"price": "0.44", "size": "10"}],
            "asks": [{"price": "0.48", "size": "10"}],
        })

        client = _ImmediateHedgeClient()
        order_manager = OrderManager(client, RateLimiter(max_per_second=10_000), dry_run=False)
        order_manager.live_orders["ord-yes"] = Order(
            order_id="ord-yes",
            market_id=market.condition_id,
            token_id=market.token_id_yes,
            side=Side.YES,
            price=0.47,
            size=1.0,
            status=OrderStatus.LIVE,
        )
        metrics = main.Metrics()
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)

        processed = main._process_trade_update_immediately(
            trade_update={
                "event_type": "trade",
                "status": "MATCHED",
                "id": "trade-1",
                "createdAt": "2026-03-08T12:00:00Z",
                "maker_orders": [{"order_id": "ord-yes", "price": "0.47", "matched_amount": "1.0"}],
            },
            ws_bridge=bridge,
            active_markets=[market],
            client=client,
            scanner=_EmptyScanner(),
            inventory=inventory,
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            metrics=metrics,
            use_mock_data=False,
        )

        self.assertEqual(processed, 1)
        self.assertEqual(metrics.orders_filled, 1)
        self.assertEqual(metrics.hedges_placed, 1)
        self.assertEqual(len(client.placed_orders), 1)
        self.assertEqual(client.placed_orders[0][1], Side.NO)

    def test_process_new_fills_skips_new_hedge_when_live_opposite_entry_covers_exposure(self):
        market = Market(
            condition_id="market-hedge-cover",
            question="Market hedge cover",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        books = _PaperBooksClient().get_market_books(market.token_id_yes, market.token_id_no)
        order_manager = _HedgeCoverageOrderManagerStub(live_orders=[
            Order(
                order_id="live-no-entry",
                market_id=market.condition_id,
                token_id=market.token_id_no,
                side=Side.NO,
                price=0.48,
                size=1.0,
                status=OrderStatus.LIVE,
                is_hedge=False,
            )
        ])
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        metrics = main.Metrics()

        main._process_new_fills(
            new_fills=[
                Order(
                    order_id="fill-yes",
                    source_order_id="entry-yes",
                    market_id=market.condition_id,
                    token_id=market.token_id_yes,
                    side=Side.YES,
                    price=0.47,
                    size=1.0,
                    filled_size=1.0,
                    status=OrderStatus.FILLED,
                )
            ],
            active_markets=[market],
            books_by_market={market.condition_id: books},
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            inventory=inventory,
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            metrics=metrics,
            use_mock_data=False,
        )

        self.assertEqual(order_manager.placed_orders, [])
        self.assertEqual(order_manager.cancelled_order_ids, [])
        self.assertEqual(metrics.hedges_placed, 0)

    def test_process_new_fills_cancels_redundant_hedge_when_position_becomes_balanced(self):
        market = Market(
            condition_id="market-balanced",
            question="Market balanced",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        books = _PaperBooksClient().get_market_books(market.token_id_yes, market.token_id_no)
        order_manager = _HedgeCoverageOrderManagerStub(live_orders=[
            Order(
                order_id="live-no-hedge",
                market_id=market.condition_id,
                token_id=market.token_id_no,
                side=Side.NO,
                price=0.53,
                size=1.0,
                status=OrderStatus.LIVE,
                is_hedge=True,
            )
        ])
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        inventory.record_fill(market.condition_id, Side.YES, 0.47, 1.0, question=market.question)
        metrics = main.Metrics()

        main._process_new_fills(
            new_fills=[
                Order(
                    order_id="fill-no-entry",
                    source_order_id="entry-no",
                    market_id=market.condition_id,
                    token_id=market.token_id_no,
                    side=Side.NO,
                    price=0.48,
                    size=1.0,
                    filled_size=1.0,
                    status=OrderStatus.FILLED,
                )
            ],
            active_markets=[market],
            books_by_market={market.condition_id: books},
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            inventory=inventory,
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            metrics=metrics,
            use_mock_data=False,
        )

        self.assertEqual(order_manager.placed_orders, [])
        self.assertEqual(order_manager.cancelled_order_ids, ["live-no-hedge"])
        self.assertEqual(metrics.orders_cancelled, 1)

    def test_process_new_fills_does_not_hedge_a_hedge_fill_again(self):
        market = Market(
            condition_id="market-hedge-loop",
            question="Market hedge loop",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        books = _PaperBooksClient().get_market_books(market.token_id_yes, market.token_id_no)
        order_manager = _HedgeCoverageOrderManagerStub(live_orders=[
            Order(
                order_id="live-no-hedge",
                market_id=market.condition_id,
                token_id=market.token_id_no,
                side=Side.NO,
                price=0.53,
                size=1.0,
                filled_size=0.5,
                status=OrderStatus.PARTIALLY_FILLED,
                is_hedge=True,
            )
        ])
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        inventory.record_fill(market.condition_id, Side.YES, 0.47, 1.0, question=market.question)
        metrics = main.Metrics()

        main._process_new_fills(
            new_fills=[
                Order(
                    order_id="fill-no-hedge",
                    source_order_id="live-no-hedge",
                    market_id=market.condition_id,
                    token_id=market.token_id_no,
                    side=Side.NO,
                    price=0.53,
                    size=0.5,
                    filled_size=0.5,
                    status=OrderStatus.PARTIALLY_FILLED,
                    is_hedge=True,
                )
            ],
            active_markets=[market],
            books_by_market={market.condition_id: books},
            client=_PaperBooksClient(),
            scanner=_EmptyScanner(),
            inventory=inventory,
            hedger=Hedger(Quoter(settings.trading)),
            order_manager=order_manager,
            risk_manager=_RiskStub(max_open_orders=10),
            metrics=metrics,
            use_mock_data=False,
        )

        self.assertEqual(order_manager.placed_orders, [])
        self.assertEqual(metrics.hedges_placed, 0)

    def test_manage_unhedged_positions_escalates_after_three_cycles(self):
        market = Market(
            condition_id="market-unhedged-recovery",
            question="Market unhedged recovery",
            token_id_yes="yes",
            token_id_no="no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        books = _PaperBooksClient().get_market_books(market.token_id_yes, market.token_id_no)
        order_manager = _HedgeCoverageOrderManagerStub(live_orders=[
            Order(
                order_id="live-no-entry",
                market_id=market.condition_id,
                token_id=market.token_id_no,
                side=Side.NO,
                price=0.48,
                size=1.0,
                status=OrderStatus.LIVE,
                is_hedge=False,
            )
        ])
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)
        inventory.record_fill(market.condition_id, Side.YES, 0.47, 1.0, question=market.question)
        metrics = main.Metrics()

        with patch.object(main.settings.trading, "unhedged_alert_cycles", 3):
            first = main._manage_unhedged_positions(
                active_markets=[market],
                books_by_market={market.condition_id: books},
                client=_PaperBooksClient(),
                scanner=_EmptyScanner(),
                inventory=inventory,
                hedger=Hedger(Quoter(settings.trading)),
                order_manager=order_manager,
                metrics=metrics,
                use_mock_data=False,
            )
            second = main._manage_unhedged_positions(
                active_markets=[market],
                books_by_market={market.condition_id: books},
                client=_PaperBooksClient(),
                scanner=_EmptyScanner(),
                inventory=inventory,
                hedger=Hedger(Quoter(settings.trading)),
                order_manager=order_manager,
                metrics=metrics,
                use_mock_data=False,
            )
            third = main._manage_unhedged_positions(
                active_markets=[market],
                books_by_market={market.condition_id: books},
                client=_PaperBooksClient(),
                scanner=_EmptyScanner(),
                inventory=inventory,
                hedger=Hedger(Quoter(settings.trading)),
                order_manager=order_manager,
                metrics=metrics,
                use_mock_data=False,
            )

        self.assertEqual(first, set())
        self.assertEqual(second, set())
        self.assertEqual(third, {market.condition_id})
        self.assertEqual(order_manager.cancelled_order_ids, ["live-no-entry"])
        self.assertEqual(len(order_manager.placed_orders), 1)
        self.assertEqual(order_manager.placed_orders[0][1], Side.NO)
        self.assertTrue(order_manager.placed_orders[0][5])
        self.assertEqual(metrics.hedges_placed, 1)


class ReservedCapitalRiskTests(unittest.TestCase):
    def test_risk_manager_blocks_when_live_orders_exhaust_total_capital(self):
        inventory = InventoryManager(max_per_market=5.0, max_capital=1.0)
        risk_manager = main.RiskManager(
            TradingConfig(max_capital=1.0, max_per_market=5.0, drawdown_limit=-5.0),
            inventory,
        )

        result = risk_manager.check_can_place_order(
            market_id="m-capital",
            cost=0.30,
            reserved_market_cost=0.30,
            reserved_total_cost=0.80,
        )

        self.assertFalse(result.passed)
        self.assertIn("max capital reached", result.reason)

    def test_risk_manager_blocks_when_live_orders_exhaust_market_limit(self):
        inventory = InventoryManager(max_per_market=1.0, max_capital=10.0)
        risk_manager = main.RiskManager(
            TradingConfig(max_capital=10.0, max_per_market=1.0, drawdown_limit=-5.0),
            inventory,
        )

        result = risk_manager.check_can_place_order(
            market_id="m-market",
            cost=0.30,
            reserved_market_cost=0.80,
            reserved_total_cost=0.80,
        )

        self.assertFalse(result.passed)
        self.assertIn("per-market exposure limit reached", result.reason)

    def test_risk_manager_uses_unrealized_pnl_for_drawdown(self):
        inventory = InventoryManager(max_per_market=10.0, max_capital=10.0)
        risk_manager = main.RiskManager(
            TradingConfig(max_capital=10.0, max_per_market=10.0, drawdown_limit=-0.50),
            inventory,
        )

        risk_manager.update_pnl(0.20)
        risk_manager.update_unrealized_pnl(-0.80)
        result = risk_manager.check_can_place_order(
            market_id="m-drawdown",
            cost=0.10,
        )

        self.assertFalse(result.passed)
        self.assertIn("drawdown limit reached", result.reason)
        self.assertAlmostEqual(risk_manager.total_pnl, -0.60)
        self.assertTrue(risk_manager.should_kill())


class StartupRecoveryTests(unittest.TestCase):
    def test_recover_existing_state_restores_orders_and_positions(self):
        market = Market(
            condition_id="market-recover",
            question="Recovered market",
            token_id_yes="token-yes",
            token_id_no="token-no",
            status=MarketStatus.ACTIVE,
            active=True,
        )
        scanner = main.MarketScanner(Mock(), settings)
        scanner.register_markets([market])
        client = _RecoveryClientStub(
            open_orders=[
                {
                    "id": "ord-1",
                    "tokenId": "token-no",
                    "price": "0.51",
                    "size": "1.0",
                }
            ],
            positions=[
                {
                    "conditionId": "market-recover",
                    "tokenId": "token-yes",
                    "avgPrice": "0.49",
                    "size": "2.0",
                }
            ],
        )
        order_manager = OrderManager(client, RateLimiter(max_per_second=10_000), dry_run=False)
        inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)

        recovered_markets = main._recover_existing_state(client, scanner, order_manager, inventory)

        self.assertEqual([m.condition_id for m in recovered_markets], ["market-recover"])
        self.assertIn("ord-1", order_manager.live_orders)
        self.assertTrue(order_manager.live_orders["ord-1"].is_hedge)
        position = inventory.get_position("market-recover")
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.yes_size, 2.0)
        self.assertAlmostEqual(position.yes_price, 0.49)
        self.assertEqual(position.no_size, 0.0)


class ProcessLockTests(unittest.TestCase):
    def test_process_lock_blocks_second_instance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = pathlib.Path(tmpdir) / "bot.lock"

            with ProcessLock(lock_path):
                with self.assertRaises(ProcessLockError):
                    with ProcessLock(lock_path):
                        pass


class SessionReportingTests(unittest.TestCase):
    def test_session_reporter_writes_single_run_history_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reporter = SessionReporter(
                report_dir=pathlib.Path(tmpdir),
                mode_label="[PAPER]",
                started_at=datetime.now(timezone.utc),
                target_end_at=compute_target_end(datetime.now(timezone.utc), 120.0),
                run_config={"target_entry_orders": 25, "profit_target": 1.5},
            )
            metrics = main.Metrics()
            inventory = InventoryManager(max_per_market=10.0, max_capital=100.0)

            reporter.write_snapshot(metrics, inventory, status="starting")
            reporter.write_snapshot(metrics, inventory, status="running")
            reporter.write_final_summary(
                metrics,
                inventory,
                status="completed",
                stop_reason="profit_target_reached",
            )

            history_path = pathlib.Path(tmpdir) / "run_history.jsonl"
            self.assertTrue(history_path.exists())
            self.assertEqual(sorted(pathlib.Path(tmpdir).iterdir()), [history_path])

            entries = [
                json.loads(line)
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([entry["status"] for entry in entries], ["starting", "completed"])
            self.assertEqual(len({entry["run_id"] for entry in entries}), 1)
            self.assertIsNone(entries[0]["ended_at_utc"])
            self.assertIsNotNone(entries[1]["ended_at_utc"])
            self.assertEqual(entries[0]["run_config"]["target_entry_orders"], 25)
            self.assertEqual(entries[1]["stop_reason"], "profit_target_reached")


class RuntimeStopReasonTests(unittest.TestCase):
    def test_runtime_stop_reason_for_target_entry_orders(self):
        metrics = main.Metrics()
        metrics.record_order()
        metrics.record_order()

        with patch.object(main.settings.trading, "target_entry_orders", 2), patch.object(
            main.settings.trading,
            "profit_target",
            0.0,
        ):
            self.assertEqual(main._get_runtime_stop_reason(metrics), "target_entry_orders_reached")

    def test_runtime_stop_reason_for_profit_target(self):
        metrics = main.Metrics()
        metrics.record_pnl(1.25)

        with patch.object(main.settings.trading, "target_entry_orders", 0), patch.object(
            main.settings.trading,
            "profit_target",
            1.0,
        ):
            self.assertEqual(main._get_runtime_stop_reason(metrics), "profit_target_reached")


if __name__ == "__main__":
    unittest.main()
