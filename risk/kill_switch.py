"""Kill switch: emergency bot shutdown."""

from __future__ import annotations
import signal
import sys

from execution.order_manager import OrderManager
from observability import logger as log


class KillSwitch:
    """Handle emergency shutdown by cancelling all orders and stopping the bot."""

    def __init__(self, order_manager: OrderManager):
        self.order_manager = order_manager
        self._triggered = False
        self._setup_signal_handlers()

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    def _setup_signal_handlers(self):
        """Capture SIGINT and SIGTERM for a clean shutdown."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        sig_name = signal.Signals(signum).name
        log.kill(f"received {sig_name} - triggering kill switch")
        self.trigger(f"signal {sig_name}")

    def trigger(self, reason: str = ""):
        """Trigger the kill switch."""
        if self._triggered:
            log.warn("kill switch already active, forcing exit")
            sys.exit(1)

        self._triggered = True
        log.kill(f"KILL SWITCH TRIGGERED: {reason}")
        log.kill("cancelling all orders...")

        try:
            self.order_manager.cancel_all()
            log.kill("all orders cancelled")
        except Exception as e:
            log.err(f"error during emergency cancellation: {e}")

        log.kill("bot stopped")
