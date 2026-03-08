"""
Stress state tracker for the bot's recovery mode logic.

Tracks how many markets are in active recovery (unhedged for too long),
the cumulative unhedged exposure duration, and exposes helpers that the
scan cycle can query to decide whether to pause new entries.

Design principles
-----------------
- Zero external dependencies: only stdlib + project modules.
- Thread-safe: all mutations hold a simple lock so it can be updated from
  the immediate WebSocket handler as well as the main scan cycle.
- Conservative: when in doubt, it is safer to pause entries than to open
  new risk while recoveries are outstanding.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from observability import logger as log


@dataclass
class _RecoveryEntry:
    market_id: str
    started_at: float = field(default_factory=time.time)
    unhedged_cycles: int = 0
    last_seen_at: float = field(default_factory=time.time)


class StressState:
    """
    Tracks active recovery positions and decides whether new entry orders
    should be paused to reduce simultaneous risk.

    Parameters
    ----------
    max_concurrent_recoveries : int
        Maximum number of markets in recovery before new entries are blocked.
        0 means never block on count alone.
    recovery_pauses_entry : bool
        If False, stress state is monitored but never actually blocks entries.
        Useful for paper runs where you want to observe without changing behaviour.
    stress_unhedged_sec_trigger : float
        If the oldest active recovery has been open for longer than this many
        seconds, the bot enters stress mode and blocks new entries regardless
        of the count limit.  0.0 disables this guard.
    """

    def __init__(
        self,
        max_concurrent_recoveries: int = 3,
        recovery_pauses_entry: bool = True,
        stress_unhedged_sec_trigger: float = 120.0,
    ) -> None:
        self._max_concurrent = max_concurrent_recoveries
        self._pauses_entry = recovery_pauses_entry
        self._trigger_sec = stress_unhedged_sec_trigger
        self._lock = threading.Lock()
        self._recoveries: dict[str, _RecoveryEntry] = {}

    # ------------------------------------------------------------------
    # Mutation API (called from _manage_unhedged_positions each cycle)
    # ------------------------------------------------------------------

    def mark_recovery(self, market_id: str, unhedged_cycles: int) -> None:
        """Register or update an active recovery for a market."""
        with self._lock:
            entry = self._recoveries.get(market_id)
            if entry is None:
                self._recoveries[market_id] = _RecoveryEntry(
                    market_id=market_id,
                    unhedged_cycles=unhedged_cycles,
                )
                log.warn(
                    f"[STRESS] recovery started: {market_id[:12]}... "
                    f"active_recoveries={len(self._recoveries)}"
                )
            else:
                entry.unhedged_cycles = unhedged_cycles
                entry.last_seen_at = time.time()

    def clear_recovery(self, market_id: str) -> None:
        """Mark a market as no longer in recovery (hedge filled or position closed)."""
        with self._lock:
            if market_id in self._recoveries:
                duration = time.time() - self._recoveries[market_id].started_at
                del self._recoveries[market_id]
                log.ok(
                    f"[STRESS] recovery resolved: {market_id[:12]}... "
                    f"duration={duration:.1f}s "
                    f"remaining_recoveries={len(self._recoveries)}"
                )

    def clear_stale(self, active_recovery_ids: set[str]) -> None:
        """Remove recoveries that were not reported in the latest cycle."""
        with self._lock:
            stale = [mid for mid in self._recoveries if mid not in active_recovery_ids]
            for mid in stale:
                del self._recoveries[mid]
                log.info(f"[STRESS] recovery expired (no longer unhedged): {mid[:12]}...")

    # ------------------------------------------------------------------
    # Query API (called from _scan_cycle before each entry order)
    # ------------------------------------------------------------------

    @property
    def active_recovery_count(self) -> int:
        with self._lock:
            return len(self._recoveries)

    @property
    def oldest_recovery_age_sec(self) -> Optional[float]:
        """Age in seconds of the longest-running recovery, or None if none."""
        with self._lock:
            if not self._recoveries:
                return None
            now = time.time()
            return max(now - e.started_at for e in self._recoveries.values())

    def should_pause_new_entries(self) -> tuple[bool, str]:
        """
        Return (should_pause, reason).

        `should_pause` is True only when `recovery_pauses_entry` is enabled
        AND one of the blocking conditions fires.
        """
        if not self._pauses_entry:
            return False, ""

        with self._lock:
            count = len(self._recoveries)

        if self._max_concurrent > 0 and count >= self._max_concurrent:
            reason = (
                f"stress: {count} active recoveries >= limit {self._max_concurrent}"
            )
            return True, reason

        oldest = self.oldest_recovery_age_sec
        if self._trigger_sec > 0 and oldest is not None and oldest >= self._trigger_sec:
            reason = (
                f"stress: oldest recovery age {oldest:.0f}s >= trigger {self._trigger_sec:.0f}s"
            )
            return True, reason

        return False, ""

    def summary(self) -> dict:
        """Return a dict suitable for logging or metrics export."""
        with self._lock:
            now = time.time()
            ages = [now - e.started_at for e in self._recoveries.values()]
        return {
            "active_recoveries": len(ages),
            "oldest_sec": round(max(ages), 1) if ages else 0.0,
            "avg_sec": round(sum(ages) / len(ages), 1) if ages else 0.0,
        }
