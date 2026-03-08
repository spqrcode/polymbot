"""Market scanner: fetch, normalize, and filter available markets."""

from __future__ import annotations
import json
import ast
from datetime import datetime, timezone
from typing import Optional
import requests

from config.settings import Settings
from config.markets_filter import apply_all_filters
from data.clob_client import PolymarketClient
from data.models import Market, MarketOrderBooks, MarketStatus
from observability import logger as log


class MarketScanner:
    """Fetch and filter Polymarket markets."""

    def __init__(self, client: PolymarketClient, settings: Settings):
        self.client = client
        self.settings = settings
        self._markets_cache: dict[str, Market] = {}

    def fetch_all_markets(self) -> list[Market]:
        """Fetch all markets and normalize them into Market dataclasses."""
        raw_markets = self._fetch_from_gamma()
        markets = []
        for raw in raw_markets:
            market = self._parse_market(raw)
            if market:
                markets.append(market)
                self._markets_cache[market.condition_id] = market

        log.info(f"parsed {len(markets)} active markets out of {len(raw_markets)} total")
        return markets

    def register_markets(self, markets: list[Market]):
        """Explicitly register markets already known in the local cache."""
        for market in markets:
            self._markets_cache[market.condition_id] = market

    def _fetch_from_gamma(self) -> list[dict]:
        """Fetch markets from the Gamma API, which contains richer data."""
        try:
            all_markets = []
            offset = 0
            limit = 100

            while True:
                resp = requests.get(
                    f"{self.settings.api.gamma_base_url}/markets",
                    params={
                        "limit": limit,
                        "offset": offset,
                        "active": True,
                        "closed": False,
                    },
                    timeout=self.settings.api.request_timeout_sec,
                )
                resp.raise_for_status()
                batch = resp.json()

                if not batch:
                    break

                all_markets.extend(batch)
                if len(batch) < limit:
                    break
                offset += limit

            return all_markets
        except Exception as e:
            log.err(f"Gamma API fetch error: {e}")
            return []

    def _parse_market(self, raw: dict) -> Optional[Market]:
        """Convert a raw market into a Market dataclass."""
        try:
            condition_id = raw.get("conditionId", raw.get("condition_id", ""))
            if not condition_id:
                return None

            # Extract token IDs from CLOB token IDs
            clob_token_ids = self._parse_clob_token_ids(raw.get("clobTokenIds", []))
            token_yes = clob_token_ids[0] if len(clob_token_ids) > 0 else ""
            token_no = clob_token_ids[1] if len(clob_token_ids) > 1 else ""

            # Estimate competition from volume and liquidity
            volume = float(raw.get("volume", 0) or 0)
            liquidity = float(raw.get("liquidity", 0) or 0)
            competition = self._estimate_competition(volume, liquidity)

            active = raw.get("active", True) and not raw.get("closed", False)

            return Market(
                condition_id=condition_id,
                question=raw.get("question", ""),
                token_id_yes=token_yes,
                token_id_no=token_no,
                status=MarketStatus.ACTIVE if active else MarketStatus.CLOSED,
                active=active,
                competition=competition,
                volume=volume,
                liquidity=liquidity,
                end_date=raw.get("endDate", raw.get("end_date_iso")),
            )
        except Exception as e:
            log.warn(f"market parsing error: {e}")
            return None

    def _estimate_competition(self, volume: float, liquidity: float) -> str:
        """Estimate competition level from volume and liquidity."""
        if volume > 100_000 or liquidity > 50_000:
            return "high"
        elif volume > 10_000 or liquidity > 5_000:
            return "medium"
        return "low"

    def filter_markets(self, markets: list[Market]) -> list[Market]:
        """Apply configured filters to the markets."""
        passed = []
        for market in markets:
            result = apply_all_filters(market, self.settings.trading)
            if result.passed:
                passed.append(market)

        log.info(f"filtered {len(passed)} markets out of {len(markets)} (filters applied)")
        return passed

    def enrich_with_book(self, market: Market) -> Market:
        """Enrich a market with order book data."""
        if not market.token_id_yes or not market.token_id_no:
            return market

        books = self.client.get_market_books(market.token_id_yes, market.token_id_no)
        self._apply_books_snapshot(market, books)
        return market

    def scan_and_select(self) -> list[Market]:
        """Full pipeline: fetch -> enrich -> filter -> select top N."""
        markets = self.fetch_all_markets()
        candidates = self._select_enrichment_candidates(markets)

        # Enrich with book data to obtain spread and mid
        enriched = []
        for m in candidates:
            m = self.enrich_with_book(m)
            enriched.append(m)

        # Filter
        filtered = self.filter_markets(enriched)

        # Composite score: spread weighted by normalized volume
        # Markets with good spread and high volume beat markets with only wide spread
        filtered.sort(
            key=lambda m: (m.spread_cents or 0) * min((m.volume or 0) / 10_000, 1.0),
            reverse=True,
        )
        selected = self._select_fee_safe_markets(filtered)

        log.ok(f"selected {len(selected)} markets for trading")
        for m in selected:
            fee_label = ""
            if m.max_fee_rate_bps is not None:
                fee_label = f" | fee={m.max_fee_rate_bps}bps"
            log.info(
                f"  -> {m.short_question} | "
                f"YES mid={m.yes_mid_price:.2f} spr={m.yes_spread_cents:.1f}c | "
                f"NO mid={m.no_mid_price:.2f} spr={m.no_spread_cents:.1f}c"
                f"{fee_label}"
            )

        return selected

    def get_cached_market(self, condition_id: str) -> Optional[Market]:
        return self._markets_cache.get(condition_id)

    def get_cached_market_by_token_id(self, token_id: str) -> Optional[Market]:
        token_id = str(token_id or "")
        if not token_id:
            return None

        for market in self._markets_cache.values():
            if market.token_id_yes == token_id or market.token_id_no == token_id:
                return market
        return None

    def _parse_clob_token_ids(self, raw_value) -> list[str]:
        if isinstance(raw_value, list):
            return [str(token) for token in raw_value if str(token).strip()]

        if isinstance(raw_value, str):
            normalized = raw_value.strip()
            if not normalized:
                return []

            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(normalized)
                    if isinstance(parsed, list):
                        return [str(token) for token in parsed if str(token).strip()]
                except (ValueError, SyntaxError):
                    continue

            return [part.strip().strip('"').strip("'") for part in normalized.split(",") if part.strip()]

        return []

    def _select_enrichment_candidates(self, markets: list[Market]) -> list[Market]:
        candidates = [
            market for market in markets
            if market.active and market.token_id_yes and market.token_id_no
        ]

        levels = {"low": 0, "medium": 1, "high": 2}
        max_competition = levels.get(self.settings.trading.competition, 0)
        scoped = [
            market for market in candidates
            if levels.get(market.competition, 1) <= max_competition
        ]
        if not scoped:
            scoped = candidates

        # Sort by composite score: liquidity/volume weighted by time-to-expiry urgency
        scoped.sort(
            key=lambda market: (market.liquidity + market.volume) * self._urgency_multiplier(market),
            reverse=True,
        )
        limit = self.settings.trading.book_enrich_limit
        selected = scoped[:limit]
        log.info(f"candidate enrichment set: {len(selected)} markets out of {len(markets)} total")
        return selected

    def _urgency_multiplier(self, market: Market) -> float:
        """Multiplier based on time to expiry.
        Markets nearing expiry usually have more activity and faster fills."""
        if not market.end_date:
            return 1.0
        try:
            end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_left = (end - now).total_seconds() / 86400
            if days_left < 0:
                return 0.1
            if days_left < 3:
                return 2.0
            if days_left < 7:
                return 1.5
            if days_left < 30:
                return 1.0
            return 0.5
        except (ValueError, TypeError):
            return 1.0

    def _apply_books_snapshot(self, market: Market, books: MarketOrderBooks) -> None:
        market.mid_price = books.yes_mid_price
        market.spread_cents = books.min_spread_cents
        market.yes_mid_price = books.yes_mid_price
        market.no_mid_price = books.no_mid_price
        market.yes_spread_cents = books.yes_spread_cents
        market.no_spread_cents = books.no_spread_cents

    def _select_fee_safe_markets(self, markets: list[Market]) -> list[Market]:
        selected: list[Market] = []
        skipped_fee = 0
        skipped_lookup = 0
        assumed_fee_free = 0

        for market in markets:
            if len(selected) >= self.settings.trading.max_markets:
                break

            self._enrich_fee_metadata(market)
            if market.max_fee_rate_bps is None:
                if not self._market_fee_allowed(market):
                    skipped_lookup += 1
                    log.warn(f"skip {market.short_question} | fee rate could not be determined")
                    continue

                assumed_fee_free += 1
                log.warn(
                    f"fee rate could not be determined for {market.short_question} | "
                    "continuing in paper/dry-run"
                )
                selected.append(market)
                continue

            if not self._market_fee_allowed(market):
                skipped_fee += 1
                log.warn(
                    f"skip {market.short_question} | fee-enabled {market.max_fee_rate_bps}bps"
                )
                continue

            selected.append(market)

        if skipped_lookup > 0:
            log.warn(f"incomplete fee lookup: skipped {skipped_lookup} markets")
        if assumed_fee_free > 0:
            log.warn(f"incomplete fee lookup: allowed {assumed_fee_free} markets in paper/dry-run")
        if skipped_fee > 0:
            log.info(f"fee-enabled markets skipped: {skipped_fee}")

        return selected

    def _enrich_fee_metadata(self, market: Market) -> None:
        if market.fee_rate_bps_yes is None and market.token_id_yes:
            market.fee_rate_bps_yes = self.client.get_fee_rate(market.token_id_yes)
        if market.fee_rate_bps_no is None and market.token_id_no:
            market.fee_rate_bps_no = self.client.get_fee_rate(market.token_id_no)
        market.fee_enabled = (market.max_fee_rate_bps or 0) > 0

    def _market_fee_allowed(self, market: Market) -> bool:
        max_fee_rate_bps = market.max_fee_rate_bps
        if max_fee_rate_bps is None:
            # Unknown fee: assume 0 in paper/dry-run (no real fees)
            # In LIVE mode, block for safety
            if self.settings.dry_run or self.settings.paper_trading:
                return True
            return False
        if not self.settings.trading.allow_fee_enabled_markets and max_fee_rate_bps > 0:
            return False
        return max_fee_rate_bps <= self.settings.trading.max_allowed_fee_rate_bps
