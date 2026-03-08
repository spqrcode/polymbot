"""
Terminal dashboard with session, config, and position panels.
Uses the rich library for rendering.
"""

from __future__ import annotations
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.live import Live

from config.settings import TradingConfig
from observability.metrics import Metrics


class Dashboard:
    """Terminal dashboard powered by rich."""

    def __init__(self, config: TradingConfig):
        self.console = Console()
        self.config = config
        self._live: Optional[Live] = None

    def render_session(self, metrics: Metrics) -> Panel:
        """SESSION panel."""
        summary = metrics.summary()
        text = Text()
        text.append(f"Uptime:   {summary['uptime']}\n", style="cyan")
        text.append(f"Entry:    {summary['entry_orders']} placed / {summary['entry_fill_events']} fills ({summary['entry_fill_rate']})\n")
        text.append(f"Hedge:    {summary['hedge_orders']} placed / {summary['hedge_fill_events']} fills ({summary['hedge_fill_rate']})\n")
        text.append(f"Open:     {summary['open_orders']}  | Canc: {summary['orders_cancelled']}\n")
        text.append(f"Resolved: {summary['resolved']}\n")
        text.append(
            f"Debug:    ws(m/u) {summary['debug_ws_market']}/{summary['debug_ws_user']} | "
            f"books ws/rest {summary['debug_books_ws']}/{summary['debug_books_rest']}\n",
            style="dim",
        )
        text.append(
            f"          ws trades/orders {summary['debug_ws_trades']}/{summary['debug_ws_orders']} | "
            f"reprices {summary['debug_reprices']}\n",
            style="dim",
        )
        text.append(
            f"          skip young/competitive/hedge "
            f"{summary['debug_skip_reprice_age']}/"
            f"{summary['debug_skip_competitive']}/"
            f"{summary['debug_skip_hedge_live']} | "
            f"pairs hedge/bypass {summary['debug_pairs_hedge_path']}/"
            f"{summary['debug_pairs_entry_bypass']}\n",
            style="dim",
        )
        text.append(
            f"Perf:     cyc/books/quote {metrics.scan_cycle_latency.compact()} / "
            f"{metrics.book_fetch_latency.compact()} / {metrics.quote_loop_latency.compact()}\n",
            style="dim",
        )
        text.append(
            f"          fill chk/proc {metrics.fill_check_latency.compact()} / "
            f"{metrics.fill_process_latency.compact()} | place e/h "
            f"{metrics.entry_place_latency.avg_only()} / {metrics.hedge_place_latency.avg_only()}\n",
            style="dim",
        )
        text.append(
            f"          cancel {metrics.cancel_latency.avg_only()} | fill age e/h "
            f"{metrics.entry_fill_age_latency.avg_only()} / {metrics.hedge_fill_age_latency.avg_only()} | "
            f"fill->submit {metrics.fill_to_hedge_latency.avg_only()}\n",
            style="dim",
        )
        text.append(
            f"          hedge submit->fill {metrics.hedge_submit_to_fill_latency.avg_only()} | "
            f"unhedged win {metrics.unhedged_window_latency.avg_only()} | "
            f"slip {metrics.hedge_slippage_cents.avg_only('c')} | "
            f"drift {metrics.adverse_move_cents.avg_only('c')}\n",
            style="dim",
        )
        text.append(
            f"          queue ahead {metrics.hedge_queue_ahead_size.avg_only()} sz / "
            f"{metrics.hedge_queue_levels_ahead.avg_only()} lvl | gap "
            f"{metrics.hedge_queue_gap_cents.avg_only('c')}\n",
            style="dim",
        )
        text.append(
            f"          book age {metrics.book_age_latency.compact()} | rate wait "
            f"{metrics.rate_limit_wait_latency.avg_only()}",
            style="dim",
        )
        text.append("\n", style="dim")
        text.append(f"Rewards:  {summary['rewards']}\n", style="blue")
        text.append(f"PnL:      {summary['pnl']}\n",
                     style="green" if metrics.realized_pnl >= 0 else "red")
        text.append(f"uPnL:     {summary['unrealized']}\n",
                     style="green" if metrics.unrealized_pnl >= 0 else "red")
        text.append(f"Total:    {summary['total']}",
                     style="bold green" if metrics.total_pnl >= 0 else "bold red")
        return Panel(text, title="SESSION", border_style="cyan")

    def render_config(self) -> Panel:
        """CONFIG panel."""
        text = Text()
        text.append(f"Capital:    ${self.config.max_capital:.0f}\n")
        text.append(f"Per market: ${self.config.max_per_market:.0f}\n")
        text.append(f"Order size: ${self.config.order_size:.1f}\n")
        text.append(f"Min spread: {self.config.min_spread_cents:.0f}c\n")
        text.append(f"Max sum:    {self.config.max_sum_cents:.0f}c\n")
        text.append(f"Markets:    {self.config.max_markets}\n")
        text.append(f"Competition:{self.config.competition}\n")
        text.append(f"Hold:       {self.config.hold_interval_sec:.0f}s\n")
        text.append(f"Drawdown:   ${self.config.drawdown_limit:.0f}")
        return Panel(text, title="CONFIG", border_style="yellow")

    def render_positions(self, positions: list[dict]) -> Panel:
        """POSITIONS panel."""
        if not positions:
            return Panel(Text("no open positions", style="dim"),
                         title="POSITIONS", border_style="green")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Market", style="white", max_width=35)
        table.add_column("YES", style="cyan", justify="right")
        table.add_column("NO", style="magenta", justify="right")
        table.add_column("Status", justify="center")
        table.add_column("Cost", justify="right")

        for pos in positions:
            status_style = "green" if pos["status"] == "hedged" else "yellow"
            table.add_row(
                pos["market"],
                pos["yes"],
                pos["no"],
                Text(pos["status"], style=status_style),
                pos["cost"],
            )

        return Panel(table, title="POSITIONS", border_style="green")

    def render_full(self, metrics: Metrics, positions: list[dict]) -> Layout:
        """Full dashboard render."""
        layout = Layout()
        layout.split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )

        # Right side: session + config + positions
        layout["right"].split_column(
            Layout(self.render_session(metrics), name="session", size=12),
            Layout(self.render_config(), name="config", size=12),
            Layout(self.render_positions(positions), name="positions"),
        )

        return layout

    def print_startup_banner(self, wallet: str, markets_count: int, mode_label: str):
        """Startup banner."""
        mode_styles = {
            "[DRY RUN]": "yellow",
            "[PAPER]": "bold blue",
            "[LIVE]": "bold red",
        }
        mode_style = mode_styles.get(mode_label, "yellow")

        self.console.print()
        self.console.print(Panel(
            Text.from_markup(
                f"[bold cyan]Polymarket Liquidity Bot[/bold cyan]\n"
                f"[{mode_style}]{mode_label}[/{mode_style}]\n\n"
                f"Wallet:  {wallet[:10]}...{wallet[-6:]}\n"
                f"Markets: {markets_count}\n"
                f"Capital: ${self.config.max_capital:.0f}\n"
                f"Spread:  >= {self.config.min_spread_cents:.0f}c\n"
                f"Max Sum: {self.config.max_sum_cents:.0f}c"
            ),
            title="STARTUP",
            border_style="cyan",
        ))
        self.console.print()

    def print_status_line(self, metrics: Metrics, positions_count: int):
        """Compact status line for logs without panels."""
        summary = metrics.summary()
        pnl_color = "green" if metrics.total_pnl >= 0 else "red"
        self.console.print(
            f"  [dim]scan #{summary['scans']}[/dim] | "
            f"entry:{summary['entry_orders']} hedge:{summary['hedge_orders']} open:{summary['open_orders']} | "
            f"fills:{summary['entry_fill_events']}/{summary['hedge_fill_events']} | "
            f"ws books:{summary['debug_books_ws']}/{summary['debug_books_rest']} | "
            f"repr:{summary['debug_reprices']} | "
            f"hold:{summary['debug_skip_reprice_age']} | "
            f"pairs h/b:{summary['debug_pairs_hedge_path']}/{summary['debug_pairs_entry_bypass']} | "
            f"positions:{positions_count} | "
            f"uPnL:{summary['unrealized']} | "
            f"rewards:{summary['rewards']} | "
            f"[{pnl_color}]total:{summary['total']}[/{pnl_color}] | "
            f"up:{summary['uptime']}"
        )
        self.console.print(
            f"  [dim]perf[/dim] | "
            f"cyc/books/quote {metrics.scan_cycle_latency.compact()} / "
            f"{metrics.book_fetch_latency.compact()} / {metrics.quote_loop_latency.compact()} | "
            f"fill chk/proc {metrics.fill_check_latency.compact()} / "
            f"{metrics.fill_process_latency.compact()} | "
            f"place e/h {metrics.entry_place_latency.avg_only()} / {metrics.hedge_place_latency.avg_only()} | "
            f"cancel {metrics.cancel_latency.avg_only()} | "
            f"fill age e/h {metrics.entry_fill_age_latency.avg_only()} / "
            f"{metrics.hedge_fill_age_latency.avg_only()} | "
            f"fill->submit {metrics.fill_to_hedge_latency.avg_only()} | "
            f"submit->fill {metrics.hedge_submit_to_fill_latency.avg_only()} | "
            f"unhedged {metrics.unhedged_window_latency.avg_only()} | "
            f"slip {metrics.hedge_slippage_cents.avg_only('c')} | "
            f"queue {metrics.hedge_queue_ahead_size.avg_only()} / "
            f"{metrics.hedge_queue_levels_ahead.avg_only()} | "
            f"gap {metrics.hedge_queue_gap_cents.avg_only('c')} | "
            f"book age {metrics.book_age_latency.compact()} | "
            f"rate wait {metrics.rate_limit_wait_latency.avg_only()}",
            style="dim",
        )
