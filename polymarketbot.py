"""
Main loop for the Polymarket bot.
Liquidity provision with deterministic hedging.
"""

from __future__ import annotations
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import sys
import warnings
from pathlib import Path
from typing import Any

try:
    from urllib3.exceptions import NotOpenSSLWarning
except ImportError:  # pragma: no cover - depends on the local urllib3 version
    NotOpenSSLWarning = None

if NotOpenSSLWarning is not None:
    # Avoid logging the user's local Python paths.
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)

from config.settings import settings
from data.clob_client import PolymarketClient
from data.market_scanner import MarketScanner
from data.metrics_tracker import MarketMetricsTracker
from data.models import Market, MarketOrderBooks, MarketStatus, Order, OrderStatus, PositionStatus, Side
from data.resolution_checker import ResolutionChecker
from data.websocket_manager import PolymarketWebSocketBridge
from data.rewards_checker import RewardsChecker
from strategy.quoter import Quoter, Quote
from strategy.inventory import InventoryManager
from strategy.hedger import Hedger
from execution.order_manager import OrderManager
from execution.rate_limiter import RateLimiter
from risk.risk_manager import RiskManager
from risk.kill_switch import KillSwitch
from risk.process_lock import ProcessLock, ProcessLockError
from risk.preflight import LivePreflight
from risk.stress_state import StressState
from observability import logger as log
from observability.audit import AuditLogger
from observability.metrics import Metrics
from observability.dashboard import Dashboard
from observability.reporting import SessionReporter, compute_target_end


def main():
    try:
        with ProcessLock(_resolve_process_lock_path()):
            _run_main()
    except ProcessLockError as exc:
        log.err(f"lock: {exc}")
        sys.exit(1)


def _run_main():
    use_mock_data = settings.dry_run and not settings.paper_trading
    started_at = _utc_now()
    target_end_at = compute_target_end(started_at, settings.session_duration_hours)

    # --- Configuration validation ---
    errors = settings.validate()
    if errors:
        for e in errors:
            log.err(f"config: {e}")
        if not settings.dry_run:
            log.err("configuration errors in LIVE mode, exiting")
            sys.exit(1)
        else:
            log.warn("configuration errors detected, continuing in DRY RUN")

    # --- Component initialization ---
    client = PolymarketClient(settings)
    rate_limiter = RateLimiter(settings.api.rate_limit_per_sec)
    order_manager = OrderManager(
        client,
        rate_limiter,
        dry_run=settings.dry_run,
        paper_trading=settings.paper_trading,
        hold_interval_sec=settings.trading.hold_interval_sec,
    )
    quoter = Quoter(settings.trading)
    inventory = InventoryManager(settings.trading.max_per_market, settings.trading.max_capital)
    hedger = Hedger(quoter)
    risk_manager = RiskManager(settings.trading, inventory)
    kill_switch = KillSwitch(order_manager)
    scanner = MarketScanner(client, settings)
    metrics = Metrics()
    order_manager.set_metrics(metrics)
    dashboard = Dashboard(settings.trading)
    preflight = LivePreflight(settings)
    ws_bridge: PolymarketWebSocketBridge | None = None
    runtime_lock = threading.RLock()

    # --- Connection ---
    log.info("starting Polymarket bot...")
    if not settings.dry_run:
        if not client.connect():
            log.err("unable to connect to the CLOB API")
            sys.exit(1)

        preflight_report = preflight.run(client)
        for note in preflight_report.notes:
            log.info(f"preflight: {note}")
        for warning in preflight_report.warnings:
            log.warn(f"preflight: {warning}")
        if not preflight_report.passed:
            for error in preflight_report.errors:
                log.err(f"preflight: {error}")
            log.err("live preflight failed, exiting")
            sys.exit(1)

        log.ok("live preflight completed")
    else:
        if settings.paper_trading:
            log.warn("PAPER TRADING mode - real market data, no real orders")
            log.warn(
                "paper trading: YES/NO can close as a pair without going through the live hedge path; "
                "fills/hour and PnL are optimistic versus live"
            )
        else:
            log.warn("DRY RUN mode - no real orders")

    mode_label = "[LIVE]"
    if settings.paper_trading:
        mode_label = "[PAPER]"
    elif settings.dry_run:
        mode_label = "[DRY RUN]"
    report_dir = _resolve_report_dir(settings.report_dir)
    reporter = SessionReporter(
        report_dir=report_dir,
        mode_label=mode_label,
        started_at=started_at,
        target_end_at=target_end_at,
        run_config=_build_run_config_snapshot(mode_label),
    )
    metrics_tracker = MarketMetricsTracker(report_dir / "market_metrics.json", mode_label)
    reporter.write_snapshot(metrics, inventory, status="starting")
    metrics_tracker.write_snapshot()

    # --- Initial market scan ---
    log.info("scanning markets...")
    if use_mock_data:
        # Use synthetic markets for dry-run testing
        active_markets = _generate_mock_markets()
        scanner.register_markets(active_markets)
        log.info(f"generated {len(active_markets)} mock markets for dry run")
    else:
        active_markets = scanner.scan_and_select()

    if not active_markets:
        log.warn("no market selected, exiting")
        reporter.write_final_summary(metrics, inventory, status="stopped", stop_reason="no_markets_selected")
        metrics_tracker.write_snapshot()
        return

    if not settings.dry_run:
        _recover_existing_state(
            client=client,
            scanner=scanner,
            order_manager=order_manager,
            inventory=inventory,
        )

    if not use_mock_data:
        ws_bridge = PolymarketWebSocketBridge(settings)
        ws_bridge.register_markets(
            _build_managed_markets(active_markets, scanner, inventory, order_manager)
        )
        user_auth = None if settings.dry_run else client.get_ws_auth()
        if not ws_bridge.start(user_auth=user_auth):
            ws_bridge = None
        else:
            metrics.record_ws_started(
                market_channel=True,
                user_channel=bool(user_auth),
            )

    wallet = client.get_address()
    if settings.paper_trading:
        wallet = "0xPAPER_TRADING"
    elif use_mock_data:
        wallet = "0xDRY_RUN_WALLET"

    audit_logger = AuditLogger(
        report_dir=report_dir,
        run_id=reporter.run_id,
        mode_label=mode_label,
    )
    order_manager.set_audit_logger(audit_logger)
    if ws_bridge is not None and not settings.dry_run:
        _install_immediate_ws_handlers(
            ws_bridge=ws_bridge,
            runtime_lock=runtime_lock,
            active_markets=active_markets,
            client=client,
            scanner=scanner,
            inventory=inventory,
            hedger=hedger,
            order_manager=order_manager,
            risk_manager=risk_manager,
            metrics=metrics,
            use_mock_data=use_mock_data,
            audit_logger=audit_logger,
            metrics_tracker=metrics_tracker,
        )

    dashboard.print_startup_banner(wallet, len(active_markets), mode_label)

    # --- Post-connection checker initialization ---
    resolution_checker = ResolutionChecker(settings, dry_run=use_mock_data)
    rewards_checker = RewardsChecker(
        settings,
        wallet_address=wallet,
        dry_run=use_mock_data,
        enabled=not settings.paper_trading,
    )
    stress_state = StressState(
        max_concurrent_recoveries=settings.trading.max_concurrent_recoveries,
        recovery_pauses_entry=settings.trading.recovery_pauses_entry,
        stress_unhedged_sec_trigger=settings.trading.stress_unhedged_sec_trigger,
    )

    # --- Main loop ---
    log.info(f"starting main loop - scan every {settings.trading.scan_interval_sec}s")
    stop_reason: str | None = None

    try:
        while not kill_switch.is_triggered:
            _scan_cycle(
                active_markets=active_markets,
                client=client,
                scanner=scanner,
                quoter=quoter,
                inventory=inventory,
                hedger=hedger,
                order_manager=order_manager,
                risk_manager=risk_manager,
                resolution_checker=resolution_checker,
                rewards_checker=rewards_checker,
                metrics=metrics,
                dashboard=dashboard,
                stress_state=stress_state,
                ws_bridge=ws_bridge,
                audit_logger=audit_logger,
                metrics_tracker=metrics_tracker,
                runtime_lock=runtime_lock,
            )

            reporter.write_snapshot(metrics, inventory, status="running")

            if target_end_at and _utc_now() >= target_end_at:
                stop_reason = "session_duration_reached"
                log.ok(f"test duration reached ({settings.session_duration_hours:.1f}h), stopping session")
                break

            if (
                settings.trading.target_entry_fill_events > 0
                and metrics.orders_filled >= settings.trading.target_entry_fill_events
            ):
                stop_reason = "target_entry_fill_events_reached"
                log.ok(
                    "target entry fills reached "
                    f"({metrics.orders_filled}/{settings.trading.target_entry_fill_events}), stopping session"
                )
                break

            runtime_stop_reason = _get_runtime_stop_reason(metrics)
            if runtime_stop_reason is not None:
                stop_reason = runtime_stop_reason
                break

            if not settings.dry_run and client.should_trigger_kill_switch():
                stop_reason = "api_kill_switch"
                kill_switch.trigger(
                    f"too many consecutive API errors ({client.consecutive_api_errors}): {client.last_api_error}"
                )
                break

            # Check kill switch
            if risk_manager.should_kill():
                stop_reason = "drawdown_limit_reached"
                kill_switch.trigger("drawdown limit reached")
                break

            # Status line
            dashboard.print_status_line(metrics, inventory.active_positions_count)

            # Sleep
            time.sleep(settings.trading.scan_interval_sec)

    except KeyboardInterrupt:
        stop_reason = "manual_interrupt"
        log.info("manual interruption")
    finally:
        log.info("shutdown in progress...")
        if ws_bridge is not None:
            ws_bridge.stop()
        open_before_shutdown = order_manager.live_order_count
        if order_manager.cancel_all():
            metrics.record_cancel(open_before_shutdown)
        metrics.update_open_orders(order_manager.live_order_count)
        final_status = _resolve_final_status(kill_switch.is_triggered, stop_reason)
        reporter.write_final_summary(metrics, inventory, status=final_status, stop_reason=stop_reason)
        metrics_tracker.write_snapshot()
        audit_logger.record(
            "session_end",
            status=final_status,
            stop_reason=stop_reason,
            total_pnl=metrics.total_pnl,
            entry_fill_events=metrics.orders_filled,
            hedge_fill_events=metrics.hedges_filled,
            open_orders=order_manager.live_order_count,
        )
        _print_final_summary(metrics, inventory, dashboard)


def _scan_cycle(
    active_markets: list[Market],
    client: PolymarketClient,
    scanner: MarketScanner,
    quoter: Quoter,
    inventory: InventoryManager,
    hedger: Hedger,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    resolution_checker: ResolutionChecker,
    rewards_checker: RewardsChecker,
    metrics: Metrics,
    dashboard: Dashboard,
    stress_state: StressState,
    ws_bridge: PolymarketWebSocketBridge | None = None,
    audit_logger: AuditLogger | None = None,
    metrics_tracker: MarketMetricsTracker | None = None,
    runtime_lock: threading.RLock | None = None,
):
    """One full cycle of scanning, quoting, fill checks, and hedging."""
    cycle_started_at = time.perf_counter()
    metrics.record_scan()
    tradable_markets = [
        market for market in active_markets
        if market.active and market.status == MarketStatus.ACTIVE
    ]
    managed_markets = _build_managed_markets(
        tradable_markets,
        scanner,
        inventory,
        order_manager,
    )
    log.info(f"scanning {len(tradable_markets)} markets...")
    use_mock_data = settings.dry_run and (not settings.paper_trading or client is None)
    books_started_at = time.perf_counter()
    books_by_market, ws_books_used, rest_books_used = _fetch_books_snapshot(
        managed_markets,
        client,
        use_mock_data,
        ws_bridge=ws_bridge,
    )
    metrics.record_book_fetch_latency(time.perf_counter() - books_started_at)
    metrics.record_book_source(ws_count=ws_books_used, rest_count=rest_books_used)
    metrics.record_book_age_samples(_collect_book_age_samples(books_by_market))
    runtime_guard = runtime_lock if runtime_lock is not None else nullcontext()
    with runtime_guard:
        if ws_bridge is not None and not settings.paper_trading:
            _consume_user_stream_updates(
                ws_bridge=ws_bridge,
                order_manager=order_manager,
                active_markets=active_markets,
                books_by_market=books_by_market,
                client=client,
                scanner=scanner,
                inventory=inventory,
                hedger=hedger,
                risk_manager=risk_manager,
                metrics=metrics,
                use_mock_data=use_mock_data,
                audit_logger=audit_logger,
                metrics_tracker=metrics_tracker,
            )

        if settings.paper_trading and books_by_market:
            fill_process_started_at = time.perf_counter()
            _process_new_fills(
                new_fills=order_manager.check_fills(market_books_by_id=books_by_market),
                active_markets=active_markets,
                books_by_market=books_by_market,
                client=client,
                scanner=scanner,
                inventory=inventory,
                hedger=hedger,
                order_manager=order_manager,
                risk_manager=risk_manager,
                metrics=metrics,
                use_mock_data=use_mock_data,
                metrics_tracker=metrics_tracker,
            )
            metrics.record_fill_process_latency(time.perf_counter() - fill_process_started_at)

        current_unrealized_pnl = inventory.get_unrealized_pnl(books_by_market)
        metrics.update_unrealized_pnl(current_unrealized_pnl)
        risk_manager.update_unrealized_pnl(current_unrealized_pnl)

        # Cleanup before quoting to avoid getting stuck at max_open_orders
        for market in managed_markets:
            cancelled = order_manager.cancel_stale_orders(market.condition_id, settings.trading.hold_interval_sec)
            if cancelled > 0:
                metrics.record_cancel(cancelled)

        recovery_market_ids = _manage_unhedged_positions(
            active_markets=active_markets,
            books_by_market=books_by_market,
            client=client,
            scanner=scanner,
            inventory=inventory,
            hedger=hedger,
            order_manager=order_manager,
            metrics=metrics,
            use_mock_data=use_mock_data,
            stress_state=stress_state,
            metrics_tracker=metrics_tracker,
        )

        quote_loop_started_at = time.perf_counter()
        for market in tradable_markets:
            books = books_by_market.get(market.condition_id)
            if books is None:
                continue

            log.info(
                f"  {market.short_question} | "
                f"YES mid={books.yes_mid_price*100:.0f}c spread={books.yes_spread_cents:.1f}c | "
                f"NO mid={books.no_mid_price*100:.0f}c spread={books.no_spread_cents:.1f}c"
            )

            if market.condition_id in recovery_market_ids:
                log.warn(f"  hold {market.short_question} | unhedged recovery hedge in progress")
                continue

            net_exposure = inventory.get_net_exposure(market.condition_id)
            quote = quoter.compute_quotes(books, net_exposure)

            if not quote.valid:
                if quote.reason:
                    if "book stale" in quote.reason:
                        log.warn(f"  skip {market.short_question} | {quote.reason}")
                    else:
                        log.info(f"  skip {market.short_question} | {quote.reason}")
                continue

            if order_manager.has_live_orders_for_market(market.condition_id):
                if order_manager.has_hedge_orders_for_market(market.condition_id):
                    metrics.record_skip_live_hedge()
                    log.info(f"  skip {market.short_question} | live hedge in progress")
                    continue

                entry_orders = order_manager.get_entry_orders_for_market(market.condition_id)
                if _entry_orders_too_young_for_reprice(
                    entry_orders,
                    min_order_age_sec=settings.trading.reprice_interval_sec,
                ):
                    metrics.record_skip_reprice_age()
                    log.info(f"  hold {market.short_question} | orders still too young")
                    continue

                if _should_reprice_entry_orders(
                    entry_orders,
                    quote,
                    price_threshold_cents=settings.trading.reprice_threshold_cents,
                ):
                    cancelled = order_manager.cancel_all_for_market(market.condition_id)
                    if cancelled > 0:
                        metrics.record_cancel(cancelled)
                        metrics.record_reprice(cancelled // 2 if cancelled > 1 else 1)
                        log.info(
                            f"  reprice {market.short_question} | cancelled {cancelled} entry orders"
                        )
                else:
                    metrics.record_skip_competitive()
                    log.info(f"  skip {market.short_question} | live orders still competitive")
                    continue

            global_check = risk_manager.check_global_limits(order_manager.live_order_count)
            if not global_check.passed:
                log.warn(f"global limit: {global_check.reason}")
                break

            pause, pause_reason = stress_state.should_pause_new_entries()
            if pause:
                log.warn(f"  hold {market.short_question} | {pause_reason}")
                continue

            cost = quote.yes_price * quote.size + quote.no_price * quote.size
            risk_check = risk_manager.check_can_place_order(
                market.condition_id,
                cost,
                reserved_market_cost=order_manager.get_reserved_cost_for_market(market.condition_id),
                reserved_total_cost=order_manager.get_total_reserved_cost(),
            )
            if not risk_check.passed:
                log.warn(f"  risk: {risk_check.reason}")
                continue

            log.ok(f"  placing YES@{quote.yes_cents:.0f}c NO@{quote.no_cents:.0f}c sum={quote.sum_cents:.0f}c")

            yes_order = order_manager.place_order(
                token_id=market.token_id_yes,
                side=Side.YES,
                price=quote.yes_price,
                size=quote.size,
                market_id=market.condition_id,
            )
            if yes_order:
                metrics.record_order()

            no_order = order_manager.place_order(
                token_id=market.token_id_no,
                side=Side.NO,
                price=quote.no_price,
                size=quote.size,
                market_id=market.condition_id,
            )
            if no_order:
                metrics.record_order()
            elif yes_order:
                log.err(f"  incomplete pair placement on {market.short_question}: rolling back YES order")
                if order_manager.cancel_order(yes_order.order_id):
                    metrics.record_cancel()
                else:
                    log.err(f"  rollback failed for order {yes_order.order_id[:8]}")
        metrics.record_quote_loop_latency(time.perf_counter() - quote_loop_started_at)

        trade_updates = None
        if ws_bridge is not None and not settings.paper_trading:
            pending_trade_updates = ws_bridge.drain_trade_updates()
            metrics.record_ws_trade_updates(len(pending_trade_updates))
            trade_updates = pending_trade_updates or None

        fill_process_started_at = time.perf_counter()
        _process_new_fills(
            new_fills=order_manager.check_fills(
                market_books_by_id=books_by_market,
                trade_updates=trade_updates,
            ),
            active_markets=active_markets,
            books_by_market=books_by_market,
            client=client,
            scanner=scanner,
            inventory=inventory,
            hedger=hedger,
            order_manager=order_manager,
            risk_manager=risk_manager,
            metrics=metrics,
            use_mock_data=use_mock_data,
            audit_logger=audit_logger,
            metrics_tracker=metrics_tracker,
        )
        metrics.record_fill_process_latency(time.perf_counter() - fill_process_started_at)

        current_unrealized_pnl = inventory.get_unrealized_pnl(books_by_market)
        metrics.update_unrealized_pnl(current_unrealized_pnl)
        risk_manager.update_unrealized_pnl(current_unrealized_pnl)

        active_ids = [m.condition_id for m in tradable_markets]
        reward_delta = rewards_checker.tick(active_ids)
        if reward_delta > 0:
            metrics.record_reward(reward_delta)
            risk_manager.update_pnl(reward_delta)

        resolution_checker.tick()

        open_position_ids = [
            mid for mid, pos in inventory.positions.items()
            if pos.status.value != "closed"
        ]
        if open_position_ids:
            should_poll_resolution = getattr(resolution_checker, "should_poll_now", True)
            if use_mock_data:
                events = resolution_checker.mock_check_positions(open_position_ids)
            elif should_poll_resolution:
                markets_with_positions = _get_markets_for_ids(open_position_ids, active_markets, scanner)
                events = resolution_checker.check_markets(markets_with_positions)
            else:
                events = []

            for event in events:
                pnl_delta = inventory.close_position(event.market_id, event.resolved_side)
                if pnl_delta != 0:
                    metrics.record_pnl(pnl_delta)
                    risk_manager.update_pnl(pnl_delta)
                metrics.record_resolution()

                cancelled = order_manager.cancel_all_for_market(event.market_id)
                if cancelled > 0:
                    metrics.record_cancel(cancelled)

                resolved_market = next(
                    (m for m in active_markets if m.condition_id == event.market_id), None
                )
                if resolved_market:
                    resolved_market.status = MarketStatus.RESOLVED
                    resolved_market.active = False

        metrics.update_open_orders(order_manager.live_order_count)
        if metrics_tracker is not None:
            metrics_tracker.sync_positions(inventory, books_by_market)
            metrics_tracker.write_snapshot()
    metrics.record_scan_cycle_latency(time.perf_counter() - cycle_started_at)


def _recover_existing_state(
    client: PolymarketClient,
    scanner: MarketScanner,
    order_manager: OrderManager,
    inventory: InventoryManager,
) -> list[Market]:
    recovered_market_ids: set[str] = set()

    open_orders = client.get_open_orders() or []
    for raw_order in open_orders:
        restored_order = _restore_order_from_raw(raw_order, scanner)
        if restored_order is None:
            continue
        order_manager.restore_live_order(restored_order)
        recovered_market_ids.add(restored_order.market_id)

    positions = client.get_positions() or []
    restored_positions = 0
    for raw_position in positions:
        restored = _restore_position_from_raw(raw_position, scanner)
        if restored is None:
            continue
        inventory.restore_market_side(
            market_id=restored["market_id"],
            side=restored["side"],
            size=restored["size"],
            price=restored["price"],
            question=restored["question"],
        )
        recovered_market_ids.add(restored["market_id"])
        restored_positions += 1

    for order in order_manager.live_orders.values():
        required_side, _ = inventory.get_required_hedge(order.market_id)
        if required_side is not None and order.side == required_side:
            order.is_hedge = True

    recovered_markets = _get_markets_for_ids(list(recovered_market_ids), [], scanner)
    if open_orders or restored_positions:
        log.warn(
            f"startup recovery: live orders={len(order_manager.live_orders)} restored positions={restored_positions}"
        )

    return recovered_markets


def _process_new_fills(
    new_fills: list[Order],
    active_markets: list[Market],
    books_by_market: dict[str, MarketOrderBooks],
    client: PolymarketClient,
    scanner: MarketScanner,
    inventory: InventoryManager,
    hedger: Hedger,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    metrics: Metrics,
    use_mock_data: bool,
    audit_logger: AuditLogger | None = None,
    metrics_tracker: MarketMetricsTracker | None = None,
) -> None:
    hedge_eps = 1e-9

    for fill in new_fills:
        fill_completed_at = fill.filled_at or time.time()
        fill_age_sec = max(0.0, fill_completed_at - fill.created_at)
        metrics.record_fill_age(fill_age_sec, is_hedge=fill.is_hedge)
        metrics.record_fill(
            source_order_id=fill.source_order_id or fill.order_id,
            is_hedge=fill.is_hedge,
        )

        market = _get_market_for_fill(active_markets, scanner, fill.market_id)
        question = market.question if market else fill.market_id[:20]
        pnl_delta = inventory.record_fill(
            market_id=fill.market_id,
            side=fill.side,
            price=fill.price,
            size=fill.size,
            question=question,
            is_hedge=fill.is_hedge,
        )
        if pnl_delta != 0:
            metrics.record_pnl(pnl_delta)
            risk_manager.update_pnl(pnl_delta)
            metrics.record_pair_lock_event(via_hedge=fill.is_hedge)
            log.lock(f"locked PnL +${pnl_delta:.4f} | cumulative ${metrics.realized_pnl:+.4f}")
        if audit_logger is not None:
            audit_logger.record(
                "order_fill",
                order_id=fill.order_id,
                source_order_id=fill.source_order_id or fill.order_id,
                market_id=fill.market_id,
                token_id=fill.token_id,
                side=fill.side.value,
                price=fill.price,
                size=fill.size,
                is_hedge=fill.is_hedge,
                fee_rate_bps=fill.fee_rate_bps,
                status=fill.status.value,
                pnl_delta=pnl_delta,
                cumulative_realized_pnl=metrics.realized_pnl,
                fill_age_ms=round(fill_age_sec * 1000.0, 3),
                question=question,
            )

        required_hedge_side, required_hedge_size = inventory.get_required_hedge(fill.market_id)
        fill_books = books_by_market.get(fill.market_id)
        if metrics_tracker is not None:
            if fill.is_hedge:
                hedge_fill_metrics = metrics_tracker.record_hedge_fill(
                    fill=fill,
                    market=market,
                    required_hedge_side=required_hedge_side,
                    required_hedge_size=required_hedge_size,
                    books=fill_books,
                    pnl_delta=pnl_delta,
                )
                if hedge_fill_metrics is not None:
                    if "hedge_submit_to_fill_ms" in hedge_fill_metrics:
                        metrics.record_hedge_submit_to_fill_latency(
                            hedge_fill_metrics["hedge_submit_to_fill_ms"] / 1000.0
                        )
                    metrics.record_unhedged_window_latency(
                        hedge_fill_metrics["unhedged_window_ms"] / 1000.0
                    )
                    metrics.record_hedge_slippage_cents(
                        hedge_fill_metrics["hedge_slippage_cents"]
                    )
                    metrics.record_adverse_move_cents(
                        hedge_fill_metrics["adverse_move_cents"]
                    )
            else:
                bypass_metrics = metrics_tracker.record_entry_fill(
                    fill=fill,
                    market=market,
                    required_hedge_side=required_hedge_side,
                    required_hedge_size=required_hedge_size,
                    books=fill_books,
                    pnl_delta=pnl_delta,
                )
                if bypass_metrics is not None:
                    metrics.record_unhedged_window_latency(
                        bypass_metrics["unhedged_window_ms"] / 1000.0
                    )
                    metrics.record_adverse_move_cents(
                        bypass_metrics["adverse_move_cents"]
                    )
        if required_hedge_side is None or required_hedge_size <= hedge_eps:
            cancelled = order_manager.cancel_orders_for_market_side(
                fill.market_id,
                hedges_only=True,
            )
            if cancelled > 0:
                metrics.record_cancel(cancelled)
                log.info(f"  cleared {cancelled} redundant hedge orders on {fill.market_id[:10]}...")
            continue

        opposite_hedge_side = Side.NO if required_hedge_side == Side.YES else Side.YES
        cancelled = order_manager.cancel_orders_for_market_side(
            fill.market_id,
            side=opposite_hedge_side,
            hedges_only=True,
        )
        if cancelled > 0:
            metrics.record_cancel(cancelled)
            log.info(
                f"  cleared {cancelled} opposite-side hedge orders on {fill.market_id[:10]}..."
            )

        hedge_only_coverage = order_manager.get_live_coverage_for_market(
            fill.market_id,
            required_hedge_side,
            hedges_only=True,
        )
        if hedge_only_coverage > required_hedge_size + hedge_eps:
            cancelled = order_manager.cancel_orders_for_market_side(
                fill.market_id,
                side=required_hedge_side,
                hedges_only=True,
            )
            if cancelled > 0:
                metrics.record_cancel(cancelled)
                log.info(
                    f"  cleared {cancelled} excess hedge orders on {fill.market_id[:10]}..."
                )

        live_coverage = order_manager.get_live_coverage_for_market(
            fill.market_id,
            required_hedge_side,
        )
        hedge_shortfall = max(0.0, required_hedge_size - live_coverage)
        if hedge_shortfall <= hedge_eps:
            continue

        if not market:
            continue

        hedge_books = books_by_market.get(fill.market_id)
        if hedge_books is None:
            if use_mock_data:
                hedge_books = _generate_mock_books(market)
            else:
                hedge_books = client.get_market_books(market.token_id_yes, market.token_id_no)
            if hedge_books is not None:
                books_by_market[fill.market_id] = hedge_books

        reference_price = inventory.get_reference_price_for_hedge(fill.market_id, required_hedge_side)
        if reference_price is None:
            continue

        hedge_compute_started_at = time.perf_counter()
        hedge_action = hedger.compute_target_hedge(
            market=market,
            books=hedge_books,
            hedge_side=required_hedge_side,
            reference_price=reference_price,
            hedge_size=hedge_shortfall,
        )
        hedge_compute_sec = time.perf_counter() - hedge_compute_started_at
        metrics.record_hedge_compute_latency(hedge_compute_sec)
        if not hedge_action.valid:
            continue

        hedge_order = order_manager.place_order(
            token_id=hedge_action.token_id,
            side=hedge_action.side,
            price=hedge_action.price,
            size=hedge_action.size,
            market_id=fill.market_id,
            is_hedge=True,
        )
        if hedge_order:
            metrics.record_hedge()
            fill_to_submit_ms = None
            if metrics_tracker is not None:
                hedge_submit_metrics = metrics_tracker.record_hedge_submit(
                    market=market,
                    hedge_side=hedge_action.side,
                    order_id=hedge_order.order_id,
                    price=hedge_action.price,
                    size=hedge_action.size,
                    books=hedge_books,
                )
                metrics.record_hedge_queue_estimate(
                    ahead_size=hedge_submit_metrics["queue_ahead_size"],
                    levels_ahead=int(hedge_submit_metrics["queue_levels_ahead"]),
                    gap_cents=hedge_submit_metrics["queue_gap_cents"],
                )
                fill_to_submit_ms = hedge_submit_metrics["fill_to_hedge_submit_ms"]
                metrics.record_fill_to_hedge_latency(fill_to_submit_ms / 1000.0)
            else:
                fill_to_submit_ms = max(0.0, time.time() - fill_completed_at) * 1000.0
                metrics.record_fill_to_hedge_latency(fill_to_submit_ms / 1000.0)
            log.hedg(
                f"hedge order {hedge_action.side.value} @ {hedge_action.price*100:.0f}c x{hedge_action.size:.1f} | "
                f"fill->submit={fill_to_submit_ms:.0f}ms compute={hedge_compute_sec*1000:.0f}ms"
            )


def _manage_unhedged_positions(
    active_markets: list[Market],
    books_by_market: dict[str, MarketOrderBooks],
    client: PolymarketClient,
    scanner: MarketScanner,
    inventory: InventoryManager,
    hedger: Hedger,
    order_manager: OrderManager,
    metrics: Metrics,
    use_mock_data: bool,
    stress_state: StressState,
    metrics_tracker: MarketMetricsTracker | None = None,
) -> set[str]:
    hedge_eps = 1e-9
    recovery_market_ids: set[str] = set()

    for position in inventory.get_all_active():
        required_hedge_side, required_hedge_size = inventory.get_required_hedge(position.market_id)
        if required_hedge_side is None or required_hedge_size <= hedge_eps:
            inventory.note_unhedged_scan(position.market_id, False)
            stress_state.clear_recovery(position.market_id)
            continue

        cycles = inventory.note_unhedged_scan(position.market_id, True)
        if cycles < settings.trading.unhedged_alert_cycles:
            continue

        recovery_market_ids.add(position.market_id)
        stress_state.mark_recovery(position.market_id, cycles)
        market = _get_market_for_fill(active_markets, scanner, position.market_id)
        if market is None:
            continue

        hedge_books = books_by_market.get(position.market_id)
        if (
            hedge_books is None or _market_book_age_seconds(hedge_books) > settings.trading.max_book_age_sec
        ) and not use_mock_data:
            refreshed_books = client.get_market_books(market.token_id_yes, market.token_id_no)
            if refreshed_books.has_both_books:
                hedge_books = refreshed_books
                books_by_market[position.market_id] = hedge_books
                _apply_books_snapshot(market, hedge_books)

        if hedge_books is None:
            continue

        unrealized_pnl = inventory.get_position_unrealized_pnl(position.market_id, hedge_books)
        current_pnl = position.pnl + unrealized_pnl
        book_age_sec = _market_book_age_seconds(hedge_books)
        log.warn(
            "UNHEDGED "
            f"{market.short_question} | cycles={cycles} side={required_hedge_side.value} "
            f"size={required_hedge_size:.4f} pnl=${current_pnl:+.4f} "
            f"unrealized=${unrealized_pnl:+.4f} book_age={book_age_sec:.1f}s"
        )

        cancelled_entries = order_manager.cancel_orders_for_market_side(
            position.market_id,
            side=required_hedge_side,
            hedges_only=False,
        )
        if cancelled_entries > 0:
            metrics.record_cancel(cancelled_entries)
            log.warn(
                f"  converted {cancelled_entries} {required_hedge_side.value} entry orders "
                f"into recovery hedge on {market.short_question}"
            )

        cancelled_hedges = order_manager.cancel_orders_for_market_side(
            position.market_id,
            side=required_hedge_side,
            hedges_only=True,
        )
        if cancelled_hedges > 0:
            metrics.record_cancel(cancelled_hedges)
            log.info(
                f"  refreshed {cancelled_hedges} {required_hedge_side.value} hedge orders "
                f"on {market.short_question}"
            )

        reference_price = inventory.get_reference_price_for_hedge(position.market_id, required_hedge_side)
        if reference_price is None:
            continue

        hedge_compute_started_at = time.perf_counter()
        hedge_action = hedger.compute_target_hedge(
            market=market,
            books=hedge_books,
            hedge_side=required_hedge_side,
            reference_price=reference_price,
            hedge_size=required_hedge_size,
        )
        hedge_compute_sec = time.perf_counter() - hedge_compute_started_at
        metrics.record_hedge_compute_latency(hedge_compute_sec)
        if not hedge_action.valid:
            log.warn(f"  skip hedge recovery {market.short_question} | {hedge_action.reason}")
            continue

        hedge_order = order_manager.place_order(
            token_id=hedge_action.token_id,
            side=hedge_action.side,
            price=hedge_action.price,
            size=hedge_action.size,
            market_id=position.market_id,
            is_hedge=True,
        )
        if hedge_order:
            metrics.record_hedge()
            fill_to_submit_ms = None
            if metrics_tracker is not None:
                hedge_submit_metrics = metrics_tracker.record_hedge_submit(
                    market=market,
                    hedge_side=hedge_action.side,
                    order_id=hedge_order.order_id,
                    price=hedge_action.price,
                    size=hedge_action.size,
                    books=hedge_books,
                )
                metrics.record_hedge_queue_estimate(
                    ahead_size=hedge_submit_metrics["queue_ahead_size"],
                    levels_ahead=int(hedge_submit_metrics["queue_levels_ahead"]),
                    gap_cents=hedge_submit_metrics["queue_gap_cents"],
                )
                fill_to_submit_ms = hedge_submit_metrics["fill_to_hedge_submit_ms"]
                metrics.record_fill_to_hedge_latency(fill_to_submit_ms / 1000.0)
            log.hedg(
                f"escalated hedge {hedge_action.side.value} @ {hedge_action.price*100:.0f}c "
                f"x{hedge_action.size:.4f} | unhedged_cycles={cycles} "
                f"fill->submit={(fill_to_submit_ms or 0.0):.0f}ms compute={hedge_compute_sec*1000:.0f}ms"
            )

    # Prune any market that was in recovery last cycle but is no longer unhedged.
    stress_state.clear_stale(recovery_market_ids)

    if recovery_market_ids:
        summary = stress_state.summary()
        log.warn(
            f"[STRESS] summary: active={summary['active_recoveries']} "
            f"oldest={summary['oldest_sec']}s avg={summary['avg_sec']}s"
        )

    return recovery_market_ids


def _consume_user_stream_updates(
    ws_bridge: PolymarketWebSocketBridge,
    order_manager: OrderManager,
    active_markets: list[Market],
    books_by_market: dict[str, MarketOrderBooks],
    client: PolymarketClient,
    scanner: MarketScanner,
    inventory: InventoryManager,
    hedger: Hedger,
    risk_manager: RiskManager,
    metrics: Metrics,
    use_mock_data: bool,
    audit_logger: AuditLogger | None = None,
    metrics_tracker: MarketMetricsTracker | None = None,
) -> None:
    trade_updates = ws_bridge.drain_trade_updates()
    metrics.record_ws_trade_updates(len(trade_updates))
    if trade_updates:
        fill_process_started_at = time.perf_counter()
        _process_new_fills(
            new_fills=order_manager.check_fills(
                market_books_by_id=books_by_market,
                trade_updates=trade_updates,
            ),
            active_markets=active_markets,
            books_by_market=books_by_market,
            client=client,
            scanner=scanner,
            inventory=inventory,
            hedger=hedger,
            order_manager=order_manager,
            risk_manager=risk_manager,
            metrics=metrics,
            use_mock_data=use_mock_data,
            audit_logger=audit_logger,
            metrics_tracker=metrics_tracker,
        )
        metrics.record_fill_process_latency(time.perf_counter() - fill_process_started_at)

    order_updates = ws_bridge.drain_order_updates()
    metrics.record_ws_order_updates(len(order_updates))
    cancelled = order_manager.apply_order_updates(order_updates)
    if cancelled > 0:
        metrics.record_cancel(cancelled)


def _install_immediate_ws_handlers(
    ws_bridge: PolymarketWebSocketBridge,
    runtime_lock: threading.RLock,
    active_markets: list[Market],
    client: PolymarketClient,
    scanner: MarketScanner,
    inventory: InventoryManager,
    hedger: Hedger,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    metrics: Metrics,
    use_mock_data: bool,
    audit_logger: AuditLogger | None = None,
    metrics_tracker: MarketMetricsTracker | None = None,
) -> None:
    def _trade_handler(trade_update: dict) -> None:
        with runtime_lock:
            _process_trade_update_immediately(
                trade_update=trade_update,
                ws_bridge=ws_bridge,
                active_markets=active_markets,
                client=client,
                scanner=scanner,
                inventory=inventory,
                hedger=hedger,
                order_manager=order_manager,
                risk_manager=risk_manager,
                metrics=metrics,
                use_mock_data=use_mock_data,
                audit_logger=audit_logger,
                metrics_tracker=metrics_tracker,
            )

    def _order_handler(order_update: dict) -> None:
        with runtime_lock:
            _apply_order_update_immediately(
                order_update=order_update,
                order_manager=order_manager,
                metrics=metrics,
            )

    ws_bridge.set_trade_update_handler(_trade_handler)
    ws_bridge.set_order_update_handler(_order_handler)


def _process_trade_update_immediately(
    trade_update: dict,
    ws_bridge: PolymarketWebSocketBridge,
    active_markets: list[Market],
    client: PolymarketClient,
    scanner: MarketScanner,
    inventory: InventoryManager,
    hedger: Hedger,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    metrics: Metrics,
    use_mock_data: bool,
    audit_logger: AuditLogger | None = None,
    metrics_tracker: MarketMetricsTracker | None = None,
) -> int:
    metrics.record_ws_trade_updates(1)
    fill_process_started_at = time.perf_counter()
    try:
        new_fills = order_manager.check_fills(trade_updates=[trade_update])
        if not new_fills:
            return 0

        books_by_market = _get_books_for_fill_markets(
            market_ids=[fill.market_id for fill in new_fills],
            active_markets=active_markets,
            scanner=scanner,
            client=client,
            ws_bridge=ws_bridge,
            use_mock_data=use_mock_data,
        )
        _process_new_fills(
            new_fills=new_fills,
            active_markets=active_markets,
            books_by_market=books_by_market,
            client=client,
            scanner=scanner,
            inventory=inventory,
            hedger=hedger,
            order_manager=order_manager,
            risk_manager=risk_manager,
            metrics=metrics,
            use_mock_data=use_mock_data,
            audit_logger=audit_logger,
            metrics_tracker=metrics_tracker,
        )
        if metrics_tracker is not None:
            metrics_tracker.sync_positions(inventory, books_by_market)
            metrics_tracker.write_snapshot()
        return len(new_fills)
    finally:
        metrics.record_fill_process_latency(time.perf_counter() - fill_process_started_at)


def _apply_order_update_immediately(
    order_update: dict,
    order_manager: OrderManager,
    metrics: Metrics,
) -> int:
    metrics.record_ws_order_updates(1)
    cancelled = order_manager.apply_order_updates([order_update])
    if cancelled > 0:
        metrics.record_cancel(cancelled)
    return cancelled


def _get_books_for_fill_markets(
    market_ids: list[str],
    active_markets: list[Market],
    scanner: MarketScanner,
    client: PolymarketClient,
    ws_bridge: PolymarketWebSocketBridge | None,
    use_mock_data: bool,
) -> dict[str, MarketOrderBooks]:
    unique_market_ids = list(dict.fromkeys(market_id for market_id in market_ids if market_id))
    books_by_market = ws_bridge.get_books_snapshot(unique_market_ids) if ws_bridge is not None else {}
    for market in _get_markets_for_ids(unique_market_ids, active_markets, scanner):
        if market.condition_id in books_by_market:
            _apply_books_snapshot(market, books_by_market[market.condition_id])
            continue
        if use_mock_data:
            books = _generate_mock_books(market)
        else:
            books = client.get_market_books(market.token_id_yes, market.token_id_no)
        if books is None or not books.has_both_books:
            continue
        books_by_market[market.condition_id] = books
        _apply_books_snapshot(market, books)
    return books_by_market


def _fetch_books_snapshot(
    tradable_markets: list[Market],
    client: PolymarketClient,
    use_mock_data: bool,
    ws_bridge: PolymarketWebSocketBridge | None = None,
) -> tuple[dict[str, MarketOrderBooks], int, int]:
    books_by_market = ws_bridge.get_books_snapshot(
        [market.condition_id for market in tradable_markets]
    ) if ws_bridge is not None else {}
    if not tradable_markets:
        return books_by_market, 0, 0

    missing_markets = [
        market for market in tradable_markets
        if market.condition_id not in books_by_market
    ]
    ws_books_used = len(books_by_market)
    rest_books_used = 0

    if use_mock_data or client is None:
        for market in missing_markets:
            books = _generate_mock_books(market)
            if not books.has_both_books:
                continue
            books_by_market[market.condition_id] = books
            _apply_books_snapshot(market, books)
        return books_by_market, ws_books_used, rest_books_used

    for market in tradable_markets:
        books = books_by_market.get(market.condition_id)
        if books is not None:
            _apply_books_snapshot(market, books)

    if not missing_markets:
        return books_by_market, ws_books_used, rest_books_used

    max_workers = min(len(missing_markets), max(1, settings.api.book_fetch_workers))
    if max_workers == 1:
        for market in missing_markets:
            books = client.get_market_books(market.token_id_yes, market.token_id_no)
            if not books.has_both_books:
                continue
            books_by_market[market.condition_id] = books
            _apply_books_snapshot(market, books)
            rest_books_used += 1
        return books_by_market, ws_books_used, rest_books_used

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_market = {
            executor.submit(client.get_market_books, market.token_id_yes, market.token_id_no): market
            for market in missing_markets
        }
        for future in as_completed(future_to_market):
            market = future_to_market[future]
            try:
                books = future.result()
            except Exception as exc:
                log.warn(f"book fetch error {market.short_question}: {exc}")
                continue
            if not books.has_both_books:
                continue
            books_by_market[market.condition_id] = books
            _apply_books_snapshot(market, books)
            rest_books_used += 1

    return books_by_market, ws_books_used, rest_books_used


def _collect_book_age_samples(books_by_market: dict[str, MarketOrderBooks]) -> list[float]:
    now = time.time()
    ages: list[float] = []
    for books in books_by_market.values():
        for book in (books.yes_book, books.no_book):
            if book.best_yes_bid is None and book.best_yes_ask is None:
                continue
            ages.append(max(0.0, now - book.timestamp))
    return ages


def _market_book_age_seconds(books: MarketOrderBooks) -> float:
    ages = _collect_book_age_samples({"market": books})
    if not ages:
        return 0.0
    return max(ages)


def _should_reprice_entry_orders(
    entry_orders: list[Order],
    desired_quote: Quote,
    max_order_age_sec: float | None = None,
    price_threshold_cents: float = 0.0,
) -> bool:
    if len(entry_orders) != 2:
        return False

    orders_by_side = {order.side: order for order in entry_orders}
    yes_order = orders_by_side.get(Side.YES)
    no_order = orders_by_side.get(Side.NO)
    if yes_order is None or no_order is None:
        return False

    yes_drift_cents = abs(yes_order.price - desired_quote.yes_price) * 100
    no_drift_cents = abs(no_order.price - desired_quote.no_price) * 100
    return yes_drift_cents >= price_threshold_cents or no_drift_cents >= price_threshold_cents


def _entry_orders_too_young_for_reprice(
    entry_orders: list[Order],
    min_order_age_sec: float,
) -> bool:
    if len(entry_orders) != 2:
        return False

    orders_by_side = {order.side: order for order in entry_orders}
    yes_order = orders_by_side.get(Side.YES)
    no_order = orders_by_side.get(Side.NO)
    if yes_order is None or no_order is None:
        return False

    youngest_order_age_sec = min(yes_order.age_seconds, no_order.age_seconds)
    return youngest_order_age_sec < min_order_age_sec


def _get_market_for_fill(
    active_markets: list[Market],
    scanner: MarketScanner,
    market_id: str,
) -> Market | None:
    """Resolve a market from the scanner cache, falling back to the cycle's active markets."""
    market = scanner.get_cached_market(market_id)
    if market:
        return market

    for active_market in active_markets:
        if active_market.condition_id == market_id:
            return active_market

    return None


def _build_managed_markets(
    active_markets: list[Market],
    scanner: MarketScanner,
    inventory: InventoryManager,
    order_manager: OrderManager,
) -> list[Market]:
    markets_by_id = {
        market.condition_id: market
        for market in active_markets
    }
    managed_ids = set(markets_by_id.keys())
    managed_ids.update(order_manager.get_live_market_ids())
    managed_ids.update(
        market_id
        for market_id, position in inventory.positions.items()
        if position.status != PositionStatus.CLOSED
    )

    for market_id in managed_ids:
        if market_id in markets_by_id:
            continue
        cached_market = scanner.get_cached_market(market_id)
        if cached_market is not None:
            markets_by_id[market_id] = cached_market

    return list(markets_by_id.values())


def _get_markets_for_ids(
    market_ids: list[str],
    active_markets: list[Market],
    scanner: MarketScanner,
) -> list[Market]:
    resolved_markets: list[Market] = []
    seen_market_ids: set[str] = set()

    for market_id in market_ids:
        if market_id in seen_market_ids:
            continue
        market = _get_market_for_fill(active_markets, scanner, market_id)
        if market is None:
            continue
        resolved_markets.append(market)
        seen_market_ids.add(market_id)

    return resolved_markets


def _restore_order_from_raw(raw_order: dict[str, Any], scanner: MarketScanner) -> Order | None:
    market, market_id, token_id = _resolve_market_from_raw(raw_order, scanner)
    side = _resolve_side_from_raw(raw_order, market, token_id)
    price = _find_first_float(raw_order, {"price", "limitprice", "limit_price"})
    size = _find_first_float(raw_order, {"size", "originalsize", "original_size", "amount", "shares"})
    filled_size = _find_first_float(
        raw_order,
        {"matchedamount", "matched_amount", "filledsize", "filled_size", "sizematched", "size_matched"},
    )
    remaining_size = _find_first_float(
        raw_order,
        {"remainingsize", "remaining_size", "remainingamount", "remaining_amount", "openamount", "open_size"},
    )

    if size is None and remaining_size is not None and filled_size is not None:
        size = remaining_size + filled_size
    elif size is None:
        size = remaining_size

    if filled_size is None:
        if size is not None and remaining_size is not None:
            filled_size = max(0.0, size - remaining_size)
        else:
            filled_size = 0.0

    order_id = _find_first_text(raw_order, {"id", "orderid", "order_id"})
    if not order_id or not market_id or side is None or price is None or size is None or size <= 0:
        return None

    return Order(
        order_id=order_id,
        source_order_id=order_id,
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        filled_size=min(max(filled_size, 0.0), size),
        status=OrderStatus.LIVE,
    )


def _restore_position_from_raw(raw_position: dict[str, Any], scanner: MarketScanner) -> dict[str, Any] | None:
    market, market_id, token_id = _resolve_market_from_raw(raw_position, scanner)
    side = _resolve_side_from_raw(raw_position, market, token_id)
    size = _find_first_float(raw_position, {"size", "amount", "balance", "shares"})
    price = _find_first_float(
        raw_position,
        {"avgprice", "avg_price", "averageprice", "entryprice", "entry_price", "price", "markprice"},
    )
    question = (market.question if market else "") or _find_first_text(raw_position, {"question", "title", "name"})

    if not market_id or side is None or size is None or size <= 0 or price is None or price < 0:
        return None

    return {
        "market_id": market_id,
        "side": side,
        "size": size,
        "price": price,
        "question": question,
    }


def _resolve_market_from_raw(
    raw_value: dict[str, Any],
    scanner: MarketScanner,
) -> tuple[Market | None, str, str]:
    market_id = _find_first_text(raw_value, {"conditionid", "condition_id", "marketid", "market_id"})
    token_id = _find_first_text(raw_value, {"tokenid", "token_id", "assetid", "asset_id", "asset"})

    market = scanner.get_cached_market(market_id) if market_id else None
    if market is None and token_id:
        market = scanner.get_cached_market_by_token_id(token_id)
        if market is not None:
            market_id = market.condition_id

    return market, market_id, token_id


def _resolve_side_from_raw(
    raw_value: dict[str, Any],
    market: Market | None,
    token_id: str,
) -> Side | None:
    if market is not None:
        if token_id and token_id == market.token_id_yes:
            return Side.YES
        if token_id and token_id == market.token_id_no:
            return Side.NO

    raw_side = _find_first_text(raw_value, {"outcome", "side"})
    if raw_side == "YES":
        return Side.YES
    if raw_side == "NO":
        return Side.NO

    return None


def _find_first_text(raw_value: Any, candidate_keys: set[str]) -> str:
    found = _find_first_value(raw_value, candidate_keys)
    if found is None:
        return ""
    return str(found).strip()


def _find_first_float(raw_value: Any, candidate_keys: set[str]) -> float | None:
    found = _find_first_value(raw_value, candidate_keys)
    if found in (None, ""):
        return None
    try:
        return float(found)
    except (TypeError, ValueError):
        return None


def _find_first_value(raw_value: Any, candidate_keys: set[str]) -> Any:
    normalized_candidates = {
        str(candidate_key).lower().replace("_", "")
        for candidate_key in candidate_keys
    }
    if isinstance(raw_value, dict):
        for key, child in raw_value.items():
            normalized = str(key).lower().replace("_", "")
            if normalized in normalized_candidates and not isinstance(child, (dict, list)):
                return child
        for child in raw_value.values():
            found = _find_first_value(child, candidate_keys)
            if found is not None:
                return found

    if isinstance(raw_value, list):
        for child in raw_value:
            found = _find_first_value(child, candidate_keys)
            if found is not None:
                return found

    return None


def _print_final_summary(metrics: Metrics, inventory: InventoryManager, dashboard: Dashboard):
    """Print the final summary."""
    log.info("=" * 50)
    log.info("SESSION SUMMARY")
    log.info("=" * 50)
    summary = metrics.summary()
    for key, val in summary.items():
        log.info(f"  {key:12s}: {val}")
    log.info(f"  {'positions':12s}: {inventory.active_positions_count}")
    log.info("=" * 50)


def _generate_mock_markets() -> list[Market]:
    """Generate mock markets for dry run."""
    from data.models import MarketStatus
    mocks = [
        ("Will BTC exceed $100k by end of March?", "mock_btc_100k"),
        ("Will ETH hit $5k in Q2?", "mock_eth_5k"),
        ("Will the Fed cut rates in March?", "mock_fed_cut"),
        ("Will GPT-5 be released before July?", "mock_gpt5"),
        ("Will SpaceX Starship reach orbit?", "mock_starship"),
    ]
    markets = []
    for question, cid in mocks:
        markets.append(Market(
            condition_id=cid,
            question=question,
            token_id_yes=f"{cid}_yes",
            token_id_no=f"{cid}_no",
            status=MarketStatus.ACTIVE,
            active=True,
            competition="low",
            mid_price=0.52,
            spread_cents=5.0,
            yes_mid_price=0.52,
            no_mid_price=0.47,
            yes_spread_cents=5.0,
            no_spread_cents=5.0,
        ))
    return markets


def _generate_mock_books(market: Market) -> MarketOrderBooks:
    """Generate mock YES/NO order books for dry run."""
    from data.models import OrderBook, OrderBookLevel
    import random
    yes_mid = market.yes_mid_price or market.mid_price or 0.50
    no_mid = market.no_mid_price or max(0.01, 0.99 - yes_mid)
    yes_spread = (market.yes_spread_cents or market.spread_cents or 5.0) / 100
    no_spread = (market.no_spread_cents or market.spread_cents or 5.0) / 100

    yes_noise = random.uniform(-0.01, 0.01)
    no_noise = random.uniform(-0.01, 0.01)
    yes_mid = min(0.98, max(0.02, yes_mid + yes_noise))
    no_mid = min(0.98, max(0.02, no_mid + no_noise))

    yes_book = OrderBook(
        yes_bids=[
            OrderBookLevel(price=round(yes_mid - yes_spread / 2, 2), size=10.0),
            OrderBookLevel(price=round(yes_mid - yes_spread / 2 - 0.01, 2), size=20.0),
        ],
        yes_asks=[
            OrderBookLevel(price=round(yes_mid + yes_spread / 2, 2), size=10.0),
            OrderBookLevel(price=round(yes_mid + yes_spread / 2 + 0.01, 2), size=20.0),
        ],
    )

    no_book = OrderBook(
        yes_bids=[
            OrderBookLevel(price=round(no_mid - no_spread / 2, 2), size=10.0),
            OrderBookLevel(price=round(no_mid - no_spread / 2 - 0.01, 2), size=20.0),
        ],
        yes_asks=[
            OrderBookLevel(price=round(no_mid + no_spread / 2, 2), size=10.0),
            OrderBookLevel(price=round(no_mid + no_spread / 2 + 0.01, 2), size=20.0),
        ],
    )

    return MarketOrderBooks(yes_book=yes_book, no_book=no_book)


def _apply_books_snapshot(market: Market, books: MarketOrderBooks) -> None:
    market.mid_price = books.yes_mid_price
    market.spread_cents = books.min_spread_cents
    market.yes_mid_price = books.yes_mid_price
    market.no_mid_price = books.no_mid_price
    market.yes_spread_cents = books.yes_spread_cents
    market.no_spread_cents = books.no_spread_cents


def _resolve_report_dir(report_dir: str):
    path = Path(report_dir)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def _build_run_config_snapshot(mode_label: str) -> dict[str, Any]:
    return {
        "mode": mode_label,
        "dry_run": settings.dry_run,
        "paper_trading": settings.paper_trading,
        "session_duration_hours": settings.session_duration_hours,
        "target_entry_orders": settings.trading.target_entry_orders,
        "target_entry_fill_events": settings.trading.target_entry_fill_events,
        "profit_target": settings.trading.profit_target,
        "drawdown_limit": settings.trading.drawdown_limit,
        "max_capital": settings.trading.max_capital,
        "max_per_market": settings.trading.max_per_market,
        "order_size": settings.trading.order_size,
        "max_markets": settings.trading.max_markets,
        "max_open_orders": settings.trading.max_open_orders,
        "allow_fee_enabled_markets": settings.trading.allow_fee_enabled_markets,
    }


def _get_runtime_stop_reason(metrics: Metrics) -> str | None:
    if (
        settings.trading.target_entry_orders > 0
        and metrics.orders_placed >= settings.trading.target_entry_orders
    ):
        log.ok(
            "target entry orders reached "
            f"({metrics.orders_placed}/{settings.trading.target_entry_orders}), stopping session"
        )
        return "target_entry_orders_reached"

    if (
        settings.trading.profit_target > 0
        and metrics.total_pnl >= settings.trading.profit_target
    ):
        log.ok(
            "profit target reached "
            f"(${metrics.total_pnl:+.2f} / ${settings.trading.profit_target:+.2f}), stopping session"
        )
        return "profit_target_reached"

    return None


def _resolve_final_status(kill_switch_triggered: bool, stop_reason: str | None) -> str:
    if kill_switch_triggered or stop_reason == "manual_interrupt":
        return "stopped"
    return "completed"


def _resolve_process_lock_path() -> Path:
    return Path(__file__).resolve().parent / ".polymarketbot.lock"


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


if __name__ == "__main__":
    main()
