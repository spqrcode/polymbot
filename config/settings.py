"""
Central bot configuration.
Loads variables from .env and defines conservative defaults for initial testing.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class WalletConfig:
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    api_key: str = field(default_factory=lambda: os.getenv("CLOB_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("CLOB_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("CLOB_API_PASSPHRASE", ""))
    chain_id: int = 137  # Polygon mainnet


@dataclass
class TradingConfig:
    # Capital
    max_capital: float = field(default_factory=lambda: _env_float("MAX_CAPITAL", 50.0))
    max_per_market: float = field(default_factory=lambda: _env_float("MAX_PER_MARKET", 5.0))
    order_size: float = field(default_factory=lambda: _env_float("ORDER_SIZE", 1.0))

    # Markets
    max_markets: int = field(default_factory=lambda: _env_int("MAX_MARKETS", 5))
    book_enrich_limit: int = field(default_factory=lambda: _env_int("BOOK_ENRICH_LIMIT", 250))
    min_spread_cents: float = field(default_factory=lambda: _env_float("MIN_SPREAD_CENTS", 4.0))
    max_sum_cents: float = field(default_factory=lambda: _env_float("MAX_SUM_CENTS", 103.0))  # Defensive hedge cap: limits worst-case loss to a few cents
    price_range_min: float = field(default_factory=lambda: _env_float("PRICE_RANGE_MIN", 0.20))
    price_range_max: float = field(default_factory=lambda: _env_float("PRICE_RANGE_MAX", 0.80))
    competition: str = field(default_factory=lambda: os.getenv("COMPETITION", "low"))

    # Timing
    scan_interval_sec: float = field(default_factory=lambda: _env_float("SCAN_INTERVAL_SEC", 10.0))
    hold_interval_sec: float = field(default_factory=lambda: _env_float("HOLD_INTERVAL_SEC", 60.0))
    reprice_interval_sec: float = field(default_factory=lambda: _env_float("REPRICE_INTERVAL_SEC", 15.0))
    reprice_threshold_cents: float = field(default_factory=lambda: _env_float("REPRICE_THRESHOLD_CENTS", 1.0))
    max_book_age_sec: float = field(default_factory=lambda: _env_float("MAX_BOOK_AGE_SEC", 60.0))
    unhedged_alert_cycles: int = field(default_factory=lambda: _env_int("UNHEDGED_ALERT_CYCLES", 3))
    target_entry_orders: int = field(default_factory=lambda: _env_int("TARGET_ENTRY_ORDERS", 0))
    target_entry_fill_events: int = field(default_factory=lambda: _env_int("TARGET_ENTRY_FILL_EVENTS", 0))
    profit_target: float = field(default_factory=lambda: _env_float("PROFIT_TARGET", 0.0))

    # Recovery / stress mode
    max_concurrent_recoveries: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_RECOVERIES", 3))
    recovery_pauses_entry: bool = field(default_factory=lambda: _env_bool("RECOVERY_PAUSES_ENTRY", True))
    stress_unhedged_sec_trigger: float = field(default_factory=lambda: _env_float("STRESS_UNHEDGED_SEC_TRIGGER", 120.0))

    # Limits
    max_open_orders: int = field(default_factory=lambda: _env_int("MAX_OPEN_ORDERS", 10))
    drawdown_limit: float = field(default_factory=lambda: _env_float("DRAWDOWN_LIMIT", -5.0))  # Kill switch at -$5

    # Quote offset
    quote_offset_cents: float = field(default_factory=lambda: _env_float("QUOTE_OFFSET_CENTS", 2.0))  # Offset from mid to place orders

    # Live guardrails
    min_usdc_buffer: float = field(default_factory=lambda: _env_float("MIN_USDC_BUFFER", 2.0))
    allow_existing_open_orders: bool = field(default_factory=lambda: _env_bool("ALLOW_EXISTING_OPEN_ORDERS", False))
    allow_existing_positions: bool = field(default_factory=lambda: _env_bool("ALLOW_EXISTING_POSITIONS", False))
    allow_fee_enabled_markets: bool = field(default_factory=lambda: _env_bool("ALLOW_FEE_ENABLED_MARKETS", False))
    max_allowed_fee_rate_bps: int = field(default_factory=lambda: _env_int("MAX_ALLOWED_FEE_RATE_BPS", 10_000))


@dataclass
class APIConfig:
    clob_base_url: str = "https://clob.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    data_api_base_url: str = field(default_factory=lambda: os.getenv("DATA_API_BASE_URL", "https://data-api.polymarket.com"))
    market_ws_url: str = field(default_factory=lambda: os.getenv("MARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"))
    user_ws_url: str = field(default_factory=lambda: os.getenv("USER_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/user"))
    use_websocket: bool = field(default_factory=lambda: _env_bool("USE_WEBSOCKET", True))
    websocket_ping_interval_sec: float = field(default_factory=lambda: _env_float("WEBSOCKET_PING_INTERVAL_SEC", 15.0))
    websocket_ping_timeout_sec: float = field(default_factory=lambda: _env_float("WEBSOCKET_PING_TIMEOUT_SEC", 5.0))
    rate_limit_per_sec: float = 5.0  # Max requests per second
    request_timeout_sec: float = 10.0
    max_retries: int = 3
    retry_delay_sec: float = 1.0
    book_fetch_workers: int = field(default_factory=lambda: _env_int("BOOK_FETCH_WORKERS", 4))
    max_consecutive_api_errors: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_API_ERRORS", 5))


@dataclass
class Settings:
    wallet: WalletConfig = field(default_factory=WalletConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    api: APIConfig = field(default_factory=APIConfig)
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")
    paper_trading: bool = field(default_factory=lambda: _env_bool("PAPER_TRADING", False))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    session_duration_hours: float = field(default_factory=lambda: _env_float("SESSION_DURATION_HOURS", 0.0))
    report_dir: str = field(default_factory=lambda: os.getenv("REPORT_DIR", "reports"))

    def validate(self) -> list[str]:
        """Return a list of configuration errors."""
        errors = []
        if not self.wallet.private_key:
            errors.append("PRIVATE_KEY missing in .env")
        if self.trading.max_capital <= 0:
            errors.append("max_capital must be > 0")
        if self.trading.max_per_market > self.trading.max_capital:
            errors.append("max_per_market cannot exceed max_capital")
        if self.trading.book_enrich_limit <= 0:
            errors.append("book_enrich_limit must be > 0")
        if self.trading.max_markets <= 0:
            errors.append("max_markets must be > 0")
        if self.trading.order_size <= 0:
            errors.append("order_size must be > 0")
        if self.trading.min_spread_cents < 1:
            errors.append("min_spread_cents too low (< 1)")
        if self.trading.max_sum_cents > 105:
            errors.append("max_sum_cents too high (> 105), excessive risk")
        if self.trading.price_range_min <= 0:
            errors.append("price_range_min must be > 0")
        if self.trading.price_range_max <= 0:
            errors.append("price_range_max must be > 0")
        if self.trading.price_range_min >= self.trading.price_range_max:
            errors.append("price_range_min must be < price_range_max")
        if self.trading.drawdown_limit >= 0:
            errors.append("drawdown_limit must be negative")
        if self.trading.min_usdc_buffer < 0:
            errors.append("min_usdc_buffer must be >= 0")
        if self.trading.scan_interval_sec <= 0:
            errors.append("scan_interval_sec must be > 0")
        if self.trading.hold_interval_sec <= 0:
            errors.append("hold_interval_sec must be > 0")
        if self.trading.reprice_interval_sec <= 0:
            errors.append("reprice_interval_sec must be > 0")
        if self.trading.reprice_threshold_cents <= 0:
            errors.append("reprice_threshold_cents must be > 0")
        if self.trading.max_book_age_sec <= 0:
            errors.append("max_book_age_sec must be > 0")
        if self.trading.unhedged_alert_cycles <= 0:
            errors.append("unhedged_alert_cycles must be > 0")
        if self.trading.target_entry_orders < 0:
            errors.append("target_entry_orders must be >= 0")
        if self.trading.target_entry_fill_events < 0:
            errors.append("target_entry_fill_events must be >= 0")
        if self.trading.profit_target < 0:
            errors.append("profit_target must be >= 0")
        if self.trading.max_allowed_fee_rate_bps < 0:
            errors.append("max_allowed_fee_rate_bps must be >= 0")
        if self.api.max_consecutive_api_errors <= 0:
            errors.append("max_consecutive_api_errors must be > 0")
        if self.api.websocket_ping_interval_sec <= 0:
            errors.append("websocket_ping_interval_sec must be > 0")
        if self.api.websocket_ping_timeout_sec <= 0:
            errors.append("websocket_ping_timeout_sec must be > 0")
        if self.api.book_fetch_workers <= 0:
            errors.append("book_fetch_workers must be > 0")
        if self.paper_trading and not self.dry_run:
            errors.append("PAPER_TRADING requires DRY_RUN=true")
        if self.session_duration_hours < 0:
            errors.append("session_duration_hours must be >= 0")
        return errors


# Global instance
settings = Settings()
