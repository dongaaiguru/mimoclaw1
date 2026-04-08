# Polymarket Trading Strategies Research — April 2026

## Executive Summary

The existing `mimoclaw1` repo implements a **directional signal engine** (Bayesian + EV + LMSR + Stoikov). It's solid for alpha generation but missing **4 proven profit strategies** that top traders use to generate daily income. This research identifies those strategies and proposes a combined "super strategy."

---

## How Top Traders Actually Make Money on Polymarket

### The Landscape (April 2026)

- Polymarket runs a CLOB (Central Limit Order Book) on Polygon
- Total volume: $9B+ cumulative
- April 2026 liquidity rewards: **$5M/month** across sports & esports
- Maker rebates: 20-25% of taker fees returned to liquidity providers
- Taker fees: 0.072 (crypto), 0.03 (sports), 0.04-0.05 (other categories)
- Competition: Only 3-4 serious automated market makers on the entire platform
- Average arbitrage opportunity duration: **2.7 seconds** (down from 12.3s in 2024)
- 73% of arb profits captured by sub-100ms execution bots

### Top Profitable Strategies (ranked by consistency)

---

## Strategy 1: Automated Market Making (AMM) — The Cash Cow

**Win Rate: 78-85% | Returns: 1-3% monthly | Volatility: Low**

### How It Works
Place limit orders on BOTH sides of a market:
- Sell YES at $0.48, Buy YES at $0.45 → pocket 3¢ spread
- You're running a mini-casino — profit regardless of outcome

### Real Example (Jan 2026)
A bot on "Will Bitcoin hit $100k by February?":
- Bought YES at $0.45, sold at $0.48 (42 times)
- Bought NO at $0.51, sold at $0.54 (38 times)
- **Net profit: $1,247 on $10k capital (12.47% in 3 weeks)**

### Why It Works Now
- Almost nobody provides liquidity on Polymarket — competition is tiny
- Spreads are wider than they should be (free money)
- Order flow is predictable (retail panic sells, bots buy)
- Polymarket PAYS you to do this via Liquidity Rewards + Maker Rebates

### Liquidity Rewards (April 2026)
- **$5M distributed** across sports/esports markets
- EPL games: $10,000/game ($2,800 pre + $7,200 live)
- Champions League QFs: $24,000/game
- Formula rewards: two-sided orders at 3x rate vs single-sided
- Quadratic scoring: closer to midpoint = exponentially more rewards

### Maker Rebates
- 20-25% of taker fees returned to makers daily in USDC
- Crypto markets: 20% rebate, taker fee 0.072
- Sports markets: 25% rebate, taker fee 0.03

### Open Source Reference
- `@defiance_cr` made **$700-800/day** at peak with $10k capital
- Open-sourced his bot: https://github.com/warproxxx/poly-maker
- Key: find low-volatility markets with high rewards (mismatch = free money)

### Implementation Requirements
- Monitor orderbook depth across 100+ markets
- Place limit orders on both sides with calculated spreads
- Adjust positioning every 30 seconds based on inventory
- Withdraw liquidity 2 min before major news events
- Inventory limits: never hold >30% on one side
- Widen spreads automatically when volatility spikes

---

## Strategy 2: Cross-Market Logical Arbitrage — The IQ Play

**Win Rate: 70-80% | Returns: 2-5% monthly | Volatility: Low-Medium**

### How It Works
Exploit pricing inconsistencies between correlated markets:

**Example 1: Subset Violation**
- "Trump wins 2028 election" = 35%
- "Republican wins 2028 election" = 32%
- **Impossible.** Trump IS a Republican. If Trump wins, Republican wins.
- Trade: Buy "Republican wins" at 32%. Guaranteed profit if math corrects.

**Example 2: Cumulative Probability Violation**
- "Recession in Jan": 12%, "Feb": 15%, "Mar": 18%, "Apr": 14%, "None": 52%
- Total: 111% — mathematically impossible (must sum to 100%)
- Trade: Sell overpriced outcomes, buy underpriced ones.

### Academic Research (IMDEA Networks, Aug 2025)
- Analyzed **86 million transactions** on Polymarket
- Found **$40 million** in guaranteed arbitrage profits
- Top trader: **$2,009,632** from 4,049 trades ($496/trade average)
- 41% of all conditions were mispriced by average of 40%
- Used Bregman divergence + Frank-Wolfe algorithm + integer programming

### Why It Persists
- Fragmented attention: traders focus on single markets, not relationships
- Cognitive load: identifying correlations requires systematic analysis
- Execution complexity: requires simultaneous multi-leg trades
- 305 markets during election = 46,360 possible pairs to check

### Implementation
- Map logical relationships between all active markets (graph theory)
- Calculate implied probabilities based on correlations
- Flag violations where implied probability differs by >3%
- Execute multi-leg trades within 500ms window
- Use LLM (DeepSeek/GPT) to detect dependencies — 81% accuracy on complex markets

---

## Strategy 3: AI-Powered Information Arbitrage — The Speed Edge

**Win Rate: 65-75% | Returns: 3-8% monthly | Volatility: Medium**

### How It Works
When breaking news hits, Polymarket prices take 30 seconds to 5 minutes to fully adjust. AI processes information faster than humans.

### Real Example (Jan 14, 2026)
- News: Key witness in Trump case recants testimony
- AI bot:
  - Ingested AP article (2s)
  - Cross-referenced 3 sources (3s)
  - Sentiment analysis on 500 expert Twitter accounts (4s)
  - Calculated: "Charges Dismissed" jumped from 23% → 41%
  - Market still at 28%
  - Bought YES at $0.29
- **Result: $896 profit in under 10 minutes** ($2,000 position, 13¢ spread)

### 5-Minute Crypto Markets (NEW — BTC/ETH/SOL/XRP)
- Polymarket has "Up or Down — 5 min" markets for crypto
- A bot monitors the **Chainlink oracle data stream** directly
- When BTC price crosses threshold, bot knows resolution before Polymarket's UI updates
- **2-15 second execution window** before general market reacts
- Reference: Bot with **98% win rate** trading BTC/ETH/SOL 15-min markets, $4-5k per trade

### Implementation
- Real-time news feeds: Reuters, AP, Bloomberg via API
- Social sentiment: 1,200+ verified expert Twitter accounts
- Ensemble models: multiple LLMs voting on probability
- Generate signal when market diverges >15% from AI consensus
- Sub-100ms execution via dedicated Polygon RPC

---

## Strategy 4: Cross-Platform Arbitrage (Polymarket ↔ Kalshi)

**Win Rate: 70-85% | Returns: 2-4% monthly | Volatility: Low**

### How It Works
Same events are priced differently on Polymarket vs Kalshi:
- Polymarket: "Fed Rate Cut in June" = 60%
- Kalshi: "Fed Rate Cut in June" = 55%
- Trade: Buy on Kalshi, sell on Polymarket. 5¢ spread minus fees = profit.

### Consistent Spreads
- Reddit traders report finding 5¢+ spreads consistently
- Best on: political events, Fed decisions, economic indicators
- Both platforms now have APIs for automated execution

### Risks
- Capital split across two platforms
- Settlement timing differences
- Regulatory differences (Kalshi is CFTC-regulated, Polymarket is not for US users)
- Gas/withdrawal friction

---

## The Super Strategy: Combined Multi-Engine System

### Architecture: 4 Engines Running in Parallel

```
┌─────────────────────────────────────────────────────────────┐
│                  SUPER STRATEGY ENGINE                        │
├──────────┬──────────┬──────────┬────────────────────────────┤
│ ENGINE 1 │ ENGINE 2 │ ENGINE 3 │ ENGINE 4                   │
│ Directional│ Market  │ Logical  │ Info Arbitrage              │
│ Signal    │ Making  │ Arb      │ (News + 5min crypto)       │
│ (existing)│ (NEW)   │ (NEW)    │ (NEW)                      │
│           │         │          │                            │
│ Score>0.50│ Spread  │ Cross-   │ Ensemble AI                │
│ + info    │ capture │ market   │ probability                │
│ advantage │ + rewards│ deps    │ vs market price            │
│           │         │          │                            │
│ 37% runs  │ 78-85%  │ 70-80%   │ 65-75%                     │
│ profitable│ win rate│ win rate │ win rate                   │
├──────────┴──────────┴──────────┴────────────────────────────┤
│                    RISK MANAGER                               │
│  • Portfolio allocation across engines                        │
│  • Kelly sizing per engine                                    │
│  • Cross-engine correlation limits                            │
│  • Circuit breaker: 15% total drawdown                        │
│  • Daily trade limits per engine                              │
└─────────────────────────────────────────────────────────────┘
```

### Capital Allocation ($50 starting)

| Engine | Allocation | Purpose | Expected Daily |
|--------|-----------|---------|---------------|
| Directional (existing) | 25% ($12.50) | High-conviction alpha trades | $0.50-2.00 |
| Market Making (NEW) | 40% ($20.00) | Spread capture + rewards | $1.00-4.00 |
| Logical Arb (NEW) | 20% ($10.00) | Guaranteed math edges | $0.50-2.00 |
| Info Arbitrage (NEW) | 15% ($7.50) | News speed + 5min crypto | $0.50-3.00 |
| **Total** | **100% ($50)** | **Diversified daily income** | **$2.50-11.00/day** |

### Why This Combination Works

1. **Market Making is the foundation** — it generates income whether or not you have directional edge. The liquidity rewards program literally pays you to provide liquidity. With only 3-4 competitors, this is massively underexploited.

2. **Directional signals (existing) are the alpha** — when you DO have a strong view (exchange price divergence, Bayesian momentum), you size up. The existing engine handles this.

3. **Logical arbitrage is the safety net** — pure math, no prediction needed. Cross-market dependency violations are shockingly common and persistent.

4. **Information arbitrage is the spike** — when news breaks or 5min crypto markets open, speed wins. These are occasional but highly profitable trades.

### Key Advantages Over Single-Strategy Bots

- **Income in ALL market conditions** (trending, ranging, quiet, volatile)
- **Liquidity rewards are uncorrelated** to market direction
- **Multiple small edges compound** into daily profits
- **Drawdown protection** — if one engine has a bad day, others compensate

### Critical Requirements

1. **Speed**: Sub-100ms WebSocket monitoring, dedicated Polygon RPC
2. **Always-on**: 24/7 operation — market making requires constant presence
3. **Order management**: Cancel/replace orders every 30s based on conditions
4. **News ingestion**: Real-time feeds for information arbitrage
5. **Graph engine**: Map cross-market dependencies for logical arb
6. **Inventory management**: Track exposure across all engines

### Risk Controls

| Control | Setting |
|---------|---------|
| Max position per market | 25% of engine capital |
| Max total exposure | 60% of capital |
| Circuit breaker | 15% drawdown = stop all engines |
| Daily trade limit | 50 total across all engines |
| Inventory limit (MM) | 30% max on one side |
| News blackout | Pull MM liquidity 2 min before events |
| Correlation limit | Max 3 markets in same event |

---

## Sources

1. "Beyond Simple Arbitrage: 4 Polymarket Strategies Bots Actually Profit From in 2026" — Medium (Feb 2026)
2. "Just Found the Math That Guarantees Profit on Polymarket" — DevGenius (Feb 2026)
3. "Automated Market Making on Polymarket" — Polymarket News (@defiance_cr interview)
4. "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets" — IMDEA Networks (Aug 2025)
5. Polymarket Official Docs: Liquidity Rewards, Maker Rebates (April 2026)
6. "Market Making on Prediction Markets: Complete 2026 Guide" — NYC Servers (Jan 2026)
7. Yahoo Finance: "Arbitrage Bots Dominate Polymarket With Millions in Profits" (Jan 2026)
8. Reddit r/algotrading: Cross-platform Polymarket-Kalshi arbitrage bots (Jan 2026)
