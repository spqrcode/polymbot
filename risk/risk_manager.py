"""Risk manager: global limits, per-market limits, and drawdown."""

from __future__ import annotations
from dataclasses import dataclass

from config.settings import TradingConfig
from strategy.inventory import InventoryManager
from observability import logger as log


@dataclass
class RiskCheck:
    passed: bool
    reason: str = ""


class RiskManager:
    """Check risk limits before each operation."""

    def __init__(self, config: TradingConfig, inventory: InventoryManager):
        self.config = config
        self.inventory = inventory
        self._realized_pnl = 0.0
        self._unrealized_pnl = 0.0

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def unrealized_pnl(self) -> float:
        return self._unrealized_pnl

    @property
    def total_pnl(self) -> float:
        return self._realized_pnl + self._unrealized_pnl

    def update_pnl(self, pnl_delta: float):
        self._realized_pnl += pnl_delta

    def update_unrealized_pnl(self, pnl_amount: float):
        self._unrealized_pnl = pnl_amount

    def _drawdown_limit_reached(self) -> bool:
        return self.total_pnl <= self.config.drawdown_limit

    def check_can_place_order(
        self,
        market_id: str,
        cost: float,
        reserved_market_cost: float = 0.0,
        reserved_total_cost: float = 0.0,
    ) -> RiskCheck:
        """Check whether a new order can be placed."""
        # Check drawdown
        if self._drawdown_limit_reached():
            return RiskCheck(
                False,
                f"drawdown limit reached: total PnL={self.total_pnl:.2f} <= {self.config.drawdown_limit}",
            )

        # Check per-market exposure
        market_exposure = self.inventory.get_market_exposure(market_id)
        projected_market_cost = market_exposure + reserved_market_cost + cost
        if projected_market_cost > self.config.max_per_market:
            return RiskCheck(
                False,
                f"per-market exposure limit reached: "
                f"{market_exposure:.2f} + {reserved_market_cost:.2f} + {cost:.2f} > {self.config.max_per_market:.2f}"
            )

        # Check global exposure
        projected_total_cost = self.inventory.total_exposure + reserved_total_cost + cost
        if projected_total_cost > self.config.max_capital:
            return RiskCheck(
                False,
                f"max capital reached: "
                f"{self.inventory.total_exposure:.2f} + {reserved_total_cost:.2f} + {cost:.2f} > {self.config.max_capital:.2f}"
            )

        return RiskCheck(True)

    def check_global_limits(self, open_orders: int) -> RiskCheck:
        """Check global limits."""
        if open_orders >= self.config.max_open_orders:
            return RiskCheck(False,
                             f"max open orders reached: {open_orders} >= {self.config.max_open_orders}")

        if self._drawdown_limit_reached():
            return RiskCheck(False, f"critical drawdown: total PnL={self.total_pnl:.2f}")

        return RiskCheck(True)

    def should_kill(self) -> bool:
        """Check whether the kill switch should trigger."""
        if self._drawdown_limit_reached():
            log.kill(
                f"KILL SWITCH: PnL total={self.total_pnl:.2f} <= drawdown limit {self.config.drawdown_limit}"
            )
            return True
        return False
