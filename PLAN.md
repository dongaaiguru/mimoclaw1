# MIMOCLAW14 — Master Plan
## Polymarket Super Strategy Bot — $100 Capital

**Date:** April 9, 2026
**Status:** PLANNING
**Goal:** Build a production-grade Polymarket bot that generates daily profits using a multi-engine strategy combining directional signals, market making, logical arbitrage, and information arbitrage — starting with $100 capital.

---

## TABLE OF CONTENTS

1. [Lessons Learned From Previous Bot](#1-lessons-learned)
2. [Capital Analysis & Mathematical Framework](#2-capital-analysis)
3. [Strategy Architecture](#3-strategy-architecture)
4. [Engine 1: Market Making](#4-engine-1-market-making)
5. [Engine 2: Directional Signal (Upgraded)](#5-engine-2-directional-signal)
6. [Engine 3: Logical Arbitrage](#6-engine-3-logical-arbitrage)
7. [Engine 4: Information Arbitrage](#7-engine-4-information-arbitrage)
8. [Order Execution Layer (Critical Fix)](#8-order-execution)
9. [Risk Management](#9-risk-management)
10. [Implementation Roadmap](#10-implementation-roadmap)
11. [Profit Projections](#11-profit-projections)

---

## 1. LESSONS LEARNED FROM PREVIOUS BOT {#1-lessons-learned}

### What Went Wrong

The previous bot attempt had **three critical execution bugs** that silently killed all live trading:

#### Bug #1: FOK Order Type (Fill-or-Kill)
```
Config: liveOrderType = 'FOK'
Problem: FOK demands INSTANT fill at EXACT price, or CANCEL EVERYTHING
Reality on thin orderbooks: Almost never enough liquidity at exact target price
Result: Every order gets canceled. Bot runs, finds opportunities, sends orders → ZERO fills.
Paper trading masked this because paper fills are simulated instantly.
```

**Fix:** Switch to **GTC (Good-Till-Cancelled)** limit orders.
- GTC places a resting order on the book at our target price
- Order sits until filled or manually canceled
- Heartbeat watchdog cancels stale orders if bot goes down
- This is how the original gabagool22 strategy works: buy cheap, wait patiently

#### Bug #2: Slippage Buffer Over-Filter
```
Config: slippageBufferBps = 50 (0.5%)
Problem: Every profitability check inflates completion price by 0.5%
With GTC: No execution slippage — order fills at EXACT listed price
Result: Bot rejects 0.5% of profitable opportunities for no reason
```

**Fix:** Set `slippageBufferBps = 0` for GTC orders.
- GTC limit orders fill at exactly the listed price or better
- No phantom slippage penalty needed
- The buffer was a band-aid for market orders, not applicable to limit orders

#### Bug #3: Silent Failure Mode
```
Problem: No fill confirmation logging
Bot: "Sent order" → exchange rejects it → bot moves on → user sees zero activity
```

**Fix:** Add comprehensive order lifecycle tracking:
- Order submitted → Order acked → Order filled/partial/canceled
- Log rejection reasons
- Track fill rate as a health metric

### Design Principles Moving Forward

1. **GTC limit orders only** — never FOK, never market orders
2. **Zero slippage buffer** — limit orders have deterministic fill prices
3. **Order lifecycle logging** — every order tracked from creation to resolution
4. **Patience over speed** — let the market come to us
5. **Heartbeat watchdog** — cancel stale orders if bot goes offline

---

## 2. CAPITAL ANALYSIS & MATHEMATICAL FRAMEWORK {#2-capital-analysis}

### Starting Capital: $100

We need to be extremely capital-efficient. Every dollar must work.

### Capital Allocation Model

Using Modern Portfolio Theory adapted for prediction markets:

```
Total Capital = $100
Reserve (emergency + gas) = $10 (10%)
Deployable Capital = $90 (90%)
```

#### Per-Engine Allocation (Kelly-Optimized)

| Engine | Allocation | Amount | Rationale |
|--------|-----------|--------|-----------|
| Market Making | 40% | $40 | Highest consistency, reward-boosted |
| Directional Signal | 25% | $25 | High-conviction alpha trades |
| Logical Arbitrage | 20% | $20 | Low-risk math-based edges |
| Information Arbitrage | 15% | $15 | Spike trades on news/crypto |
| **Reserve** | **10%** | **$10** | Gas fees, margin, recovery |

### Kelly Criterion — Position Sizing Formula

For each individual trade:

```
f* = (p × b - q) / b

Where:
  f* = fraction of capital to wager
  p  = probability of winning
  q  = 1 - p (probability of losing)
  b  = odds (profit per $1 risked)

Conservative Kelly: f_actual = f* × 0.25 (25% Kelly)
```

#### Worked Examples

**Market Making (spread capture):**
- p = 0.80 (80% of round-trips complete profitably)
- b = 0.03/0.97 ≈ 0.0309 (3¢ spread on 97¢ cost)
- f* = (0.80 × 0.0309 - 0.20) / 0.0309 = (0.0247 - 0.20) / 0.0309 = -5.67
- Negative Kelly = DON'T use Kelly for market making. Instead use fixed inventory limits.
- **Max position: 30% of engine capital = $12 per market**

**Directional Signal (score > 0.50):**
- p = 0.60 (60% win rate from backtest)
- b = 0.04/0.96 ≈ 0.0417 (4% profit target)
- f* = (0.60 × 0.0417 - 0.40) / 0.0417 = (0.025 - 0.40) / 0.0417 = -8.99
- Negative at 4% target. Use fractional sizing instead.
- **Max position: $6.25 per trade (25% of $25 engine capital)**
- **Max concurrent: 2 positions = $12.50 deployed**

**Logical Arbitrage (mathematical edge):**
- p = 0.75 (75% win rate)
- b = 0.03/0.97 ≈ 0.0309
- f* = (0.75 × 0.0309 - 0.25) / 0.0309 = (0.0232 - 0.25) / 0.0309 = -7.34
- Use fixed sizing: **$5 per leg, max 2 concurrent arb pairs = $10 deployed**

### Minimum Edge Calculation (Critical)

With taker fees on crypto markets: 0.072 (7.2%)
Wait — that's the fee *rate* parameter. The actual fee formula is:

```
fee = feeRate × price × (1 - price) × shares

At price = $0.50:
fee = 0.072 × 0.50 × 0.50 × 100 shares = $1.80 per 100 shares
fee% = $1.80 / $50.00 = 3.6%
```

**Minimum edge required to overcome fees:**
- Crypto: ~3.6% at mid-price (lower at extremes)
- Sports: ~1.5% at mid-price
- **Strategy: Focus on SPORTS markets for market making (lower fees, higher rewards)**

### $100 Growth Projections (Conservative)

| Timeframe | Conservative (1%/day) | Moderate (2%/day) | Aggressive (3%/day) |
|-----------|----------------------|-------------------|---------------------|
| Week 1 | $107 | $115 | $123 |
| Month 1 | $135 | $181 | $243 |
| Month 3 | $246 | $594 | $1,434 |
| Month 6 | $603 | $3,540 | $20,491 |
| Month 12 | $3,630 | $125,352 | $420,688,227 |

**Reality check:** 1%/day sustained is excellent. 2%/day is the stretch goal. 3%/day is unlikely long-term.

**Realistic target: 0.5-1.5%/day = $0.50-$1.50/day on $100 = $15-$45/month**

---

## 3. STRATEGY ARCHITECTURE {#3-strategy-architecture}

### System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                    MIMOCLAW14 SUPER ENGINE                         │
│                                                                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │ ENGINE 1 │  │ ENGINE 2 │  │ ENGINE 3 │  │ ENGINE 4         │ │
│  │ Market   │  │Directional│  │ Logical  │  │ Info Arbitrage   │ │
│  │ Making   │  │ Signal   │  │ Arb      │  │ (News + 5min)    │ │
│  │          │  │          │  │          │  │                  │ │
│  │ $40      │  │ $25      │  │ $20      │  │ $15              │ │
│  │ Always-on│  │ Opportun.│  │ Opportun.│  │ Event-driven     │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────────────┘ │
│       │              │              │              │               │
│  ┌────┴──────────────┴──────────────┴──────────────┴─────────────┐│
│  │              ORDER EXECUTION LAYER                             ││
│  │  • GTC limit orders ONLY (never FOK)                          ││
│  │  • slippageBufferBps = 0                                       ││
│  │  • Full order lifecycle tracking                               ││
│  │  • Heartbeat watchdog for stale orders                         ││
│  │  • HMAC-SHA256 auth                                            ││
│  └──────────────────────────┬────────────────────────────────────┘│
│                              │                                     │
│  ┌──────────────────────────┴────────────────────────────────────┐│
│  │                    RISK MANAGER                                ││
│  │  • Per-engine capital limits                                   ││
│  │  • Cross-engine correlation check                              ││
│  │  • 15% total drawdown circuit breaker                          ││
│  │  • Daily trade limits (50 total)                               ││
│  │  • Gas cost tracking                                           ││
│  │  • Inventory rebalancing                                       ││
│  └───────────────────────────────────────────────────────────────┘│
│                                                                    │
│  ┌───────────────────────────────────────────────────────────────┐│
│  │                    MONITORING & LOGGING                        ││
│  │  • Real-time P&L dashboard                                     ││
│  │  • Order lifecycle logs                                        ││
│  │  • Fill rate tracking                                          ││
│  │  • Alert system (Discord/Telegram)                             ││
│  │  • Daily performance reports                                   ││
│  └───────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
WebSocket (orderbook) ──→ Signal Engines ──→ Score/Signal
                                                  │
                                                  ▼
                                          Risk Manager (approve?)
                                                  │
                                          ┌───────┴───────┐
                                          │    YES        NO
                                          ▼               ▼
                                    GTC Limit        Log & skip
                                    Order Place
                                          │
                                          ▼
                                    Order Lifecycle
                                    Track & Monitor
                                          │
                                    ┌─────┴─────┐
                                    │  Fill?    │
                                    ▼           ▼
                                Update P&L   Cancel/stale?
                                Log trade    Re-evaluate
```

---

## 4. ENGINE 1: MARKET MAKING {#4-engine-1-market-making}

### The Core Idea

Place resting GTC limit orders on BOTH sides of a market. Collect the spread. Earn liquidity rewards on top.

### Mathematical Model

#### Spread Calculation

```python
def calculate_quotes(mid_price, volatility, inventory_position, reward_multiplier):
    """
    Calculate optimal bid/ask quotes for market making.
    
    Args:
        mid_price: Current market midpoint (0-1)
        volatility: Recent price volatility (std dev of returns)
        inventory_position: Current position (-1 to +1, 0 = flat)
        reward_multiplier: Expected reward value per $ traded
    
    Returns:
        (bid_price, ask_price, bid_size, ask_size)
    """
    # Base half-spread: proportional to volatility
    # Higher vol → wider spread (more risk)
    # Lower vol → tighter spread (more competitive)
    base_half_spread = max(0.005, volatility * 2.0)  # Min 0.5¢
    
    # Inventory skew: shift quotes to reduce exposure
    # If long YES, lower both bid and ask (encourage selling)
    # If short YES, raise both bid and ask (encourage buying)
    inventory_skew = inventory_position * 0.01  # 1¢ per full position
    
    # Reward adjustment: tighter spread if rewards are high
    # Higher rewards justify tighter spreads
    reward_adjustment = min(0.003, reward_multiplier * 0.5)
    
    # Final quotes
    half_spread = base_half_spread - reward_adjustment
    half_spread = max(0.003, half_spread)  # Never below 0.3¢
    
    bid = mid_price - half_spread - inventory_skew
    ask = mid_price + half_spread - inventory_skew
    
    # Clamp to valid range
    bid = clamp(bid, 0.01, 0.99)
    ask = clamp(ask, 0.01, 0.99)
    
    # Size: inversely proportional to spread (wider = smaller)
    max_size_per_side = engine_capital * 0.30 / 2  # 30% per side max
    size_factor = 1.0 - (half_spread / 0.05)  # Less size at wider spreads
    size = max_size_per_side * clamp(size_factor, 0.2, 1.0)
    
    return (bid, ask, size, size)
```

#### Inventory Management

```python
class InventoryManager:
    """Track and manage inventory risk for market making."""
    
    MAX_INVENTORY_RATIO = 0.30  # Never hold >30% on one side
    
    def __init__(self, engine_capital):
        self.capital = engine_capital
        self.positions = {}  # slug -> {'yes_qty': x, 'no_qty': y, 'cost_basis': z}
    
    def inventory_ratio(self, slug):
        """Current inventory as fraction of capital (-1 to +1)."""
        pos = self.positions.get(slug, {'yes_qty': 0, 'no_qty': 0})
        total = pos['yes_qty'] + pos['no_qty']
        if total == 0:
            return 0.0
        return (pos['yes_qty'] - pos['no_qty']) / total
    
    def can_add(self, slug, side, size):
        """Check if adding this position exceeds inventory limits."""
        ratio = self.inventory_ratio(slug)
        if side == 'buy_yes':
            new_ratio = ratio + (size / self.capital)
        else:
            new_ratio = ratio - (size / self.capital)
        return abs(new_ratio) <= self.MAX_INVENTORY_RATIO
    
    def round_trip_profit(self, entry_price, exit_price, size, fees):
        """Calculate profit from a complete buy-sell round trip."""
        buy_cost = size * entry_price * (1 + fees)
        sell_revenue = size * exit_price * (1 - fees)
        return sell_revenue - buy_cost
```

#### Market Selection (Which Markets to Make)

```python
def score_market_for_mm(market):
    """
    Score a market for market making attractiveness.
    Higher score = better market to make.
    """
    score = 0.0
    
    # 1. Volatility (want LOW volatility)
    vol = market['volatility_24h']
    vol_score = max(0, 1.0 - vol / 0.10)  # 0 at 10% vol, 1 at 0% vol
    score += 0.30 * vol_score
    
    # 2. Spread (want WIDE spread = more profit per round trip)
    spread = market['current_spread']
    spread_score = clamp(spread / 0.05, 0, 1)  # Normalize to 5¢ max
    score += 0.20 * spread_score
    
    # 3. Volume (want HIGH volume = more fills)
    volume = market['daily_volume']
    volume_score = clamp(volume / 100000, 0, 1)  # Normalize to $100K
    score += 0.20 * volume_score
    
    # 4. Liquidity Rewards (want HIGH rewards)
    rewards = market.get('liquidity_rewards_per_day', 0)
    reward_score = clamp(rewards / 1000, 0, 1)  # Normalize to $1000/day
    score += 0.20 * reward_score
    
    # 5. Fee Level (want LOW fees = more margin)
    fee_rate = market.get('fee_rate', 0.05)
    fee_score = max(0, 1.0 - fee_rate / 0.08)  # 0 at 8%, 1 at 0%
    score += 0.10 * fee_score
    
    return score
```

### Optimal Markets for $40 Capital

| Market Type | Why | Expected Daily |
|-------------|-----|---------------|
| Sports (pre-game, low volatility) | Lowest fees (0.03), highest rewards ($500-10K/game) | $0.80-2.00 |
| Crypto (fee-free geopoltical) | No fees at all | $0.40-1.00 |
| Economics (slow-moving) | Low vol, predictable | $0.30-0.80 |

**Focus: Sports markets in April 2026 — $5M in rewards this month.**

### Daily Operation Cycle

```
00:00 UTC — Rewards distributed for previous day. Log earnings.
00:01 UTC — Re-score all markets. Drop low-scorers, add high-scorers.
00:05 UTC — Place initial quotes on top 5-8 markets.
Every 30s — Adjust quotes based on: price movement, inventory, fills
Every 5min — Re-score markets, add/remove as needed
2min pre-event — Pull liquidity from that market (news risk)
Post-event — Re-evaluate, redeploy capital
```

---

## 5. ENGINE 2: DIRECTIONAL SIGNAL (UPGRADED) {#5-engine-2-directional-signal}

### What Already Exists (from mimoclaw1)

The existing bot implements:
- EV (Expected Value) — weight 0.35
- KL Divergence — weight 0.20
- Bayesian DeltaP — weight 0.20
- LMSR Edge — weight 0.15
- Stoikov Risk — weight -0.10

```
Score = 0.35×EV + 0.20×KL + 0.20×ΔP + 0.15×LMSR - 0.10×Risk
Trade if Score > 0.50 AND all hard filters pass
```

### Upgrades Needed

1. **Exchange price feed** — the existing code has this concept but needs a real Binance/Coinbase WebSocket for BTC/ETH spot prices
2. **GTC execution** — switch from simulated fills to real GTC limit orders
3. **Position tracking** — integrate with the multi-engine risk manager

### Capital Constraints ($25 engine allocation)

| Parameter | Value |
|-----------|-------|
| Max position | $6.25 (25% Kelly) |
| Max concurrent | 2 positions |
| Max deployed | $12.50 |
| Min edge | 1.5% (above fees) |
| Max spread | 3% |
| Stop loss | 8% |
| Profit target | 4% |

---

## 6. ENGINE 3: LOGICAL ARBITRAGE {#6-engine-3-logical-arbitrage}

### How It Works

Detect mathematically impossible pricing between related markets.

#### Dependency Types

```python
class DependencyType(Enum):
    SUBSET = "subset"      # A implies B (Trump wins → Republican wins)
    SUPERSET = "superset"  # B implies A (Republican wins ← Trump wins)
    MUTUAL_EXCL = "mutex"  # A and B cannot both be true
    COMPLEMENT = "comp"    # A and B must sum to 1.00

@dataclass
class MarketDependency:
    market_a: str
    market_b: str
    dep_type: DependencyType
    confidence: float  # 0-1, how certain is this dependency?
    discovered_by: str  # "manual", "llm", "rule-based"
```

#### Arbitrage Detection

```python
def detect_arbitrage(dep: MarketDependency, price_a: float, price_b: float):
    """Check if dependency is violated by current prices."""
    
    if dep.dep_type == DependencyType.SUBSET:
        # A implies B: P(B) >= P(A)
        if price_b < price_a - 0.03:  # 3¢ violation threshold
            return ArbitrageSignal(
                action_a="sell", action_b="buy",
                edge=price_a - price_b,
                confidence=dep.confidence
            )
    
    elif dep.dep_type == DependencyType.COMPLEMENT:
        # YES + NO should = 1.00
        total = price_a + price_b
        if total < 0.97:  # 3¢ under
            return ArbitrageSignal(
                action_a="buy", action_b="buy",
                edge=1.00 - total,
                confidence=1.0  # Mathematical certainty
            )
    
    elif dep.dep_type == DependencyType.MUTUAL_EXCL:
        # Cannot both be true — sum should be <= 1.00
        # (can both be false, so only check sum > 1.00)
        if price_a + price_b > 1.03:
            return ArbitrageSignal(
                action_a="sell", action_b="sell",
                edge=price_a + price_b - 1.00,
                confidence=dep.confidence
            )
    
    return None
```

#### Dependency Discovery

Use LLM to read market descriptions and identify logical relationships:

```python
DEPENDENCY_PROMPT = """
Given these two Polymarket markets:

Market A: "{question_a}"
Market B: "{question_b}"

Determine if there is a logical relationship between them.
Is A a subset of B? (If A resolves YES, B MUST resolve YES)
Is B a subset of A?
Are they mutually exclusive? (Cannot both resolve YES)
Are they complements? (Must sum to 100%)

Output JSON:
{{
  "dependency": "subset|superset|mutex|complement|independent",
  "confidence": 0.0-1.0,
  "explanation": "..."
}}
"""
```

### $20 Capital Constraints

| Parameter | Value |
|-----------|-------|
| Max per leg | $5.00 |
| Max concurrent pairs | 2 |
| Max deployed | $10.00 |
| Min violation threshold | 3¢ (3%) |
| Execution window | <500ms for both legs |

---

## 7. ENGINE 4: INFORMATION ARBITRAGE {#7-engine-4-information-arbitrage}

### Strategy A: News-Based Probability Arbitrage

Monitor news feeds → detect market-moving events → trade before price adjusts.

```python
class NewsArbitrageEngine:
    def __init__(self):
        self.news_sources = [
            "reuters", "ap", "bloomberg",
            "polymarket_alerts", "twitter_verified"
        ]
        self.market_keywords = self._build_keyword_index()
    
    async def on_news(self, headline, source, timestamp):
        # 1. Classify news relevance to active markets
        affected_markets = self.classify_impact(headline)
        
        for market in affected_markets:
            # 2. Estimate new probability using ensemble AI
            new_prob = await self.ensemble_estimate(market, headline)
            
            # 3. Compare to current market price
            current = market.current_price
            divergence = abs(new_prob - current)
            
            if divergence > 0.15:  # 15% divergence threshold
                # 4. Execute GTC limit order
                side = "buy_yes" if new_prob > current else "buy_no"
                price = current + 0.02 if new_prob > current else current - 0.02
                await self.place_order(market, side, price, self.calculate_size(divergence))
```

### Strategy B: 5-Minute Crypto Market Sniper

Polymarket has BTC/ETH/SOL/XRP "Up or Down — 5 min" markets.

**The Edge:** Monitor Chainlink oracle price feed directly. When BTC crosses the threshold, you know the resolution before Polymarket's UI updates. 2-15 second window.

```python
class FiveMinSniper:
    """
    Monitor Chainlink price feeds for 5-minute crypto markets.
    When price crosses market threshold, place GTC order
    on the winning side before the market adjusts.
    """
    
    def __init__(self):
        self.chainlink_ws = "wss://..."  # Chainlink data stream
        self.active_markets = {}  # condition_id -> market data
    
    async def on_price_update(self, asset, price, timestamp):
        for cid, market in self.active_markets.items():
            if market['asset'] != asset:
                continue
            
            target = market['price_to_beat']
            seconds_remaining = market['end_time'] - timestamp
            
            if seconds_remaining < 5 and seconds_remaining > 0:
                # Market about to resolve — check if price crossed
                if price > target and market['current_yes_price'] < 0.95:
                    # BTC is above target, YES should be ~$1.00
                    # Buy YES at current (cheap) price
                    await self.place_snipe(market, "buy_yes", market['current_yes_price'])
                elif price < target and market['current_yes_price'] > 0.05:
                    # BTC is below target, NO should be ~$1.00
                    await self.place_snipe(market, "buy_no", market['current_no_price'])
```

### $15 Capital Constraints

| Parameter | Value |
|-----------|-------|
| Max per trade | $3.75 (25% Kelly) |
| Max concurrent | 2 |
| Max deployed | $7.50 |
| Divergence threshold | 15% (news) |
| Execution target | <5 seconds (crypto) |

---

## 8. ORDER EXECUTION LAYER (CRITICAL FIX) {#8-order-execution}

### The Previous Bugs and Their Fixes

This is the most critical section. The previous bot failed because of execution issues.

### Bug #1: FOK → GTC

```python
# ❌ OLD (broken for live trading)
ORDER_TYPE = "FOK"  # Fill-or-Kill: instant fill or cancel

# ✅ NEW (patient, reliable)
ORDER_TYPE = "GTC"  # Good-Till-Cancel: rest on book until filled
```

**Why GTC is correct:**
- FOK demands instant fill at exact price → almost never works on thin orderbooks
- GTC places resting limit order → market comes to you
- Heartbeat watchdog cancels stale orders if bot goes offline
- This is how professional market makers operate

### Bug #2: Slippage Buffer → Zero

```python
# ❌ OLD (phantom cost killing profitable trades)
slippageBufferBps = 50  # Inflates price by 0.5% for no reason

# ✅ NEW (GTC has deterministic fill price)
slippageBufferBps = 0   # No slippage on limit orders
```

**Why zero is correct:**
- GTC limit orders fill at exactly the listed price or better
- There is no execution slippage with limit orders
- The 50 bps buffer was rejecting ~0.5% of profitable opportunities

### Order Lifecycle Manager

```python
@dataclass
class Order:
    id: str
    market_slug: str
    side: str        # "buy_yes", "buy_no", "sell_yes", "sell_no"
    price: float
    size: float
    engine: str      # "mm", "directional", "arb", "info"
    status: str      # "pending", "acked", "filled", "partial", "canceled", "rejected"
    created_at: float
    updated_at: float
    filled_qty: float = 0.0
    fill_price: float = 0.0
    rejection_reason: str = ""

class OrderLifecycleManager:
    """Track every order from creation to resolution."""
    
    def __init__(self, config):
        self.config = config
        self.orders: Dict[str, Order] = {}
        self.heartbeat_interval = 30  # seconds
        self.max_order_age = 300      # 5 minutes — cancel stale orders
    
    async def place_order(self, market, side, price, size, engine):
        """Place a GTC limit order with full lifecycle tracking."""
        
        # Validate
        if not self._validate_order(market, side, price, size):
            return None
        
        # Create order record
        order = Order(
            id=generate_id(),
            market_slug=market['slug'],
            side=side,
            price=round(price, 4),
            size=round(size, 2),
            engine=engine,
            status="pending",
            created_at=time.time(),
            updated_at=time.time(),
        )
        
        # Submit to exchange
        try:
            response = await self._submit_to_exchange(order)
            order.status = "acked"
            order.exchange_id = response['order_id']
            log.info(f"ORDER ACK | {order.engine} | {order.side} | "
                     f"{order.market_slug} | ${order.size:.2f} @ {order.price:.4f}")
        except Exception as e:
            order.status = "rejected"
            order.rejection_reason = str(e)
            log.error(f"ORDER REJECTED | {order.engine} | {e}")
            return None
        
        self.orders[order.id] = order
        return order
    
    async def heartbeat(self):
        """Periodic check: cancel stale orders, reconcile fills."""
        while self.running:
            now = time.time()
            for oid, order in list(self.orders.items()):
                age = now - order.created_at
                
                # Cancel stale orders (no fill after max_order_age)
                if order.status == "acked" and age > self.max_order_age:
                    await self.cancel_order(order)
                    log.warning(f"ORDER STALE | {order.engine} | {oid} | "
                               f"Age: {age:.0f}s — canceled")
                
                # Check for fills via exchange API
                if order.status == "acked":
                    await self._check_fill(order)
            
            await asyncio.sleep(self.heartbeat_interval)
    
    def get_stats(self):
        """Order execution health metrics."""
        total = len(self.orders)
        filled = sum(1 for o in self.orders.values() if o.status == "filled")
        rejected = sum(1 for o in self.orders.values() if o.status == "rejected")
        canceled = sum(1 for o in self.orders.values() if o.status == "canceled")
        
        return {
            "total_orders": total,
            "fill_rate": filled / max(1, total - rejected),
            "rejection_rate": rejected / max(1, total),
            "cancellation_rate": canceled / max(1, total),
            "avg_fill_time": self._avg_fill_time(),
        }
```

### Exchange Auth (HMAC-SHA256)

```python
class PolymarketAuth:
    """HMAC-SHA256 authentication for Polymarket CLOB API."""
    
    def __init__(self, api_key, api_secret, passphrase):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
    
    def sign_request(self, method, path, body=""):
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method + path + body
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return {
            "Content-Type": "application/json",
            "API-KEY": self.api_key,
            "API-SIGNATURE": signature,
            "API-TIMESTAMP": timestamp,
            "API-PASSPHRASE": self.passphrase,
        }
```

---

## 9. RISK MANAGEMENT {#9-risk-management}

### Portfolio-Level Risk Controls

```python
@dataclass
class RiskLimits:
    # Capital
    total_capital: float = 100.0
    reserve_pct: float = 0.10
    max_deployed_pct: float = 0.85  # Max 85% deployed at any time
    
    # Drawdown
    max_drawdown_pct: float = 0.15  # Circuit breaker at 15%
    warning_drawdown_pct: float = 0.10  # Reduce size at 10%
    
    # Trading
    max_daily_trades: int = 50
    max_concurrent_positions: int = 6  # Across all engines
    
    # Per-Engine
    engine_limits: Dict[str, float] = field(default_factory=lambda: {
        "mm": 0.40,           # 40% of capital
        "directional": 0.25,   # 25%
        "arb": 0.20,          # 20%
        "info": 0.15,         # 15%
    })
    
    # Position
    max_position_pct: float = 0.25  # Max 25% of engine in single position
    max_same_event: int = 2  # Max 2 positions in same event


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.peak_capital = limits.total_capital
        self.daily_trades = 0
        self.positions = {}
    
    def check_drawdown(self, current_capital):
        """Circuit breaker on drawdown."""
        drawdown = (self.peak_capital - current_capital) / self.peak_capital
        
        if drawdown >= self.limits.max_drawdown_pct:
            return Decision.BLOCK, f"DRAWDOWN CIRCUIT BREAKER: {drawdown:.1%}"
        elif drawdown >= self.limits.warning_drawdown_pct:
            return Decision.REDUCE, f"Drawdown warning: {drawdown:.1%}"
        return Decision.ALLOW, ""
    
    def can_trade(self, engine, size, market_slug):
        """Master approval gate for all trades."""
        
        # 1. Daily limit
        if self.daily_trades >= self.limits.max_daily_trades:
            return False, "daily_limit"
        
        # 2. Drawdown
        dd_decision, dd_reason = self.check_drawdown(self.current_capital)
        if dd_decision == Decision.BLOCK:
            return False, dd_reason
        
        # 3. Engine capital limit
        engine_deployed = self.engine_exposure(engine)
        engine_max = self.limits.total_capital * self.limits.engine_limits[engine]
        if engine_deployed + size > engine_max:
            return False, f"engine_cap_limit({engine})"
        
        # 4. Total deployment
        total_deployed = sum(self.engine_exposure(e) for e in self.limits.engine_limits)
        if total_deployed + size > self.limits.total_capital * self.limits.max_deployed_pct:
            return False, "total_deployment_limit"
        
        # 5. Same-event concentration
        event_positions = self.positions_in_event(market_slug)
        if len(event_positions) >= self.limits.max_same_event:
            return False, "same_event_limit"
        
        return True, "approved"
```

### Gas Cost Tracking

```python
class GasTracker:
    """Track Polygon gas costs to ensure they don't eat profits."""
    
    def __init__(self, daily_budget=2.00):
        self.daily_budget = daily_budget  # Max $2/day on gas
        self.daily_spent = 0.0
    
    def can_execute(self, estimated_gas_usd):
        if self.daily_spent + estimated_gas_usd > self.daily_budget:
            return False
        return True
    
    def record(self, actual_gas_usd):
        self.daily_spent += actual_gas_usd
```

---

## 10. IMPLEMENTATION ROADMAP {#10-implementation-roadmap}

### Phase 1: Foundation (Week 1)
**Goal: Fix execution, get paper trading working with GTC**

- [ ] Fix order execution layer (FOK → GTC, slippageBufferBps → 0)
- [ ] Implement OrderLifecycleManager with full tracking
- [ ] Implement heartbeat watchdog for stale order cancellation
- [ ] Upgrade auth module (HMAC-SHA256)
- [ ] Build RiskManager with all limits
- [ ] Test with paper trading — verify orders are placed, tracked, filled
- [ ] Verify fill rate metrics work

### Phase 2: Market Making Engine (Week 2)
**Goal: MM bot placing quotes on sports markets**

- [ ] Implement spread calculation model
- [ ] Implement inventory manager
- [ ] Implement market scoring (which markets to make)
- [ ] Integrate Polymarket Liquidity Rewards API
- [ ] Build quote adjustment loop (every 30s)
- [ ] Build news-event liquidity withdrawal (2min pre-event)
- [ ] Paper test on 3-5 sports markets

### Phase 3: Logical Arb Engine (Week 3)
**Goal: Detect and exploit cross-market dependency violations**

- [ ] Implement dependency data structures
- [ ] Build LLM-based dependency discovery (prompt + API)
- [ ] Implement arb detection logic (subset, complement, mutex)
- [ ] Build multi-leg execution (both legs within 500ms)
- [ ] Pre-populate known dependencies (BTC Jun/Dec, MegaETH 1B/2B)
- [ ] Paper test on known relationships

### Phase 4: Information Arb Engine (Week 4)
**Goal: News-based and 5-min crypto signals**

- [ ] Integrate news feed (RSS/webhook from Reuters/AP or Twitter API)
- [ ] Build ensemble probability estimator (multiple LLMs)
- [ ] Implement divergence detection (>15% threshold)
- [ ] Build 5-min crypto market monitor (Chainlink feed)
- [ ] Implement snipe execution for crypto resolution
- [ ] Paper test on live news events

### Phase 5: Integration & Live (Week 5)
**Goal: All 4 engines running together, live with $100**

- [ ] Build multi-engine orchestrator
- [ ] Cross-engine risk management (correlation, total exposure)
- [ ] Build real-time dashboard (P&L, fill rates, per-engine stats)
- [ ] Implement alert system (Discord/Telegram notifications)
- [ ] **Go live with $100** — start conservative, scale up
- [ ] Daily performance logging and review

### Phase 6: Optimization (Week 6+)
**Goal: Tune parameters, scale up winners**

- [ ] A/B test spread widths on market making
- [ ] Optimize market selection algorithm
- [ ] Tune Kelly fractions based on live results
- [ ] Add cross-platform arb (Polymarket ↔ Kalshi) if capital allows
- [ ] Scale up capital allocation to winning engines

---

## 11. PROFIT PROJECTIONS {#11-profit-projections}

### Daily Income Model ($100 Capital)

| Engine | Capital | Win Rate | Avg Profit/Win | Trades/Day | Daily Income |
|--------|---------|----------|----------------|------------|-------------|
| Market Making | $40 | 80% | $0.15 | 10-20 | $1.20-2.40 |
| Directional | $25 | 60% | $0.50 | 1-3 | $0.30-0.90 |
| Logical Arb | $20 | 75% | $0.25 | 1-2 | $0.19-0.38 |
| Info Arb | $15 | 65% | $1.00 | 0-2 | $0.00-1.30 |
| **Subtotal** | **$100** | | | | **$1.69-4.98** |
| Minus gas | | | | | -$0.50 |
| Minus losses | | | | | -$0.30 |
| **Net Daily** | | | | | **$0.89-4.18** |

### Realistic Monthly Projection

| Scenario | Daily Avg | Monthly | ROI |
|----------|----------|---------|-----|
| Conservative | $0.89 | $26.70 | 26.7% |
| Moderate | $2.50 | $75.00 | 75.0% |
| Optimistic | $4.18 | $125.40 | 125.4% |

### Break-Even Analysis

- Gas costs: ~$0.50/day (Polygon transactions)
- Min daily income to break even: $0.50
- Market making alone should cover this with $40 capital

### Scaling Plan

| Milestone | Capital | Action |
|-----------|---------|--------|
| $100 → $200 | Initial | Conservative, prove strategy |
| $200 → $500 | Scale | Increase position sizes |
| $500 → $1000 | Expand | Add more markets, wider scanning |
| $1000+ | Compound | Full allocation, all engines at scale |

---

## APPENDIX A: CONFIGURATION

```python
# config.py
class Config:
    # Capital
    initial_capital = 100.00
    reserve_pct = 0.10
    
    # Engine Allocations
    mm_allocation = 0.40        # $40
    directional_allocation = 0.25  # $25
    arb_allocation = 0.20       # $20
    info_allocation = 0.15      # $15
    
    # Order Execution (THE CRITICAL FIXES)
    order_type = "GTC"          # ✅ Good-Till-Cancel (was FOK)
    slippage_buffer_bps = 0     # ✅ Zero (was 50)
    max_order_age_seconds = 300 # Cancel orders older than 5min
    heartbeat_interval = 30     # Check fills every 30s
    
    # Market Making
    mm_min_spread = 0.003       # 0.3¢ minimum half-spread
    mm_max_spread = 0.05        # 5¢ maximum half-spread
    mm_max_inventory = 0.30     # 30% max on one side
    mm_adjustment_interval = 30 # Adjust quotes every 30s
    
    # Risk
    max_drawdown = 0.15         # 15% circuit breaker
    max_daily_trades = 50
    max_deployed_pct = 0.85
    
    # Directional Signal (existing)
    score_threshold = 0.50
    min_edge = 0.015
    max_spread = 0.03
    stop_loss = 0.08
    profit_target = 0.04
    
    # API
    clob_url = "https://clob.polymarket.com"
    gamma_url = "https://gamma-api.polymarket.com"
    ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
```

---

## APPENDIX B: FILE STRUCTURE

```
polymarket-engine/
├── README.md
├── PLAN.md                    # This file
├── strategy.md                # Mathematical derivations
├── system_design.md           # Architecture
├── bot.py                     # Main entry point (multi-engine orchestrator)
├── config.py                  # Configuration (all parameters)
├── requirements.txt
├── .env.example
│
├── src/
│   ├── __init__.py
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── order_manager.py   # Order lifecycle (GTC, tracking, heartbeat)
│   │   ├── auth.py            # HMAC-SHA256 authentication
│   │   └── exchange.py        # Polymarket REST + WebSocket client
│   │
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── market_maker.py    # Engine 1: Market making
│   │   ├── directional.py     # Engine 2: Score-based signals (from mimoclaw1)
│   │   ├── logical_arb.py     # Engine 3: Cross-market dependency arb
│   │   └── info_arb.py        # Engine 4: News + 5min crypto
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── score.py           # Unified scoring (existing, upgraded)
│   │   ├── inventory.py       # Inventory management for MM
│   │   ├── dependency.py      # Market dependency detection
│   │   └── ensemble.py        # Ensemble AI probability estimator
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── risk_manager.py    # Portfolio-level risk controls
│   │   └── gas_tracker.py     # Gas cost monitoring
│   │
│   └── monitoring/
│       ├── __init__.py
│       ├── dashboard.py       # Real-time P&L and stats
│       └── alerts.py          # Discord/Telegram notifications
│
├── backtest/
│   ├── backtest_proven.py     # Existing backtest
│   ├── backtest_mm.py         # Market making backtest
│   └── backtest_combined.py   # Multi-engine backtest
│
└── tests/
    ├── test_order_manager.py
    ├── test_market_maker.py
    ├── test_logical_arb.py
    └── test_risk_manager.py
```
