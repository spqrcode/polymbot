"""
Track Polymarket liquidity rewards.
Polymarket pays makers in USDC via pool share each epoch.
Poll the Data API every N scans.
"""

from __future__ import annotations
import random
import time
from typing import Optional

import requests

from config.settings import Settings
from observability import logger as log


class RewardsChecker:
    """
    Check cumulative wallet rewards on the Data API.
    Track deltas between snapshots to calculate incremental earnings.
    """

    # Refresh rewards snapshot every N scans
    POLL_EVERY_N_SCANS = 5

    def __init__(self, settings: Settings, wallet_address: str, dry_run: bool = True,
                 enabled: bool = True):
        self.settings = settings
        self.wallet = wallet_address
        self.dry_run = dry_run
        self.enabled = enabled
        self._scan_count = 0
        self._last_snapshot: float = 0.0    # Cumulative rewards at the last snapshot
        self._total_earned: float = 0.0     # Total rewards earned this session
        # Mock: simulated accumulation
        self._mock_epoch_counter = 0

    @property
    def total_earned(self) -> float:
        return self._total_earned

    def tick(self, active_market_ids: list[str]) -> float:
        """
        Call on every scan cycle.
        Return the rewards delta since the last tick (0.0 if nothing new).
        """
        if not self.enabled:
            return 0.0

        self._scan_count += 1
        if self._scan_count % self.POLL_EVERY_N_SCANS != 0:
            return 0.0

        if self.dry_run:
            return self._mock_snapshot(active_market_ids)

        return self._live_snapshot(active_market_ids)

    def _live_snapshot(self, market_ids: list[str]) -> float:
        """
        Query the Data API for cumulative wallet rewards.
        Return the delta since the previous snapshot.
        """
        try:
            # User reward history endpoint
            resp = requests.get(
                f"{self.settings.api.data_api_base_url}/rewards",
                params={
                    "address": self.wallet,
                    "markets": ",".join(market_ids[:20]),  # max 20 per query
                },
                timeout=self.settings.api.request_timeout_sec,
            )

            if resp.status_code == 404:
                # Wallet has no rewards yet
                return 0.0

            resp.raise_for_status()
            data = resp.json()

            # The response format may vary, so try multiple known shapes
            cumulative = self._parse_rewards_response(data)
            if cumulative is None:
                return 0.0

            delta = max(0.0, cumulative - self._last_snapshot)
            if delta > 0:
                self._last_snapshot = cumulative
                self._total_earned += delta
                log.rwrd(f"snapshot {len(market_ids)} markets pool share +${delta:.4f} | "
                         f"session total ${self._total_earned:.4f}")

            return delta

        except requests.exceptions.ConnectionError:
            log.warn("Data API unreachable for rewards snapshot")
            return 0.0
        except Exception as e:
            log.warn(f"rewards snapshot error: {e}")
            return 0.0

    def _parse_rewards_response(self, data: dict | list) -> Optional[float]:
        """
        Interpret the Data API response.
        Try several known formats.
        """
        try:
            # Format 1: {"totalEarned": "12.34"}
            if isinstance(data, dict):
                for key in ("totalEarned", "total_earned", "earned", "amount"):
                    val = data.get(key)
                    if val is not None:
                        return float(val)

            # Format 2: list of records with "earnings"
            if isinstance(data, list) and data:
                total = sum(float(r.get("earnings", r.get("amount", 0))) for r in data)
                return total if total > 0 else None

            return None
        except Exception:
            return None

    def _mock_snapshot(self, market_ids: list[str]) -> float:
        """
        Simulate rewards in dry run.
        Each snapshot generates $0.01-$0.15 per active market.
        Real rewards depend on spread, volume, and pool share.
        """
        if not market_ids:
            return 0.0

        self._mock_epoch_counter += 1

        # Simulate variable pool share: more markets = more rewards
        base_per_market = random.uniform(0.005, 0.030)
        delta = round(base_per_market * len(market_ids), 4)

        self._total_earned += delta
        log.rwrd(f"snapshot {len(market_ids)} markets pool share +${delta:.4f} | "
                 f"session total ${self._total_earned:.4f}")

        return delta
