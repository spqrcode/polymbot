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

If you want to publish this project as a standalone repository, use this `polymarket/` folder as the repository root.

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

This folder includes a local `.gitignore` tailored for publishing the bot as a standalone repository.
The following paths are excluded from version control:

- `.env`
- `runtime/`
- `reports/`
- local lock files and temporary development artifacts

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
| `MAX_PER_MARKET` | `5` | Maximum exposure per market |
| `ORDER_SIZE` | `1.0` | Size of each order in USDC |
| `MAX_MARKETS` | `5` | Maximum number of active markets |
| `MAX_SUM_CENTS` | `103` | YES + NO cap, allowing defensive hedges with bounded downside |
| `MIN_SPREAD_CENTS` | `4` | Minimum spread required to trade |
| `DRAWDOWN_LIMIT` | `-5` | Kill switch threshold in USD |

---

## How it works

1. **Scan**: fetch active Polymarket markets and filter by spread and competition.
2. **Quote**: compute passive YES and NO bids with mid-price offset and inventory skew.
3. **Fill detection**: monitor fills through the user WebSocket channel plus REST polling fallback.
4. **Hedge**: when YES fills, immediately place the NO hedge leg to close the pair.
5. **PnL lock**: every completed YES+NO pair with total cost below 100c locks in fixed profit.
6. **Resolution**: handle market resolution and any remaining PnL.

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

---

## Planned improvements

See `docs/ROADMAP.md` for the full analysis of current bottlenecks and planned improvements.

**TL;DR most impactful improvement**: keep `MAX_SUM_CENTS=103` in `.env`.
This allows more defensive hedging when the market moves after the first fill,
greatly reducing the risk of carrying an unhedged position into resolution.
