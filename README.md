# Polymarket Bot

Market making bot for Polymarket. The core strategy is spread-lock quoting:
place bids on both YES and NO, then capture the edge when both legs fill
(YES + NO < $1.00).

---

## Compatibility

This repository was developed with a macOS-oriented workflow.

- `macOS`: full support for the included helper scripts (`start_polymarketbot`, `stop_polymarketbot`, `status_polymarketbot`), `launchd` template, and `caffeinate` integration.
- `Linux` / `Windows`: the Python code and tests are reusable, but process-management scripts and the `launchd` template require adaptation.

If you are not using macOS, the simplest entry point is:

```bash
python3 -u polymarketbot.py
```


## Legal and operational note

Polymarket access may be limited or unavailable in some jurisdictions. Anyone using this repository should independently verify platform availability, local regulatory compliance, and exchange requirements before running the bot.

---

## Project structure

```text
polymarket/
├── polymarketbot.py          # Main bot loop
├── polymarketbot             # Executable launcher
├── start_polymarketbot       # Interactive launcher
├── stop_polymarketbot        # Stops the bot in the background
├── status_polymarketbot      # Shows process status
│
├── config/
│   ├── settings.py           # Centralized configuration loader (.env)
│   └── markets_filter.py     # Market selection filters
│
├── data/
│   ├── models.py             # Dataclasses: Market, Order, Position, etc.
│   ├── clob_client.py        # Polymarket CLOB REST client
│   ├── market_scanner.py     # Fetching, parsing, and market selection
│   ├── metrics_tracker.py    # Per-market KPIs and hedge-path tracking
│   ├── websocket_manager.py  # Market + user WebSocket bridge
│   ├── resolution_checker.py # Market resolution handling
│   └── rewards_checker.py    # LP rewards tracking
│
├── strategy/
│   ├── quoter.py             # YES/NO quoting logic
│   ├── hedger.py             # Post-fill hedge logic
│   └── inventory.py          # Position and PnL tracking
│
├── execution/
│   ├── order_manager.py      # Placement, cancellation, fill detection
│   └── rate_limiter.py       # API throttling
│
├── risk/
│   ├── risk_manager.py       # Capital limits, drawdown, kill switch
│   ├── kill_switch.py        # Emergency kill switch
│   ├── preflight.py          # Pre-live checks
│   └── process_lock.py       # Prevents multiple concurrent instances
│
├── observability/
│   ├── logger.py             # Colored logger with custom levels
│   ├── metrics.py            # Runtime counters
│   ├── dashboard.py          # Live terminal dashboard
│   ├── audit.py              # JSON audit trail
│   └── reporting.py          # Session report + run history
│
├── scripts/
│   ├── run_bot.sh            # Direct launcher script
│   ├── run_background.sh     # Background process manager
│   └── run_launchd.sh        # Backward-compatible wrapper for macOS setups
│
├── launchd/
│   └── com.example.polymarketbot.plist.template  # macOS launchd template
│
├── docs/
│   ├── COMMANDS.md           # Command reference and quick operations guide
│   └── ROADMAP.md            # Planned improvements and analysis
│
├── tests/
│   └── test_regressions.py   # Regression test suite
│
├── runtime/                  # Runtime-generated files
│   ├── polymarketbot.log     # Main bot log
│   ├── polymarketbot.pid     # Bot process ID
│   ├── manager.pid           # Background manager process ID
│   └── session.env           # Last interactive session configuration
│
├── reports/                  # Runtime-generated reports
│   ├── run_history.jsonl     # Session history with PnL
│   └── market_metrics.json   # Per-market KPIs and hedge telemetry
│
├── .env                      # Local configuration
├── .env.example              # Configuration template
├── .gitignore                # Excludes local secrets and runtime artifacts
├── CONTRIBUTING.md           # Contributor guidelines
├── LICENSE                   # Open source license (MIT)
├── requirements.txt          # Python dependencies
└── README.md                 # This file
```

---

## Quick start

```bash
cd polymarket
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Local configuration
cp .env.example .env

# Recommended interactive launcher
./start_polymarketbot

# Check whether the bot is running
./status_polymarketbot

# Stop the bot
./stop_polymarketbot

# Tail the main log
tail -f runtime/polymarketbot.log
```

See `docs/COMMANDS.md` for the full command guide and `launchd/com.example.polymarketbot.plist.template`
if you want to install the bot as a macOS agent.

## Repository hygiene

The following paths are excluded from version control and will never be committed:

- `.env` — your private keys and configuration
- `runtime/` — PID files, session state, lock files
- `reports/` — telemetry and session reports generated at runtime

---

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Main parameters:

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | — | Polygon wallet private key (required for live trading) |
| `DRY_RUN` | `true` | When true, no real orders are sent |
| `PAPER_TRADING` | `true` | Simulates fills using live books |
| `MAX_CAPITAL` | `50` | Maximum capital in USDC |
| `MAX_PER_MARKET` | `3` | Maximum exposure per market |
| `ORDER_SIZE` | `0.50` | Size of each order in USDC |
| `MAX_MARKETS` | `12` | Maximum number of active markets |
| `MAX_SUM_CENTS` | `103` | YES + NO cap, allowing defensive hedges with bounded downside |
| `MIN_SPREAD_CENTS` | `4` | Minimum spread required to trade |
| `DRAWDOWN_LIMIT` | `-5` | Kill switch threshold in USD |
| `MAX_CONCURRENT_RECOVERIES` | `3` | Max active recoveries before new entries are paused |
| `RECOVERY_PAUSES_ENTRY` | `true` | Whether recovery pressure blocks new entry orders |
| `STRESS_UNHEDGED_SEC_TRIGGER` | `120` | Oldest open recovery (in seconds) that triggers stress mode |

---

## How it works

1. **Scan**: fetch active Polymarket markets and filter by spread and competition.
2. **Quote**: compute passive YES and NO bids with mid-price offset and inventory skew.
3. **Fill detection**: monitor fills through the user WebSocket channel plus REST polling fallback.
4. **Hedge**: when YES fills, immediately place the NO hedge leg to close the pair.
5. **PnL lock**: every completed YES+NO pair with total cost below 100c locks in fixed profit.
6. **Recovery mode**: if a position stays unhedged for too long, the bot escalates the hedge price and pauses new entries to prioritize closing the open risk.
7. **Resolution**: handle market resolution and any remaining PnL.

---

## Recovery mode and stress guardrails

If a YES fill is received but the NO hedge does not fill within `UNHEDGED_ALERT_CYCLES` scan cycles,
the bot enters **recovery mode** for that market:

- existing entry orders on the hedge side are cancelled and replaced with a more aggressive hedge price
- new entry orders across **all** markets are paused when the number of concurrent recoveries
  reaches `MAX_CONCURRENT_RECOVERIES` or the oldest open recovery exceeds `STRESS_UNHEDGED_SEC_TRIGGER` seconds
- each cycle, a `[STRESS]` summary line is logged with the count and duration of active recoveries

Once the hedge fills and the pair closes, the market is removed from recovery and the entry pause lifts automatically.

To observe recovery behaviour without blocking entries (useful during paper runs), set:

```env
RECOVERY_PAUSES_ENTRY=false
```

---

## Paper trading vs live

`PAPER_TRADING=true` uses live books for scanning, quoting, and repricing, so market selection,
spread behavior, and control flow remain useful. It should not be treated as a perfect live simulation.

Main limitations:

- the simulator may complete YES/NO pairs directly on entry orders without using the real hedge path
- it does not model queue priority, adverse selection, or real hedge latency
- fill rate, pairs per hour, and PnL are therefore more optimistic than live trading

Practical rule of thumb:

- use paper trading to validate the scanner, quoting, repricing, loop stability, and guardrails
- for a conservative live estimate, treat completed paper pairs and paper PnL as an upper bound
- before scaling, always verify hedge behavior live with minimal size

---

## Telemetry

The bot generates session-level telemetry automatically at runtime.
All files are written to the `reports/` directory, which is excluded from version control.
Each user builds their own telemetry independently as they run the bot.

Generated files:

- `reports/run_history.jsonl` — one record per session with PnL, fill count, duration
- `reports/market_metrics.json` — per-market KPIs: fill rate, hedge latency, unhedged window, slippage
- `reports/audit_<run_id>.jsonl` — immutable audit trail of every fill and order event

These files are never shared and contain only data from your own runs.

---

## Dependencies

```bash
pip install -r requirements.txt
```

- `py-clob-client` - official Polymarket CLOB SDK
- `python-dotenv` - environment variable management
- `requests` - HTTP client
- `rich` - terminal dashboard
- `websocket-client` - WebSocket support
- `web3` - Polygon transaction signing

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Contributing and license

- Contributor guide: `CONTRIBUTING.md`
- License: `LICENSE` (MIT)
