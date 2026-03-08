# Improvement Roadmap - Polymarket Bot

Complete analysis of the main bottlenecks limiting **completed fill pairs per day**,
with solutions ordered by priority and expected impact.

---

## Structural problem

The current loop is:

```text
YES bid + NO bid -> wait for fill -> YES filled -> place passive NO hedge -> wait for fill -> pair completed
```

Each step adds latency and failure points.
Completed pairs remain low because there are **5+ independent obstacles** that multiply together.

---

## PRIORITY 0 - Immediate fixes identified during paper trading

### 0.1 Copy values from `.env.example` into the real `.env`

**Status**: **COMPLETED** - the local `.env` is aligned with the recommended values
(`MAX_SUM_CENTS=103`, `MAX_MARKETS=12`, `ORDER_SIZE=0.50`, `SCAN_INTERVAL_SEC=8`, live guardrails).

**Evidence**: the startup banner was showing `Max Sum: 100c` and `Markets: 5`
instead of `Max Sum: 103c` and `Markets: 12`.

**Fix**:
Copy the recommended values from the "Recommended `.env` configuration" section at the end of this file into your local `.env`.

---

### 0.2 Book-age guard: do not quote on stale data

**File**: `strategy/quoter.py`

**Status**: **COMPLETED** - `Quoter.compute_quotes()` invalidates stale books and the main loop logs `skip` / `warn`; `.env` now includes `MAX_BOOK_AGE_SEC=60`.

**Problem**:
During paper runs, book age was reaching 107-170 seconds on some markets.
The bot kept repricing and placing orders using stale prices.
In live mode this leads to bad quotes that can remain open for too long.

**Fix**:
Add a check: if `book_age > MAX_BOOK_AGE_SEC` (for example 60s), skip repricing for that market and log a warning.

---

### 0.3 Alert and action for positions left unhedged too long

**File**: `polymarketbot.py` - `_process_new_fills`

**Status**: **COMPLETED** - after `UNHEDGED_ALERT_CYCLES=3`, the loop enters recovery mode: prominent warning with mark-to-market PnL, cancellation of the still-live opposite entry leg, and explicit hedge placement up to `MAX_SUM_CENTS`.

**Problem**:
Example from paper trading: a YES leg filled, the NO hedge never executed, and the market moved significantly.
The bot was simply waiting for the next scan cycle.
In live trading, an unhedged position that keeps drifting can lose much more than a controlled hedge loss.

**Fix**:
If one side has filled and more than N scan cycles pass without a hedge:
1. log a prominent warning with current mark-to-market PnL
2. widen the hedge price up to `MAX_SUM_CENTS` to increase fill probability
3. optionally place a more aggressive emergency hedge if the loss exceeds a configurable threshold

---

### 0.4 KPI tracking to evaluate the bot over time

**File**: new module `data/metrics_tracker.py`

**Status**: **COMPLETED** - added `data/metrics_tracker.py`, runtime JSON at `reports/.../market_metrics.json`, and hedge-path telemetry such as `fill->submit`, `submit->fill`, `unhedged window`, `slippage`, queue depth, and `entry bypass vs hedge path`.

**Problem**:
The log was showing aggregate counters, but key metrics were missing:
- fill rate per market
- hedge latency
- completed pairs vs abandoned pairs
- per-market PnL

**Fix**:
Add a tracker that updates a JSON report on each scan with per-market metrics and hedge-path telemetry.

---

### 0.5 Document that paper trading bypasses the real hedge path

**File**: `README.md`

**Status**: **COMPLETED** - dedicated section added to `README.md` plus an explicit startup warning in paper mode.

**Problem**:
In paper mode, the simulator can complete YES and NO pairs directly on entry orders.
That makes paper-mode completed pairs much more frequent than real live trading.
Paper metrics are therefore not comparable 1:1 with live metrics.

**Fix**:
Document the behavior clearly in the README and startup logs so users understand that paper trading is directionally useful, but optimistic.

---

## PRIORITY 1 - `.env` only, zero code, highest impact

### 1.1 Raise `MAX_SUM_CENTS` from 100 to 103

**Status**: **COMPLETED** - `.env.example` and runtime defaults are aligned to `MAX_SUM_CENTS=103`.

**Files**: `.env`, `.env.example`, `config/settings.py`

**Problem**:
If YES fills at 47c, a strict 100c cap means the NO hedge cannot go above 53c.
If the best ask is 55c, the hedge becomes impossible and the bot can stay unhedged until resolution.

**Economic tradeoff**:
- losing up to 3c on a defensive hedge is acceptable
- carrying an unhedged position can cost far more

**Fix**:

```env
MAX_SUM_CENTS=103
```

This caps the downside on the pair while dramatically reducing inventory risk.

---

### 1.2 More markets, smaller order size

**Status**: **COMPLETED** - `.env.example` updated with `MAX_MARKETS=12`, `ORDER_SIZE=0.50`, `MAX_PER_MARKET=3`, `MAX_OPEN_ORDERS=25`.

**Problem**:
With only 5 markets and $1 orders, the bot has very few active opportunities.
If one slot gets stuck waiting on a hedge, it stops generating new fills.

**Fix**:

```env
MAX_MARKETS=12
MAX_PER_MARKET=3
ORDER_SIZE=0.50
MAX_OPEN_ORDERS=25
```

Same capital budget, more opportunities, smaller single-market risk.

---

### 1.3 More aggressive repricing

**Status**: **COMPLETED** - `.env.example` updated to `REPRICE_INTERVAL_SEC=8`, `REPRICE_THRESHOLD_CENTS=0.5`.

**Problem**:
With a 15s reprice interval and 1c threshold, quotes can sit off-market too long without being refreshed.

**Fix**:

```env
REPRICE_INTERVAL_SEC=8
REPRICE_THRESHOLD_CENTS=0.5
```

---

## PRIORITY 2 - Small algorithmic changes

### 2.1 Join the top-of-book queue instead of quoting below it

**Status**: **COMPLETED**

**File**: `strategy/quoter.py`

**Problem**:
If the bot quotes below the current best bid, it sits behind multiple better-priced orders and fill probability drops.

**Fix**:
Use `max(desired_price, best_bid)` while still ensuring the quote does not cross the spread.

---

### 2.2 Market selection: rank by volume x spread, not spread alone

**Status**: **COMPLETED**

**File**: `data/market_scanner.py`

**Problem**:
A wide spread can simply mean low liquidity.
Ranking only by spread tends to pick hard-to-fill markets.

**Fix**:
Sort by a composite score such as `spread x volume factor` instead of spread only.

---

### 2.3 Prefer markets expiring within 7 days

**Status**: **COMPLETED**

**File**: `data/market_scanner.py`

**Problem**:
Markets expiring months from now often trade slowly.
Near-expiry markets usually have better urgency and better fill opportunities.

**Fix**:
Add an urgency multiplier based on time to expiry:
- < 3 days: x2.0
- 3-7 days: x1.5
- 7-30 days: x1.0
- > 30 days: x0.5

---

## PRIORITY 3 - Architectural changes

### 3.1 Immediate hedging through user WebSocket callbacks

**Status**: **COMPLETED**

**File**: `polymarketbot.py`

**Problem**:
Fills were being detected early in the scan cycle, but the hedge could still be placed several seconds later.
Those seconds are enough for the opposite book to move beyond `MAX_SUM_CENTS`.

**Fix**:
Process fill updates immediately in the user WebSocket callback and submit the hedge there, under the same lock used by the main loop.

---

### 3.2 Fill-rate tracking per market

**Status**: **PENDING**

**File**: new module `data/fill_tracker.py`

**Problem**:
If YES fills frequently on a market but NO never does, the bot keeps trading there without learning.

**Fix**:
Track, per market:
- YES fills
- NO fills
- completed hedges
- pair completion rate

If a market stays below a minimum completion rate after enough fills, demote or exclude it.

---

### 3.3 Size ladder instead of a single order

**Status**: **PENDING**

**File**: `polymarketbot.py`

**Problem**:
A single order is all-or-nothing. If the bot is second in queue and taker flow is small, it may receive nothing.

**Fix**:
Split each side into two levels, for example:
- Order 1: $0.50 at best bid
- Order 2: $0.50 one cent below

This increases the chance of partial participation and improves inventory granularity.

---

## PRIORITY 4 - KPI-driven optimization after the 10-minute run

**Evidence from the run on 2026-03-08 at 12:06 CET**:
- `debug_pairs_hedge_path = 13` -> the hedge path is finally exercised
- `hedge_fill_rate = 4.8%` -> still too low for live trading
- `perf_unhedged_window_ms avg = 32043`, `max = 158319` -> positions stay open too long
- `perf_book_age_ms avg = 50482`, `max = 476325` -> book data is still too stale too often
- end of session still showed multiple markets in `collecting` / recovery state

This section groups the improvements suggested by real telemetry, not generic speculation.

---

### 4.1 Reduce both the duration and count of unhedged positions

**Status**: **PENDING**

**Why**:
The main bottleneck is no longer just scanning. It is the time spent holding a single-sided position.

**What to measure**:
- number of `UNHEDGED` states per hour
- average and median unhedged duration
- percentage of fills that become completed hedges within 1, 2, or 3 cycles
- leaderboard of markets with the longest recoveries

**What to do**:
- keep immediate hedging via user WebSocket as the default path
- make recovery more aggressive after the first or second missed cycle
- prioritize recovery markets over new entries

**Goal**:
Bring average `perf_unhedged_window_ms` below `10-15s` and eliminate multi-minute cases.

---

### 4.2 Make `MAX_SUM_CENTS` dynamic instead of static

**Status**: **PENDING**

**Why**:
`MAX_SUM_CENTS=103` is good protection, but some markets should tolerate less slack while others justify more flexibility.

**What to measure**:
- average PnL for closes at 100c, 101c, 102c, 103c
- how often 103c closes happen
- relationship between high `MAX_SUM` usage and market quality

**What to do**:
- add reporting by `sum_cents` bucket
- consider stricter caps on slow markets and more permissive caps on liquid markets

**Goal**:
Use 103c as a controlled exception, not a blind universal default.

---

### 4.3 Add market quality scoring, blacklist logic, and penalties for problematic markets

**Status**: **PENDING**

**Why**:
Not all spreads are equal; some markets appear structurally harder to hedge.

**What to measure**:
- markets with the most unhedged states
- markets with the slowest hedges
- worst-PnL markets
- performance by category: sports, pop culture, daily stocks, weather, etc.

**What to do**:
Create a market quality score based on:
- real spread
- real depth
- stale frequency
- fill speed
- historical hedge success
- historical PnL

Reduce priority or exclude markets with consistently poor scores.

---

### 4.4 Improve entry selection with real depth and tradable spread

**Status**: **PENDING**

**Why**:
A large spread with tiny size or an empty hedge side is not a good entry.

**What to measure**:
- available depth on the side needed for the hedge
- difference between theoretical spread and actually tradable spread at target size
- profitable markets excluded or rescued by `PRICE_RANGE`

**What to do**:
- require minimum depth on the opposite side before entering
- validate spread on the actual order size
- consider adaptive `PRICE_RANGE` by market category or volatility

---

### 4.5 Separate entry, recovery, and stress handling more clearly

**Status**: **PENDING**

**Why**:
Recovery behavior is still too close to normal entry logic.
Opening new risk during stress makes the risk profile worse.

**What to measure**:
- new entries while recoveries are active
- max exposure per market and category
- max simultaneous recoveries
- opportunity cost during hold periods

**What to do**:
- add a dedicated recovery mode
- pause or slow new entries when recoveries exceed a threshold
- tighten caps on problematic markets
- add live kill-switch conditions for:
  - too many unhedged positions
  - locked negative PnL beyond threshold
  - average book age beyond threshold
  - too many simultaneous recoveries
  - daily loss cap

---

### 4.6 Clean up dust positions and useless micro-hedges

**Status**: **PENDING**

**Why**:
The logs show tiny hedges like `x0.0004` or `x0.0006`, which add operational noise while barely affecting risk.

**What to measure**:
- how often tiny residuals are created
- where they come from: rounding, partial fills, split orders
- operational cost of micro-hedges

**What to do**:
- introduce a minimum hedge size
- below threshold, either ignore, aggregate, or liquidate explicitly according to policy

---

### 4.7 Measure and exploit the real WebSocket advantage

**Status**: **PENDING**

**Why**:
Not all markets seem to receive equally fresh WebSocket updates.

**What to measure**:
- average latency and update frequency from WebSocket vs REST polling
- hedge success rate with fresh books vs stale books
- markets with the weakest WebSocket feed quality

**What to do**:
- prioritize markets with more stable feeds
- penalize or exclude high-stale-rate markets
- validate the true path `user WS fill -> immediate hedge submit`

---

### 4.8 Separate reporting for healthy pairs, recovery pairs, and pessimistic simulation

**Status**: **PENDING**

**Why**:
Total PnL can hide structural weakness. A bot can be green on paper while relying too much on risky recoveries.

**What to measure**:
- PnL from normal pair closures
- PnL from recovery closures
- average recovery cost
- share of profit consumed by recoveries

**What to do**:
- report `healthy pairs` separately from `recovery pairs`
- add a less optimistic paper simulator:
  - estimated queue priority
  - more realistic partial fills
  - more realistic slippage and latency
- run small A/B tests on the most sensitive parameters:
  - `MAX_SUM`
  - `MIN_SPREAD`
  - hedge aggressiveness
  - stale cancellation
  - minimum depth
  - max markets
  - order size

---

## Priority summary

| # | Area | Change | Impact | Status |
|---|------|--------|--------|--------|
| 0.1 | `.env` | Copy values from `.env.example` | High | Completed |
| 0.2 | `quoter.py` | Guard against book age > 60s | High | Completed |
| 0.3 | `polymarketbot.py` | Alert + action for unhedged positions | High | Completed |
| 0.4 | `metrics_tracker.py` | Per-market KPI tracking | Medium | Completed |
| 0.5 | `README.md` | Document paper vs live gap | Medium | Completed |
| 1.1 | `.env` | `MAX_SUM_CENTS=103` | High | Completed |
| 1.2 | `.env` | `MAX_MARKETS=12`, `ORDER_SIZE=0.5` | High | Completed |
| 1.3 | `.env` | Reprice at 8s / 0.5c | Medium | Completed |
| 2.1 | `quoter.py` | Join top-of-book queue | High | Completed |
| 2.2 | `market_scanner.py` | Sort by volume x spread | High | Completed |
| 2.3 | `market_scanner.py` | Boost near-expiry markets | Medium | Completed |
| 3.1 | `polymarketbot.py` | Immediate WebSocket hedge | High | Completed |
| 3.2 | `fill_tracker.py` | Exclude cold markets | Medium | Pending |
| 3.3 | `polymarketbot.py` | Two-level size ladder | Low | Pending |
| 4.1 | bot + metrics | Reduce unhedged window | High | Pending |
| 4.2 | risk / reporting | Dynamic `MAX_SUM` | High | Pending |
| 4.3 | scanner / metrics | Market quality score + blacklist | High | Pending |
| 4.4 | quoter / scanner | Entry with real depth | Medium | Pending |
| 4.5 | risk engine | Recovery mode + stress guardrails | High | Pending |
| 4.6 | inventory / hedger | Micro-residual floor | Low | Pending |
| 4.7 | websocket / routing | Prefer fresher feeds | Medium | Pending |
| 4.8 | reporting / simulator | Recovery reports + pessimistic paper mode | Medium | Pending |

---

## Suggested workflow

1. Prioritize `3.1`, `4.1`, and `4.5`: the main live bottleneck is now unhedged risk, not just market selection.
2. Repeat short `10-15m` paper runs until `debug_pairs_hedge_path > 0` and `perf_unhedged_window_ms` improves consistently.
3. Implement `4.3` and `4.4` to stop allocating capital to trap spreads and low-quality markets.
4. Add `4.8` to separate healthy PnL from recovery-driven PnL and run small A/B tests on sensitive parameters.
5. Only after two clean runs with controlled recoveries, consider tiny-live tests around `$10-20`.

---

## Recommended `.env` configuration (post-fix)

```env
# Capital
MAX_CAPITAL=50
MAX_PER_MARKET=3
ORDER_SIZE=0.50
MAX_OPEN_ORDERS=25

# Markets
MAX_MARKETS=12
BOOK_ENRICH_LIMIT=300
MIN_SPREAD_CENTS=4
MAX_SUM_CENTS=103
PRICE_RANGE_MIN=0.20
PRICE_RANGE_MAX=0.80
COMPETITION=low

# Timing
SCAN_INTERVAL_SEC=8
HOLD_INTERVAL_SEC=60
REPRICE_INTERVAL_SEC=8
REPRICE_THRESHOLD_CENTS=0.5
MAX_BOOK_AGE_SEC=60
UNHEDGED_ALERT_CYCLES=3

# Risk
DRAWDOWN_LIMIT=-5
MIN_USDC_BUFFER=2

# Guardrails
ALLOW_EXISTING_OPEN_ORDERS=false
ALLOW_EXISTING_POSITIONS=false
ALLOW_FEE_ENABLED_MARKETS=false
MAX_ALLOWED_FEE_RATE_BPS=10000
USE_WEBSOCKET=true
```
