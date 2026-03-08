"""Check whether markets with open positions have resolved, polling the Gamma API every N scans."""

from __future__ import annotations
import random
import time
from dataclasses import dataclass
from typing import Optional

import requests

from config.settings import Settings
from data.models import Market, MarketStatus, Side
from observability import logger as log


@dataclass
class ResolutionEvent:
    market_id: str
    resolved_side: Optional[Side]  # None = cancelled / not determinable
    question: str = ""


class ResolutionChecker:
    """Check market resolution status through the Gamma API."""

    # Poll every N scans to avoid stressing the API
    POLL_EVERY_N_SCANS = 3

    def __init__(self, settings: Settings, dry_run: bool = True):
        self.settings = settings
        self.dry_run = dry_run
        self._scan_count = 0
        self._should_poll_now = False
        self._resolved_market_ids: set[str] = set()
        self._warned_ambiguous_market_ids: set[str] = set()
        # Mock state: simulated resolution cycle
        self._mock_resolve_at_scan: int = random.randint(4, 8)
        self._mock_resolved: set[str] = set()

    @property
    def should_poll_now(self) -> bool:
        return self._should_poll_now

    def tick(self) -> list[ResolutionEvent]:
        """
        Call on every scan cycle.
        Return a list of resolution events, usually empty.
        """
        self._scan_count += 1
        self._should_poll_now = self._scan_count % self.POLL_EVERY_N_SCANS == 0
        if not self._should_poll_now:
            return []

        if self.dry_run:
            return self._mock_check()
        else:
            return []  # Populated by check_markets() below

    def check_markets(self, markets: list[Market]) -> list[ResolutionEvent]:
        """
        Check resolution for a list of markets.
        Call this when tick() indicates polling is due, or directly.
        """
        if self.dry_run:
            return self._mock_check()

        resolved = []
        for market in markets:
            event = self._fetch_resolution(market)
            if event:
                resolved.append(event)
        return resolved

    def _fetch_resolution(self, market: Market) -> Optional[ResolutionEvent]:
        """Query the Gamma API for the status of a single market."""
        if market.condition_id in self._resolved_market_ids:
            return None

        try:
            resp = requests.get(
                f"{self.settings.api.gamma_base_url}/markets",
                params={"conditionIds": market.condition_id},
                timeout=self.settings.api.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()

            # Gamma returns a list even for a single-market query
            items = data if isinstance(data, list) else data.get("data", [])
            if not items:
                return None

            item = items[0]
            resolved = bool(item.get("resolved", False))
            outcome_prices = item.get("outcomePrices", [])
            resolved_side = self._parse_outcome(outcome_prices, item)

            if not resolved and resolved_side is None:
                return None

            if resolved_side is None:
                if market.condition_id not in self._warned_ambiguous_market_ids:
                    log.warn(
                        f"market closed without a determinable outcome: {market.short_question}"
                    )
                    self._warned_ambiguous_market_ids.add(market.condition_id)
                return None

            self._warned_ambiguous_market_ids.discard(market.condition_id)
            self._resolved_market_ids.add(market.condition_id)
            log.info(f"resolution detected: {market.short_question} -> {resolved_side}")
            return ResolutionEvent(
                market_id=market.condition_id,
                resolved_side=resolved_side,
                question=market.question,
            )
        except Exception as e:
            log.warn(f"resolution check error {market.condition_id[:10]}...: {e}")
            return None

    def _parse_outcome(self, outcome_prices: list, item: dict) -> Optional[Side]:
        """
        Interpret Gamma API outcomePrices.
        Typical format: ["1", "0"] or ["0", "1"] (strings with value 0 or 1).
        The order is [YES, NO].
        """
        try:
            if len(outcome_prices) >= 2:
                yes_val = float(outcome_prices[0])
                no_val = float(outcome_prices[1])
                if yes_val > no_val:
                    return Side.YES
                elif no_val > yes_val:
                    return Side.NO

            # Unable to determine the outcome
            return None
        except Exception:
            return None

    def _mock_check(self) -> list[ResolutionEvent]:
        """
        Simulate market resolution in dry run.
        After mock_resolve_at_scan scans, resolve one random market every 3 scans.
        """
        events = []
        if self._scan_count < self._mock_resolve_at_scan:
            return []

        # In dry run we do not have the market list here, so the main loop
        # will pass open positions through check_markets(). Return nothing here
        # and let check_markets() run with the real data.
        return []

    def mock_check_positions(self, market_ids: list[str]) -> list[ResolutionEvent]:
        """
        Mock version of check_markets() operating on condition_id.
        Resolve one market at a time, with a pause between events.
        """
        if self._scan_count < self._mock_resolve_at_scan:
            return []

        events = []
        for mid in market_ids:
            if mid in self._mock_resolved:
                continue
            # Resolve this market
            resolved_side = random.choice([Side.YES, Side.NO])
            self._mock_resolved.add(mid)
            self._mock_resolve_at_scan = self._scan_count + random.randint(3, 6)
            log.info(f"[MOCK] market {mid[:15]}... resolves -> {resolved_side.value}")
            events.append(ResolutionEvent(
                market_id=mid,
                resolved_side=resolved_side,
                question=mid,
            ))
            break  # Only one event per cycle, not all at once

        return events
